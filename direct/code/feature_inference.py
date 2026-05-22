"""Infer 3D features from projected views.

Strategy:
  1. Among the three views, pick the **most informative** outline (most
     edges, or any outline containing ARC / non-rectangular shape) as the
     extrusion profile. Extrude it along the axis perpendicular to that
     view by the perpendicular extent of the part.
       - top   outline -> profile in XY, extrude along +Z by H
       - front outline -> profile in XZ, extrude along +Y by D
    - left outline -> profile in YZ, extrude along +X by W
     Falls back to a bounding-box block if no closed outline is found.
  2. A single same-radius CIRCLE in all three canonical views becomes a sphere
    when the projected centers agree across TOP/FRONT/LEFT.
  3. CIRCLE entities become through-holes:
       circle in TOP   view -> hole axis = Z
       circle in FRONT view -> hole axis = Y
    circle in LEFT view -> hole axis = X
    4. For prismatic polygon profiles, visible front/left arc offsets become
      a top/bottom arc-profile edge treatment.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ...dxf_loader import DxfEntity
from ...projection_mapper import ProjectedView
from ...geometry_estimator import (
    Outline, extract_outline_and_holes, extract_closed_outlines_and_circles,
    estimate_part_size,
)
from ...view_classifier import ViewBundle


@dataclass
class Feature:
    kind: str   # "extrude_profile" | "base_block" | "sphere" | "cylinder_stack" | "hole" | "profile_cut" | "edge_chamfer"
    params: Dict = field(default_factory=dict)

    def to_dict(self):
        return {"kind": self.kind, "params": self.params}


# Map view name -> (sketch plane, extrusion-axis name)
_VIEW_PLANE = {
    "top":   "XY",
    "front": "XZ",
    "left":  "YZ",
    "right": "YZ",  # backward compatibility for old feature JSON
}


def _outline_complexity(outline: Outline) -> int:
    """Higher score = more interesting profile.

    A rectangle scores 4; a hexagon scores 6; any ARC adds +10 so curved
    outlines win against straight-edge polygons.
    """
    if outline is None:
        return -1
    score = 0
    for e in outline.edges:
        score += 1
        if e.get("kind") == "ARC":
            score += 10
    # rectangle = exactly 4 LINE edges with axis-aligned bbox
    if score == 4:
        return 4
    return score


def _looks_like_boundary_construction_circle(
    circle: DxfEntity,
    outline: Optional[Outline],
) -> bool:
    """Return True for circles that describe an outer tangent/reference line.

    Some mechanical drawings include a circle tangent to a hexagon or other
    polygon in the top view. It is useful drawing geometry, but treating it as
    a through-hole destroys the model. Real holes should sit clearly inside the
    material; a circle whose bbox touches opposite sides of the outline bbox is
    a boundary construction circle for our feature purposes.
    """
    if outline is None or circle.center is None or circle.radius is None:
        return False
    min_x, min_y, max_x, max_y = outline.bbox
    width = max_x - min_x
    height = max_y - min_y
    scale = max(width, height, float(circle.radius), 1.0)
    tol = max(scale * 0.01, 1e-3)
    cx, cy = circle.center
    radius = float(circle.radius)
    touches_x = abs((cx - radius) - min_x) <= tol and abs((cx + radius) - max_x) <= tol
    touches_y = abs((cy - radius) - min_y) <= tol and abs((cy + radius) - max_y) <= tol
    return touches_x or touches_y


def _filter_hole_circles(
    outline: Optional[Outline],
    circles: List[DxfEntity],
) -> List[DxfEntity]:
    return [
        circle for circle in circles
        if not _looks_like_boundary_construction_circle(circle, outline)
    ]


def _boundary_construction_circles(
    outline: Optional[Outline],
    circles: List[DxfEntity],
) -> List[DxfEntity]:
    return [
        circle for circle in circles
        if _looks_like_boundary_construction_circle(circle, outline)
    ]


def _is_polygonal_prismatic_profile(outline: Optional[Outline]) -> bool:
    if outline is None:
        return False
    if len(outline.edges) < 5:
        return False
    return all(edge.get("kind") == "LINE" for edge in outline.edges)


def _infer_end_chamfer_distance(
    projected: Dict[str, ProjectedView],
    height: float,
) -> Optional[float]:
    """Infer top/bottom chamfer distance from FRONT/LEFT side views.

    In this nut drawing, the side views keep short vertical side segments
    inset from z=0 and z=H and connect them to the end faces with arcs. The
    inset gives a stable radius for a FreeCAD fillet on the horizontal outer
    edges of the vertical prism.
    """
    if height <= 0:
        return None
    candidates: List[float] = []
    for view_name in ("front", "left"):
        pv = projected.get(view_name)
        if pv is None:
            continue
        for entity in pv.entities:
            if entity.kind != "LINE" or len(entity.points) < 2:
                continue
            for _x, z in entity.points[:2]:
                z = float(z)
                if 1e-6 < z < height * 0.45:
                    candidates.append(z)
                upper = height - z
                if 1e-6 < upper < height * 0.45:
                    candidates.append(upper)
    if not candidates:
        return None
    distance = min(candidates)
    if distance <= 0 or distance >= height * 0.45:
        return None
    return distance


def _candidate_score(outline: Optional[Outline], holes: List[DxfEntity]) -> int:
    score = _outline_complexity(outline)
    if score <= 0:
        return score
    return score + 5 * len(holes)


def _make_outline(pv: ProjectedView) -> Tuple[Optional[Outline], List[DxfEntity]]:
    bundle = ViewBundle(name=pv.name,
                        bbox=(0.0, 0.0, pv.width, pv.height),
                        entities=pv.entities)
    return extract_outline_and_holes(bundle, hidden_pred=_is_hidden_entity)


def _visible_circles(pv: Optional[ProjectedView]) -> List[DxfEntity]:
    if pv is None:
        return []
    return [
        e for e in pv.entities
        if e.kind == "CIRCLE" and e.center is not None and e.radius is not None
        and not _is_hidden_entity(e)
    ]


def _infer_sphere_feature(
    projected: Dict[str, ProjectedView],
    width: float,
    depth: float,
    height: float,
) -> Optional[Feature]:
    """Infer a sphere from three linked circular orthographic projections.

    A through-hole usually appears as a circle in one view and hidden/parallel
    evidence in the other views. A sphere projects to a same-radius circle in
    all three canonical views, and the centers must agree under the fixed
    TOP/FRONT/LEFT coordinate mapping:
        TOP   circle center -> (X, Y)
        FRONT circle center -> (X, Z)
        LEFT circle center -> (Y, Z)
    """
    required = {name: _visible_circles(projected.get(name))
                for name in ("top", "front", "left")}
    if any(len(circles) != 1 for circles in required.values()):
        return None
    if any(
        any(e.kind != "CIRCLE" for e in projected[name].entities)
        for name in ("top", "front", "left")
    ):
        return None

    top = required["top"][0]
    front = required["front"][0]
    left = required["left"][0]
    assert top.center and front.center and left.center
    radius = float(top.radius or 0.0)
    if radius <= 0:
        return None
    scale = max(width, depth, height, radius * 2.0, 1.0)
    tol = max(scale * 0.03, 1e-3)
    for circle in (front, left):
        if abs(float(circle.radius or 0.0) - radius) > tol:
            return None
    if any(abs(dim - 2.0 * radius) > tol for dim in (width, depth, height)):
        return None

    x_from_top, y_from_top = top.center
    x_from_front, z_from_front = front.center
    y_from_left, z_from_left = left.center
    if abs(float(x_from_top) - float(x_from_front)) > tol:
        return None
    if abs(float(y_from_top) - float(y_from_left)) > tol:
        return None
    if abs(float(z_from_front) - float(z_from_left)) > tol:
        return None

    return Feature(
        kind="sphere",
        params={
            "radius": radius,
            "center": [
                (float(x_from_top) + float(x_from_front)) * 0.5,
                (float(y_from_top) + float(y_from_left)) * 0.5,
                (float(z_from_front) + float(z_from_left)) * 0.5,
            ],
            "source_views": ["top", "front", "left"],
        },
    )


# ---------------------------------------------------------------------------
# Cross-view hidden-line validation
# ---------------------------------------------------------------------------

# Tokens whose presence in a layer name (case-insensitive) marks it as
# carrying hidden / dashed lines.
_HIDDEN_LAYER_TOKENS: frozenset = frozenset({
    "HIDDEN", "HIDE", "DASH", "DASHED", "PHANTOM",
    "VERDECKT", "MASQUE", "NASCOSTA", "虚线",
})
_HIDDEN_LAYER_EXACT_TOKENS: frozenset = frozenset({"HID"})

# Standard AutoCAD linetype names (group code 6) that represent dashed/hidden lines.
_HIDDEN_LINETYPE_TOKENS: frozenset = frozenset({
    "HIDDEN", "HIDDEN2", "HIDDENX2",
    "DASHED", "DASHED2", "DASHEDX2",
    "DASH", "PHANTOM", "PHANTOM2", "PHANTOMX2",
    "CENTER", "CENTER2", "CENTERX2",     # center lines sometimes mark holes
    "ACAD_ISO02W100", "ACAD_ISO03W100",  # ISO dashed / dash-dot
})


def _is_hidden_entity(e: DxfEntity) -> bool:
    """Return True if *e* carries hidden/dashed line style.

    Checks (in order):
    1. Entity-level linetype name (group code 6) against known tokens.
    2. Linetype *description* from the LTYPE table (stored in
       ``extra['linetype_desc']`` by the loader) — catches custom names
       such as ``JIS_02_1.2`` whose description reads ``HIDDEN01.25  _ _``.
    3. Layer name keywords — works when the author used named layers but
       did not set per-entity linetypes.
    """
    lt = (e.linetype or "").upper()
    if lt and lt not in ("BYLAYER", "BYBLOCK", "CONTINUOUS"):
        if any(tok in lt for tok in _HIDDEN_LINETYPE_TOKENS):
            return True
        # Fallback: check the human-readable description from the LTYPE table.
        desc = (e.extra.get("linetype_desc") or "").upper()
        if desc and any(tok in desc for tok in _HIDDEN_LINETYPE_TOKENS):
            return True
    return _is_hidden_layer(e.layer)


# Keep old name as alias so any external callers still work.
def _is_hidden_layer(layer: str) -> bool:
    upper = (layer or "").upper()
    if any(tok in upper for tok in _HIDDEN_LAYER_TOKENS):
        return True

    parts: List[str] = []
    cur: List[str] = []
    for ch in upper:
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                parts.append("".join(cur))
                cur = []
    if cur:
        parts.append("".join(cur))
    return any(part in _HIDDEN_LAYER_EXACT_TOKENS for part in parts)


def _bbox_range(e: DxfEntity, axis: str) -> Optional[Tuple[float, float]]:
    """Return (min, max) extent of *e* along 'x' or 'y', or None."""
    b = e.bbox()
    if b is None:
        return None
    return (b[0], b[2]) if axis == "x" else (b[1], b[3])


def _interval_overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 <= b1 and b0 <= a1


def _hidden_overlaps(
    pv: ProjectedView, axis: str, lo: float, hi: float
) -> Optional[bool]:
    """Search *pv* for a HIDDEN-layer entity whose bbox overlaps [lo, hi].

    Returns:
        True  – hidden entities exist AND at least one overlaps
        False – hidden entities exist but none overlap
        None  – no hidden-layer entities at all (layer not used in this view)
    """
    hidden = [e for e in pv.entities if _is_hidden_entity(e)]
    if not hidden:
        return None
    for e in hidden:
        rng = _bbox_range(e, axis)
        if rng and _interval_overlaps(lo, hi, rng[0], rng[1]):
            return True
    return False


def _hole_has_hidden_evidence(
    hole: Feature,
    projected: Dict[str, ProjectedView],
) -> bool:
    """Return True when cross-view hidden-line evidence supports this hole.

    For each of the two views that did *not* generate this hole, look for
    HIDDEN-layer geometry whose bounding box overlaps the expected projection
    of the hole cylinder on that view plane.

    Coordinate mapping (normalised 2-D view coords == world coords):

    axis Z (source=top):
      FRONT checks bbox-x ∈ [hx ± r]   (draw.x → world X)
    LEFT checks bbox-x ∈ [hy ± r]   (draw.x → world Y)

    axis Y (source=front):
      TOP   checks bbox-x ∈ [hx ± r]   (draw.x → world X)
    LEFT checks bbox-y ∈ [hz ± r]   (draw.y → world Z)

    axis X (source=left):
      TOP   checks bbox-y ∈ [hy ± r]   (draw.y → world Y)
      FRONT checks bbox-y ∈ [hz ± r]   (draw.y → world Z)

    Acceptance logic:
    - At least one non-source view with HIDDEN entities confirms → accept.
    - No non-source view uses the HIDDEN layer at all → accept by default
      (DXF omits hidden lines; cannot filter).
    - Every non-source view has HIDDEN entities but none confirm → reject.
    """
    p = hole.params
    axis = p.get("axis", "Z")
    hx, hy, hz = p.get("position", [0.0, 0.0, 0.0])
    r = float(p.get("radius", 1.0))
    tol = max(r * 0.15, 0.5)   # 15 % of radius, min 0.5 mm

    if axis == "Z":
        checks = [("front", "x", hx), ("left", "x", hy)]
    elif axis == "Y":
        checks = [("top",   "x", hx), ("left", "y", hz)]
    else:   # X
        checks = [("top",   "y", hy), ("front", "y", hz)]

    definitive: List[bool] = []
    for view_name, coord_axis, center in checks:
        pv = projected.get(view_name)
        if pv is None:
            continue
        result = _hidden_overlaps(pv, coord_axis,
                                  center - r - tol, center + r + tol)
        if result is not None:
            definitive.append(result)

    if not definitive:
        return True   # no hidden-layer data in other views; accept by default
    return any(definitive)


def infer_features(projected: Dict[str, ProjectedView],
                   bundles: Optional[List[ViewBundle]] = None,
                   single_view_extrude_depth: Optional[float] = None,
                   model_intent: str = "") -> List[Feature]:
    if not projected:
        return []

    if single_view_extrude_depth is not None and len(projected) == 1:
        return _infer_single_view_extrusion(projected, single_view_extrude_depth)

    width, depth, height = estimate_part_size(projected, bundles)
    features: List[Feature] = []

    radial_rod = _infer_radial_stepped_cylinder_assembly(
        projected, width, depth, height, model_intent)
    if radial_rod is not None:
        return radial_rod

    side_connected_tube = _infer_side_connected_tube_assembly(
        projected, width, depth, height, model_intent)
    if side_connected_tube is not None:
        return side_connected_tube

    linkage_plate = _infer_planar_linkage_plate(
        projected, width, depth, height, model_intent)
    if linkage_plate is not None:
        return linkage_plate

    additive = _infer_additive_components(projected, width, depth, height, model_intent)
    if additive is not None:
        return additive

    sphere = _infer_sphere_feature(projected, width, depth, height)
    if sphere is not None:
        return [sphere]

    stepped_cylinder = _infer_stepped_cylinder_profile(projected, width, depth, height)
    if stepped_cylinder is not None:
        return [stepped_cylinder]

    cylinder = _infer_top_cylindrical_profile(projected, width, depth, height)
    if cylinder is not None:
        return cylinder

    # -- 1. Pick the most informative outline as the extrusion profile.
    candidates = []  # list of (score, view_name, outline, holes)
    for view_name in ("top", "front", "left"):
        pv = projected.get(view_name)
        if pv is None:
            continue
        outline, holes = _make_outline(pv)
        holes = _filter_hole_circles(outline, holes)
        score = _candidate_score(outline, holes)
        if view_name == "top" and holes and _is_polygonal_prismatic_profile(outline):
            # Hex nuts and similar prismatic parts are best reconstructed by
            # extruding the top-view polygon and cutting the true center hole.
            score += 25
        candidates.append((score, view_name, outline, holes))

    chosen = None
    if candidates:
        # Prefer the highest-complexity outline; ties broken by preferred order
        # of (top, front, left) — top first because end-face profiles are
        # the most natural extrusion source for prismatic parts.
        order = {"top": 0, "front": 1, "left": 2, "right": 2}
        candidates.sort(key=lambda c: (-c[0], order.get(c[1], 99)))
        for sc, vn, ol, hs in candidates:
            if ol is not None and sc > 0:
                chosen = (vn, ol, hs)
                break

    profile_view: Optional[str] = None
    profile_outline: Optional[Outline] = None
    profile_holes: List[DxfEntity] = []
    if chosen is not None:
        profile_view, outline, profile_holes = chosen
        profile_outline = outline
        plane = _VIEW_PLANE[profile_view]
        if profile_view == "top":
            extrusion_depth = height
        elif profile_view == "front":
            extrusion_depth = depth
        else:  # left
            extrusion_depth = width
        circular_profile = _outline_as_circle(
            outline,
            max(max(outline.width, outline.height, 1e-6) * 0.01, 1e-6),
        )
        if circular_profile is not None:
            cx, cy, radius = circular_profile
            edges = [{
                "kind": "CIRCLE",
                "center": [float(cx), float(cy)],
                "radius": float(radius),
            }]
        else:
            edges = [_serialize_edge(e) for e in outline.edges]
        features.append(Feature(
            kind="extrude_profile",
            params={
                "plane": plane,
                "depth": extrusion_depth,
                "source_view": profile_view,
                "edges": edges,
                "bbox_2d": list(outline.bbox),
                **({"cylindrical_profile": True} if circular_profile is not None else {}),
            },
        ))
    else:
        features.append(Feature(
            kind="base_block",
            params={"width": width, "depth": depth, "height": height,
                    "origin": [0.0, 0.0, 0.0]},
        ))

    # -- 2. Collect hole candidates from circles, then validate cross-view.
    hole_candidates: List[Feature] = []
    for view_name, pv in projected.items():
        if view_name == profile_view:
            circles = profile_holes
        else:
            circles = [e for e in pv.entities if e.kind == "CIRCLE"]
        for ent in circles:
            if ent.center is None or ent.radius is None:
                continue
            cx, cy = ent.center
            hole = _circle_to_hole(view_name, cx, cy, ent.radius,
                                   width, depth, height)
            if hole is not None:
                _apply_hidden_hole_extent(hole, projected, width, depth, height)
                hole_candidates.append(hole)

    for hole in hole_candidates:
        if _hole_has_hidden_evidence(hole, projected):
            features.append(hole)

    features.extend(_infer_internal_profile_cuts(
        projected, profile_view, width, depth, height))

    if profile_view == "top":
        features.extend(_infer_side_taper_cuts(projected, width, depth))

    if profile_view == "top" and _is_polygonal_prismatic_profile(profile_outline):
        chamfer_distance = _infer_end_chamfer_distance(projected, height)
        if chamfer_distance is not None:
            top_radius = None
            top_pv = projected.get("top")
            if top_pv is not None:
                boundary_circles = _boundary_construction_circles(
                    profile_outline,
                    [e for e in top_pv.entities if e.kind == "CIRCLE"],
                )
                if boundary_circles:
                    top_radius = max(
                        float(circle.radius or 0.0)
                        for circle in boundary_circles
                    )
            features.append(Feature(
                kind="edge_chamfer",
                params={
                    "distance": chamfer_distance,
                    "profile": "arc_revolve" if top_radius else "arc",
                    "scope": "outer_z_edges",
                    "source_views": ["front", "left"],
                    **({"top_radius": top_radius} if top_radius else {}),
                },
            ))

    return features


def _infer_side_connected_tube_assembly(
    projected: Dict[str, ProjectedView],
    width: float,
    depth: float,
    height: float,
    model_intent: str = "",
) -> Optional[List[Feature]]:
    front = projected.get("front")
    if front is None:
        return None
    bundle = ViewBundle(
        name=front.name,
        bbox=(0.0, 0.0, front.width, front.height),
        entities=front.entities,
    )
    outlines, _circles = extract_closed_outlines_and_circles(
        bundle, hidden_pred=_is_hidden_entity)
    if len(outlines) < 5:
        return None
    scale = max(width, depth, height, front.width, front.height, 1.0)
    tol = max(scale * 0.01, 1e-6)
    significant = [
        outline for outline in outlines
        if _outline_area(outline) > max(front.width * front.height * 0.01, tol * tol)
    ]
    if len(significant) < 5:
        return None
    inners = _nested_inner_outlines(significant, tol)
    inner_ids = {id(outline) for outline in inners}
    outer_outlines = [outline for outline in significant if id(outline) not in inner_ids]

    outer_circles: List[Tuple[Tuple[float, float, float], Outline]] = []
    for outline in outer_outlines:
        circular = _outline_as_circle(outline, tol)
        if circular is not None:
            outer_circles.append((circular, outline))
    if len(outer_circles) < 2:
        return None
    outer_circles.sort(key=lambda item: item[0][2], reverse=True)
    main_circle, main_outline = outer_circles[0]
    secondary_circle, secondary_outline = outer_circles[1]
    main_cx, main_z, main_radius = main_circle
    side_cx, side_z, side_radius = secondary_circle
    if main_radius <= tol or side_radius <= tol or side_radius >= main_radius * 0.8:
        return None

    outer_profiles = [
        outline for outline in outer_outlines
        if _outline_as_circle(outline, tol) is None
        and len(outline.edges) >= 12
        and _outline_area(outline) > max(_outline_area(secondary_outline) * 0.8, tol * tol)
    ]
    if not outer_profiles:
        return None
    slot_outline = max(outer_profiles, key=lambda outline: outline.bbox[2])
    sx0, sz0, sx1, sz1 = slot_outline.bbox
    if sx1 <= main_cx or sx0 <= main_cx + main_radius * 0.2:
        return None

    top = projected.get("top")
    full_span = (0.0, float(depth))
    lug_span = _top_component_span(top, (float(side_cx - side_radius), float(side_cx + side_radius)), depth, prefer="wide")
    slot_span = _top_component_span(top, (float(sx0), float(sx1)), depth, prefer="wide")
    arm_span = _top_component_span(top, (float(main_cx), float(sx0)), depth, prefer="narrow")
    left_arm_span = _top_component_span(top, (float(side_cx + side_radius), float(main_cx - main_radius * 0.2)), depth, prefer="narrow")
    if lug_span is None:
        lug_span = _centered_span(depth, depth / 3.0)
    if slot_span is None:
        slot_span = lug_span
    if arm_span is None:
        arm_span = _centered_span(depth, depth / 4.5)
    if left_arm_span is None:
        left_arm_span = arm_span

    features: List[Feature] = []

    def add_extrude(edges: List[Dict], bbox: Tuple[float, float, float, float],
                    reason: str, span: Tuple[float, float]) -> None:
        y0, y1 = span
        features.append(Feature(kind="extrude_profile", params={
            "plane": "XZ",
            "depth": float(max(y1 - y0, 0.0)),
            "offset": float(y0),
            "source_view": "front",
            "edges": edges,
            "bbox_2d": list(bbox),
            "depth_span": [float(y0), float(y1)],
            "additive_component": True,
            "reason": reason,
        }))

    add_extrude([{
        "kind": "CIRCLE",
        "center": [float(main_cx), float(main_z)],
        "radius": float(main_radius),
    }], tuple(main_outline.bbox), "side_connected_main_through_tube", full_span)
    add_extrude([{
        "kind": "CIRCLE",
        "center": [float(side_cx), float(side_z)],
        "radius": float(side_radius),
    }], tuple(secondary_outline.bbox), "side_connected_round_end", lug_span)
    add_extrude(_outline_edges_for_feature(slot_outline, tol),
                tuple(slot_outline.bbox), "side_connected_slotted_end", slot_span)

    left_connector = _front_diagonal_connector(front, tol)
    if left_connector is None:
        left_connector = _external_tangent_connector(
            (float(side_cx), float(side_z)), float(side_radius),
            (float(main_cx), float(main_z)), float(main_radius),
            tol,
        )
    if left_connector is not None:
        add_extrude(left_connector, _edge_bbox(left_connector),
                    "side_connected_left_arm", left_arm_span)
    right_connector = _front_horizontal_connector(front, float(sx0), tol)
    if right_connector is None:
        right_connector = _horizontal_connector(
            float(main_cx), float(sx0), float(main_z), float(main_radius), tol)
    if right_connector is not None:
        add_extrude(right_connector, _edge_bbox(right_connector),
                    "side_connected_right_arm", arm_span)

    for inner in inners:
        circular = _outline_as_circle(inner, tol)
        if circular is not None:
            cx, z, radius = circular
            if radius <= tol:
                continue
            hole_span = lug_span if cx < main_cx - main_radius else full_span
            features.append(Feature(kind="hole", params={
                "radius": float(radius),
                "diameter": float(radius) * 2.0,
                "axis": "Y",
                "position": [float(cx), float(hole_span[0]), float(z)],
                "through_length": float(hole_span[1] - hole_span[0]),
                "depth_span": [float(hole_span[0]), float(hole_span[1])],
                "source_view": "front",
                "reason": "side_connected_tube_circular_opening",
            }))
        else:
            if min(inner.width, inner.height) <= tol:
                continue
            cut_span = slot_span if inner.bbox[0] > main_cx else full_span
            features.append(Feature(kind="profile_cut", params={
                "plane": "XZ",
                "depth": float(cut_span[1] - cut_span[0]),
                "offset": float(cut_span[0]),
                "source_view": "front",
                "edges": _outline_edges_for_feature(inner, tol),
                "bbox_2d": list(inner.bbox),
                "depth_span": [float(cut_span[0]), float(cut_span[1])],
                "internal_profile_cut": True,
                "reason": "side_connected_tube_slotted_opening",
            }))

    if len(features) < 6:
        return None
    return features


def _top_component_span(
    top: Optional[ProjectedView],
    x_range: Tuple[float, float],
    fallback_depth: float,
    prefer: str = "narrow",
) -> Optional[Tuple[float, float]]:
    if top is None:
        return None
    x0, x1 = x_range
    scale = max(top.width, top.height, fallback_depth, 1.0)
    tol = max(scale * 0.01, 1e-6)
    spans: List[Tuple[float, float, float]] = []
    for entity in top.entities:
        if entity.kind != "LINE" or len(entity.points) < 2 or _is_hidden_entity(entity):
            continue
        (ex0, ey0), (ex1, ey1) = entity.points[:2]
        ex0 = float(ex0)
        ex1 = float(ex1)
        ey0 = float(ey0)
        ey1 = float(ey1)
        if abs(ey0 - ey1) > tol * 0.25:
            continue
        lx = min(ex0, ex1)
        rx = max(ex0, ex1)
        overlap = max(0.0, min(rx, x1) - max(lx, x0))
        target = max(x1 - x0, tol)
        if overlap < min(target * 0.35, rx - lx) - tol:
            continue
        spans.append((ey0, lx, rx))
    if len(spans) < 2:
        return None
    candidates: List[Tuple[float, float, float]] = []
    for idx, lower in enumerate(spans):
        for upper in spans[idx + 1:]:
            y0, lx0, rx0 = lower
            y1, lx1, rx1 = upper
            lo, hi = sorted((y0, y1))
            thickness = hi - lo
            if thickness <= tol or thickness > fallback_depth + tol:
                continue
            x_overlap = max(0.0, min(rx0, rx1, x1) - max(lx0, lx1, x0))
            if x_overlap <= tol:
                continue
            candidates.append((thickness, lo, hi))
    if not candidates:
        return None
    if prefer == "wide":
        _thickness, lo, hi = max(candidates, key=lambda item: item[0])
    else:
        usable = [item for item in candidates if item[0] <= fallback_depth * 0.4 + tol]
        if not usable:
            usable = candidates
        _thickness, lo, hi = min(usable, key=lambda item: item[0])
    return max(0.0, lo), min(float(fallback_depth), hi)


def _centered_span(depth: float, thickness: float) -> Tuple[float, float]:
    thickness = max(0.0, min(float(thickness), float(depth)))
    start = (float(depth) - thickness) * 0.5
    return start, start + thickness


def _external_tangent_connector(
    start_center: Tuple[float, float],
    start_radius: float,
    end_center: Tuple[float, float],
    end_radius: float,
    tol: float,
) -> Optional[List[Dict]]:
    sx, sz = start_center
    ex, ez = end_center
    dx = ex - sx
    dz = ez - sz
    d2 = dx * dx + dz * dz
    if d2 <= tol * tol:
        return None
    dr = start_radius - end_radius
    h2 = d2 - dr * dr
    if h2 <= tol * tol:
        return None
    root = math.sqrt(h2)
    tangents: List[Tuple[List[float], List[float]]] = []
    for sign in (-1.0, 1.0):
        vx = (dx * dr - dz * root * sign) / d2
        vz = (dz * dr + dx * root * sign) / d2
        p_start = [sx + vx * start_radius, sz + vz * start_radius]
        p_end = [ex + vx * end_radius, ez + vz * end_radius]
        tangents.append((p_start, p_end))
    if len(tangents) != 2:
        return None
    tangents.sort(key=lambda item: item[0][1], reverse=True)
    upper, lower = tangents[0], tangents[1]
    return _polyline_edges([upper[0], upper[1], lower[1], lower[0]])


def _front_diagonal_connector(pv: ProjectedView, tol: float) -> Optional[List[Dict]]:
    candidates: List[Tuple[float, Tuple[float, float], Tuple[float, float]]] = []
    for entity in pv.entities:
        if entity.kind != "LINE" or len(entity.points) < 2 or _is_hidden_entity(entity):
            continue
        p0 = (float(entity.points[0][0]), float(entity.points[0][1]))
        p1 = (float(entity.points[1][0]), float(entity.points[1][1]))
        dx = p1[0] - p0[0]
        dz = p1[1] - p0[1]
        length = math.hypot(dx, dz)
        if length <= max(pv.width, pv.height) * 0.45:
            continue
        if abs(dx) <= tol or dz / dx >= -0.2:
            continue
        candidates.append((length, p0, p1))
    if len(candidates) < 2:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    lines = candidates[:2]
    left_points: List[Tuple[float, float]] = []
    right_points: List[Tuple[float, float]] = []
    for _length, p0, p1 in lines:
        if p0[0] <= p1[0]:
            left_points.append(p0)
            right_points.append(p1)
        else:
            left_points.append(p1)
            right_points.append(p0)
    if len(left_points) != 2 or len(right_points) != 2:
        return None
    left_points.sort(key=lambda point: point[1], reverse=True)
    right_points.sort(key=lambda point: point[1], reverse=True)
    return _polyline_edges([
        [left_points[0][0], left_points[0][1]],
        [right_points[0][0], right_points[0][1]],
        [right_points[1][0], right_points[1][1]],
        [left_points[1][0], left_points[1][1]],
    ])


def _front_horizontal_connector(
    pv: ProjectedView,
    slot_left_x: float,
    tol: float,
) -> Optional[List[Dict]]:
    lines: List[Tuple[float, float, float, float]] = []
    for entity in pv.entities:
        if entity.kind != "LINE" or len(entity.points) < 2 or _is_hidden_entity(entity):
            continue
        x0, z0 = (float(entity.points[0][0]), float(entity.points[0][1]))
        x1, z1 = (float(entity.points[1][0]), float(entity.points[1][1]))
        if abs(z0 - z1) > tol * 0.25:
            continue
        left_x = min(x0, x1)
        right_x = max(x0, x1)
        if right_x - left_x <= pv.width * 0.12:
            continue
        if abs(right_x - slot_left_x) > max(tol, pv.width * 0.01):
            continue
        lines.append((z0, left_x, right_x, z1))
    if len(lines) < 2:
        return None
    lines.sort(key=lambda item: item[0])
    bottom = lines[0]
    top = lines[-1]
    left_x = min(bottom[1], top[1])
    right_x = max(bottom[2], top[2])
    return _polyline_edges([
        [left_x, bottom[0]],
        [right_x, bottom[0]],
        [right_x, top[0]],
        [left_x, top[0]],
    ])


def _horizontal_connector(
    x0: float,
    x1: float,
    z: float,
    half_height: float,
    tol: float,
) -> Optional[List[Dict]]:
    if x1 - x0 <= tol or half_height <= tol:
        return None
    return _polyline_edges([
        [x0, z - half_height],
        [x1, z - half_height],
        [x1, z + half_height],
        [x0, z + half_height],
    ])


def _polyline_edges(points: List[List[float]]) -> List[Dict]:
    edges: List[Dict] = []
    for idx, p0 in enumerate(points):
        p1 = points[(idx + 1) % len(points)]
        edges.append({"kind": "LINE", "p0": [float(p0[0]), float(p0[1])],
                      "p1": [float(p1[0]), float(p1[1])]})
    return edges


def _edge_bbox(edges: List[Dict]) -> Tuple[float, float, float, float]:
    xs: List[float] = []
    ys: List[float] = []
    for edge in edges:
        for key in ("p0", "p1"):
            point = edge.get(key)
            if point is None:
                continue
            xs.append(float(point[0]))
            ys.append(float(point[1]))
    return min(xs), min(ys), max(xs), max(ys)


def _infer_planar_linkage_plate(
    projected: Dict[str, ProjectedView],
    width: float,
    depth: float,
    height: float,
    model_intent: str = "",
) -> Optional[List[Feature]]:
    text = (model_intent or "").lower()
    if not any(token in text for token in (
        "平面连杆", "连杆板", "摇臂", "摆臂", "曲柄连杆",
        "多孔连接板", "长圆孔连杆", "linkage", "rocker", "link plate",
    )):
        return None
    front = projected.get("front")
    if front is None:
        return None

    bundle = ViewBundle(
        name=front.name,
        bbox=(0.0, 0.0, front.width, front.height),
        entities=front.entities,
    )
    outlines, _circles = extract_closed_outlines_and_circles(
        bundle, hidden_pred=_is_hidden_entity)
    if not outlines:
        return None
    scale = max(width, depth, height, front.width, front.height, 1.0)
    tol = max(scale * 0.01, 1e-6)
    significant = [
        outline for outline in outlines
        if _outline_area(outline) > max(front.width * front.height * 0.001, tol * tol)
    ]
    if len(significant) < 3:
        return None

    bbox = _combined_outline_bbox(significant)
    features: List[Feature] = [Feature(kind="extrude_profile", params={
        "plane": "XZ",
        "depth": float(depth),
        "source_view": "front",
        "edges": _rectangle_edges(bbox),
        "bbox_2d": list(bbox),
        "reason": "planar_linkage_plate_bbox_rebuild",
    })]

    for inner in _nested_inner_outlines(significant, tol):
        circular = _outline_as_circle(inner, tol)
        if circular is not None:
            cx, z, radius = circular
            if radius <= tol:
                continue
            features.append(Feature(kind="hole", params={
                "radius": float(radius),
                "axis": "Y",
                "position": [float(cx), 0.0, float(z)],
                "through_length": float(depth),
                "source_view": "front",
                "reason": "planar_linkage_plate_internal_circle",
            }))
        else:
            if min(inner.width, inner.height) <= tol:
                continue
            features.append(Feature(kind="profile_cut", params={
                "plane": "XZ",
                "depth": float(depth),
                "offset": 0.0,
                "source_view": "front",
                "edges": [_serialize_edge(edge) for edge in inner.edges],
                "bbox_2d": list(inner.bbox),
                "internal_profile_cut": True,
                "reason": "planar_linkage_plate_internal_profile",
            }))

    return features


def _combined_outline_bbox(outlines: List[Outline]) -> Tuple[float, float, float, float]:
    return (
        min(float(outline.bbox[0]) for outline in outlines),
        min(float(outline.bbox[1]) for outline in outlines),
        max(float(outline.bbox[2]) for outline in outlines),
        max(float(outline.bbox[3]) for outline in outlines),
    )


def _rectangle_edges(bbox: Tuple[float, float, float, float]) -> List[Dict]:
    x0, y0, x1, y1 = [float(value) for value in bbox]
    return [
        {"kind": "LINE", "p0": [x0, y0], "p1": [x1, y0]},
        {"kind": "LINE", "p0": [x1, y0], "p1": [x1, y1]},
        {"kind": "LINE", "p0": [x1, y1], "p1": [x0, y1]},
        {"kind": "LINE", "p0": [x0, y1], "p1": [x0, y0]},
    ]


def _nested_inner_outlines(outlines: List[Outline], tol: float) -> List[Outline]:
    inners: List[Outline] = []
    for candidate in outlines:
        containers = [
            outline for outline in outlines
            if outline is not candidate and _outline_inside_outline(candidate, outline, tol)
        ]
        if not containers:
            continue
        container = min(containers, key=_outline_area)
        if _outline_area(candidate) >= _outline_area(container) * 0.9:
            continue
        inners.append(candidate)
    deduped: List[Outline] = []
    seen = set()
    for outline in sorted(inners, key=_outline_area, reverse=True):
        key = tuple(round(float(v) / tol) for v in outline.bbox) + (len(outline.edges),)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(outline)
    return deduped


def _infer_internal_profile_cuts(
    projected: Dict[str, ProjectedView],
    profile_view: Optional[str],
    width: float,
    depth: float,
    height: float,
) -> List[Feature]:
    axis_depth = {"top": height, "front": depth, "left": width, "right": width}
    cuts: List[Feature] = []
    seen: set = set()
    for view_name in ("front", "top", "left"):
        pv = projected.get(view_name)
        if pv is None:
            continue
        bundle = ViewBundle(
            name=pv.name,
            bbox=(0.0, 0.0, pv.width, pv.height),
            entities=pv.entities,
        )
        outlines, _circles = extract_closed_outlines_and_circles(
            bundle, hidden_pred=_is_hidden_entity)
        if len(outlines) < 2:
            continue
        outer = outlines[0]
        scale = max(pv.width, pv.height, 1e-6)
        tol = max(scale * 0.01, 1e-6)
        for outline in outlines[1:]:
            if not _outline_inside_outline(outline, outer, tol):
                continue
            if min(outline.width, outline.height) <= scale * 0.01:
                continue
            circular = _outline_as_circle(outline, tol)
            if circular is not None:
                cx, cy, radius = circular
                hole = _circle_to_hole(view_name, cx, cy, radius,
                                       width, depth, height)
                if hole is not None:
                    _apply_hidden_hole_extent(hole, projected, width, depth, height)
                    if _hole_has_hidden_evidence(hole, projected):
                        cuts.append(hole)
                    continue
            depth_value = float(axis_depth.get(view_name, 0.0) or 0.0)
            offset = 0.0
            span = None
            if not _is_visible_internal_top_rectangle(view_name, outline):
                span = _internal_cut_span_from_cross_view(
                    view_name, outline, projected, width, depth, height, tol)
                if span is None:
                    continue
            if span is not None:
                offset, end = span
                depth_value = max(0.0, end - offset)
            if depth_value <= 0:
                continue
            key = (
                view_name,
                tuple(round(float(v) / tol) for v in outline.bbox),
                len(outline.edges),
            )
            if key in seen:
                continue
            seen.add(key)
            cuts.append(Feature(
                kind="profile_cut",
                params={
                    "plane": _VIEW_PLANE[view_name],
                    "depth": depth_value,
                    "offset": offset,
                    "source_view": view_name,
                    "edges": [_serialize_edge(edge) for edge in outline.edges],
                    "bbox_2d": list(outline.bbox),
                    "internal_profile_cut": True,
                    "source_hidden": False,
                },
            ))
    return cuts


def _internal_cut_span_from_cross_view(
    view_name: str,
    outline: Outline,
    projected: Dict[str, ProjectedView],
    width: float,
    depth: float,
    height: float,
    tol: float,
) -> Optional[Tuple[float, float]]:
    if view_name == "top":
        front = projected.get("front")
        if front is None:
            return None
        bundle = ViewBundle(
            name=front.name,
            bbox=(0.0, 0.0, front.width, front.height),
            entities=front.entities,
        )
        outlines, _circles = extract_closed_outlines_and_circles(
            bundle, hidden_pred=_is_hidden_entity)
        candidates: List[Tuple[float, float]] = []
        ox0, _oy0, ox1, _oy1 = outline.bbox
        for candidate in outlines[1:]:
            circular = _outline_as_circle(candidate, tol)
            if circular is None:
                continue
            cx0, cz0, cx1, cz1 = candidate.bbox
            if _interval_overlaps(ox0, ox1, cx0, cx1):
                candidates.append((float(cz0), float(cz1)))
        if not candidates:
            return None
        return min(span[0] for span in candidates), max(span[1] for span in candidates)
    return None


def _outline_inside_outline(inner: Outline, outer: Outline, tol: float) -> bool:
    ix0, iy0, ix1, iy1 = inner.bbox
    ox0, oy0, ox1, oy1 = outer.bbox
    return (
        ix0 >= ox0 + tol
        and iy0 >= oy0 + tol
        and ix1 <= ox1 - tol
        and iy1 <= oy1 - tol
    )


def _is_visible_internal_top_rectangle(view_name: str, outline: Outline) -> bool:
    if view_name != "top" or len(outline.edges) != 4:
        return False
    for edge in outline.edges:
        if edge.get("kind") != "LINE":
            return False
        x0, y0 = edge["p0"]
        x1, y1 = edge["p1"]
        if abs(float(x0) - float(x1)) > 1e-6 and abs(float(y0) - float(y1)) > 1e-6:
            return False
    return True


def _outline_as_circle(outline: Outline, tol: float) -> Optional[Tuple[float, float, float]]:
    if len(outline.edges) < 12:
        return None
    min_x, min_y, max_x, max_y = outline.bbox
    width = max_x - min_x
    height = max_y - min_y
    radius = (width + height) * 0.25
    if radius <= tol:
        return None
    if abs(width - height) > max(radius * 0.08, tol * 2.0):
        return None
    cx = (min_x + max_x) * 0.5
    cy = (min_y + max_y) * 0.5
    samples: List[Tuple[float, float]] = []
    for edge in outline.edges:
        samples.append((float(edge["p0"][0]), float(edge["p0"][1])))
        samples.append((float(edge["p1"][0]), float(edge["p1"][1])))
    if not samples:
        return None
    max_error = max(abs(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 - radius)
                    for x, y in samples)
    if max_error > max(radius * 0.06, tol * 3.0):
        return None
    return float(cx), float(cy), float(radius)


def _outline_edges_for_feature(outline: Outline, tol: float) -> List[Dict]:
    circular = _outline_as_circle(outline, tol)
    if circular is not None:
        cx, cy, radius = circular
        return [{
            "kind": "CIRCLE",
            "center": [float(cx), float(cy)],
            "radius": float(radius),
        }]
    slot = _outline_as_rounded_slot(outline, tol)
    if slot is not None:
        return slot
    return [_serialize_edge(edge) for edge in outline.edges]


def _outline_as_rounded_slot(outline: Outline, tol: float) -> Optional[List[Dict]]:
    if len(outline.edges) < 12:
        return None
    min_x, min_y, max_x, max_y = [float(v) for v in outline.bbox]
    width = max_x - min_x
    height = max_y - min_y
    if min(width, height) <= tol:
        return None
    aspect = max(width, height) / min(width, height)
    if aspect < 1.55:
        return None
    if width <= height:
        radius = width * 0.5
        cx = (min_x + max_x) * 0.5
        lower_cy = min_y + radius
        upper_cy = max_y - radius
        if upper_cy <= lower_cy + tol:
            return None
        return [
            {"kind": "LINE", "p0": [max_x, lower_cy], "p1": [max_x, upper_cy]},
            {
                "kind": "ARC",
                "center": [cx, upper_cy],
                "radius": radius,
                "start_angle": 0.0,
                "end_angle": 180.0,
                "p0": [max_x, upper_cy],
                "p1": [min_x, upper_cy],
            },
            {"kind": "LINE", "p0": [min_x, upper_cy], "p1": [min_x, lower_cy]},
            {
                "kind": "ARC",
                "center": [cx, lower_cy],
                "radius": radius,
                "start_angle": 180.0,
                "end_angle": 360.0,
                "p0": [min_x, lower_cy],
                "p1": [max_x, lower_cy],
            },
        ]

    radius = height * 0.5
    cy = (min_y + max_y) * 0.5
    left_cx = min_x + radius
    right_cx = max_x - radius
    if right_cx <= left_cx + tol:
        return None
    return [
        {"kind": "LINE", "p0": [left_cx, max_y], "p1": [right_cx, max_y]},
        {
            "kind": "ARC",
            "center": [right_cx, cy],
            "radius": radius,
            "start_angle": 90.0,
            "end_angle": 270.0,
            "clockwise": True,
            "p0": [right_cx, max_y],
            "p1": [right_cx, min_y],
        },
        {"kind": "LINE", "p0": [right_cx, min_y], "p1": [left_cx, min_y]},
        {
            "kind": "ARC",
            "center": [left_cx, cy],
            "radius": radius,
            "start_angle": 270.0,
            "end_angle": 90.0,
            "clockwise": True,
            "p0": [left_cx, min_y],
            "p1": [left_cx, max_y],
        },
    ]


def _infer_side_taper_cuts(
    projected: Dict[str, ProjectedView],
    width: float,
    depth: float,
) -> List[Feature]:
    """Infer triangular through-cuts from chamfered side-view corners.

    A top-view extrusion preserves a constant Z height. If a side view's outer
    silhouette is a rectangle with one or more diagonal corner clips, those
    clips are missing wedge cuts in the base model. Represent each clipped
    corner as a triangular profile_cut in the side-view plane and extrude it
    through the perpendicular direction.
    """
    cuts: List[Feature] = []
    specs = {
        "left": ("YZ", "X", width),
        "front": ("XZ", "Y", depth),
    }
    for view_name, (plane, axis, through_length) in specs.items():
        pv = projected.get(view_name)
        if pv is None or through_length <= 0:
            continue
        outline, _holes = _make_outline(pv)
        if outline is None:
            continue
        for triangle in _corner_clip_triangles(outline):
            cuts.append(Feature(
                kind="profile_cut",
                params={
                    "plane": plane,
                    "depth": float(through_length),
                    "source_view": view_name,
                    "edges": _triangle_edges(triangle),
                    "bbox_2d": list(_points_bbox(triangle)),
                    "axis": axis,
                    "through_length": float(through_length),
                    "reason": "side_view_corner_clip",
                },
            ))
    return cuts


def _corner_clip_triangles(outline: Outline) -> List[List[Tuple[float, float]]]:
    min_x, min_y, max_x, max_y = outline.bbox
    scale = max(max_x - min_x, max_y - min_y, 1.0)
    tol = max(scale * 0.01, 1e-3)
    corners = [
        (min_x, min_y, "left", "bottom"),
        (min_x, max_y, "left", "top"),
        (max_x, min_y, "right", "bottom"),
        (max_x, max_y, "right", "top"),
    ]
    triangles: List[List[Tuple[float, float]]] = []
    seen = set()
    for edge in outline.edges:
        if edge.get("kind") != "LINE":
            continue
        p0 = tuple(float(v) for v in edge.get("p0", []))
        p1 = tuple(float(v) for v in edge.get("p1", []))
        if len(p0) != 2 or len(p1) != 2:
            continue
        if abs(p0[0] - p1[0]) <= tol or abs(p0[1] - p1[1]) <= tol:
            continue
        for cx, cy, x_side, y_side in corners:
            on_x_side = [p for p in (p0, p1) if _near(p[0], cx, tol)]
            on_y_side = [p for p in (p0, p1) if _near(p[1], cy, tol)]
            if len(on_x_side) != 1 or len(on_y_side) != 1 or on_x_side[0] == on_y_side[0]:
                continue
            x_point = on_x_side[0]
            y_point = on_y_side[0]
            if not _point_between_bbox(x_point, outline.bbox, tol):
                continue
            if not _point_between_bbox(y_point, outline.bbox, tol):
                continue
            x_inset = abs(float(y_point[0]) - float(cx))
            y_inset = abs(float(x_point[1]) - float(cy))
            if x_inset <= tol or y_inset <= tol:
                continue
            if x_inset > scale * 0.35 or y_inset > scale * 0.35:
                continue
            key = (round(cx, 6), round(cy, 6), round(x_point[0], 6), round(x_point[1], 6), round(y_point[0], 6), round(y_point[1], 6))
            if key in seen:
                continue
            seen.add(key)
            if x_side == "left":
                corner_x = min_x
            else:
                corner_x = max_x
            if y_side == "bottom":
                corner_y = min_y
            else:
                corner_y = max_y
            triangles.append([(corner_x, corner_y), x_point, y_point])
    return triangles


def _triangle_edges(points: List[Tuple[float, float]]) -> List[Dict]:
    return [
        {"kind": "LINE", "p0": list(points[i]), "p1": list(points[(i + 1) % 3])}
        for i in range(3)
    ]


def _points_bbox(points: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _near(a: float, b: float, tol: float) -> bool:
    return abs(float(a) - float(b)) <= tol


def _point_between_bbox(point: Tuple[float, float], bbox: Tuple[float, float, float, float], tol: float) -> bool:
    min_x, min_y, max_x, max_y = bbox
    x, y = point
    return min_x - tol <= x <= max_x + tol and min_y - tol <= y <= max_y + tol


def _infer_stepped_cylinder_profile(
    projected: Dict[str, ProjectedView],
    width: float,
    depth: float,
    height: float,
) -> Optional[Feature]:
    top = projected.get("top")
    front = projected.get("front")
    left = projected.get("left") or projected.get("right")
    if top is None or front is None or left is None:
        return None
    top_circles = [
        entity for entity in top.entities
        if entity.kind == "CIRCLE" and entity.center is not None and entity.radius is not None
    ]
    if not top_circles:
        return None
    outer = max(top_circles, key=lambda circle: float(circle.radius or 0.0))
    if outer.center is None or outer.radius is None:
        return None

    front_bands = _stepped_side_bands(front)
    left_bands = _stepped_side_bands(left)
    if len(front_bands) < 2 or len(front_bands) != len(left_bands):
        return None

    scale = max(width, depth, height, float(outer.radius) * 2.0, 1.0)
    tol = max(scale * 0.03, 1e-3)
    segments: List[Dict] = []
    for front_band, left_band in zip(front_bands, left_bands):
        z0, z1, cx, radius_x = front_band
        rz0, rz1, cy, radius_y = left_band
        if abs(z0 - rz0) > tol or abs(z1 - rz1) > tol:
            return None
        if abs(radius_x - radius_y) > tol:
            return None
        segments.append({
            "z_min": float((z0 + rz0) * 0.5),
            "z_max": float((z1 + rz1) * 0.5),
            "radius": float((radius_x + radius_y) * 0.5),
        })

    max_radius = max(segment["radius"] for segment in segments)
    if abs(max_radius - float(outer.radius)) > tol:
        return None
    if max(segment["z_max"] for segment in segments) - min(segment["z_min"] for segment in segments) <= tol:
        return None
    if len({round(segment["radius"] / tol) for segment in segments}) < 2:
        return None
    if not _top_circles_support_radii(top_circles, [s["radius"] for s in segments], tol):
        return None

    top_cx, top_cy = outer.center
    center_x = sum(band[2] for band in front_bands) / len(front_bands)
    center_y = sum(band[2] for band in left_bands) / len(left_bands)
    if abs(float(top_cx) - center_x) > tol or abs(float(top_cy) - center_y) > tol:
        return None

    return Feature(
        kind="cylinder_stack",
        params={
            "axis": "Z",
            "center": [float(center_x), float(center_y)],
            "segments": segments,
            "source_views": ["top", "front", "left"],
        },
    )


def _stepped_side_bands(pv: ProjectedView) -> List[Tuple[float, float, float, float]]:
    edges = _visible_line_segments_2d(pv)
    if not edges:
        return []
    scale = max(pv.width, pv.height, 1.0)
    tol = max(scale * 0.01, 1e-3)
    z_values: List[float] = []
    verticals: List[Tuple[float, float, float]] = []
    for (x0, z0), (x1, z1) in edges:
        if abs(z0 - z1) <= tol and abs(x0 - x1) > tol:
            z_values.append((z0 + z1) * 0.5)
        elif abs(x0 - x1) <= tol and abs(z0 - z1) > tol:
            verticals.append(((x0 + x1) * 0.5, min(z0, z1), max(z0, z1)))
    levels = _cluster_values(z_values, tol)
    if len(levels) < 2:
        return []
    bands: List[Tuple[float, float, float, float]] = []
    for z0, z1 in zip(levels, levels[1:]):
        if z1 - z0 <= tol:
            continue
        mid = (z0 + z1) * 0.5
        xs = [x for x, lo, hi in verticals if lo <= mid + tol and hi >= mid - tol]
        xs = _cluster_values(xs, tol)
        if len(xs) < 2:
            continue
        x_min = min(xs)
        x_max = max(xs)
        radius = (x_max - x_min) * 0.5
        if radius <= tol:
            continue
        bands.append((float(z0), float(z1), float((x_min + x_max) * 0.5), float(radius)))
    return bands


def _visible_line_segments_2d(pv: ProjectedView) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    for entity in pv.entities:
        if _is_hidden_entity(entity):
            continue
        if entity.kind == "LINE" and len(entity.points) >= 2:
            segments.append((tuple(entity.points[0]), tuple(entity.points[1])))
        elif entity.kind in ("LWPOLYLINE", "POLYLINE") and len(entity.points) >= 2:
            points = entity.points
            closed = bool(entity.extra.get("closed", False))
            end = len(points) if closed else len(points) - 1
            for idx in range(end):
                segments.append((tuple(points[idx]), tuple(points[(idx + 1) % len(points)])))
    return segments


def _cluster_values(values: List[float], tol: float) -> List[float]:
    if not values:
        return []
    values = sorted(float(value) for value in values)
    clusters: List[List[float]] = [[values[0]]]
    for value in values[1:]:
        if abs(value - clusters[-1][-1]) <= tol:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [sum(cluster) / len(cluster) for cluster in clusters]


def _top_circles_support_radii(
    circles: List[DxfEntity],
    radii: List[float],
    tol: float,
) -> bool:
    circle_radii = [float(circle.radius or 0.0) for circle in circles]
    unique_radii = _cluster_values(radii, tol)
    for radius in unique_radii:
        if not any(abs(radius - circle_radius) <= tol for circle_radius in circle_radii):
            return False
    return True


def _infer_top_cylindrical_profile(
    projected: Dict[str, ProjectedView],
    width: float,
    depth: float,
    height: float,
) -> Optional[List[Feature]]:
    top = projected.get("top")
    if top is None:
        return None
    visible = [e for e in top.entities if not _is_hidden_entity(e)]
    circles = _visible_circles(top)
    if not circles or len(circles) != len(visible):
        return None
    outer = max(circles, key=lambda circle: float(circle.radius or 0.0))
    if outer.center is None or outer.radius is None:
        return None
    cx, cy = outer.center
    outer_radius = float(outer.radius)
    scale = max(width, depth, height, outer_radius * 2.0, 1.0)
    tol = max(scale * 0.03, 1e-3)
    if abs(width - 2.0 * outer_radius) > tol:
        return None
    if abs(depth - 2.0 * outer_radius) > tol:
        return None

    features: List[Feature] = [Feature(
        kind="extrude_profile",
        params={
            "plane": "XY",
            "depth": float(height),
            "source_view": "top",
            "edges": [{
                "kind": "CIRCLE",
                "center": [float(cx), float(cy)],
                "radius": outer_radius,
            }],
            "bbox_2d": [
                float(cx) - outer_radius,
                float(cy) - outer_radius,
                float(cx) + outer_radius,
                float(cy) + outer_radius,
            ],
            "cylindrical_profile": True,
        },
    )]

    for circle in circles:
        if circle is outer or circle.center is None or circle.radius is None:
            continue
        inner_cx, inner_cy = circle.center
        if abs(float(inner_cx) - float(cx)) > tol or abs(float(inner_cy) - float(cy)) > tol:
            continue
        radius = float(circle.radius)
        if radius >= outer_radius - tol:
            continue
        hole = _circle_to_hole("top", float(inner_cx), float(inner_cy), radius,
                               width, depth, height)
        if hole is None:
            continue
        _apply_hidden_hole_extent(hole, projected, width, depth, height)
        if _hole_has_hidden_evidence(hole, projected):
            features.append(hole)

    return features


def _infer_additive_components(
    projected: Dict[str, ProjectedView],
    width: float,
    depth: float,
    height: float,
    model_intent: str = "",
) -> Optional[List[Feature]]:
    """Infer a fused multi-body model from several strong closed outlines.

    This path is intentionally conservative and is mainly enabled by natural
    language intent such as "组合", "多个物体", "连接", or "相接".  It expresses
    each detected body as a normal ``extrude_profile`` so the builder can fuse
    them without introducing a one-off feature type.
    """
    if not _intent_requests_additive_components(model_intent):
        return None
    front = projected.get("front")
    top = projected.get("top")
    left = projected.get("left") or projected.get("right")
    if front is None or top is None:
        return None

    front_outlines = _component_candidate_outlines(front)
    top_outlines = _unique_significant_outlines(top)
    if not front_outlines or not top_outlines:
        return None

    scale = max(width, depth, height, 1.0)
    tol = max(scale * 0.015, 1e-4)
    features: List[Feature] = []
    seen: set = set()

    def add_feature(plane: str, source_view: str, outline: Outline,
                    extrude_depth: float, offset: float = 0.0,
                    reason: str = "additive_component") -> None:
        if extrude_depth <= tol:
            return
        circular = _outline_as_circle(outline, tol)
        if circular is not None:
            cx, cy, radius = circular
            edges = [{
                "kind": "CIRCLE",
                "center": [float(cx), float(cy)],
                "radius": float(radius),
            }]
        else:
            edges = [_serialize_edge(edge) for edge in outline.edges]
        key = (
            plane,
            source_view,
            tuple(round(float(v) / tol) for v in outline.bbox),
            round(float(offset) / tol),
            round(float(extrude_depth) / tol),
        )
        if key in seen:
            return
        seen.add(key)
        features.append(Feature(
            kind="extrude_profile",
            params={
                "plane": plane,
                "depth": float(extrude_depth),
                "offset": float(offset),
                "source_view": source_view,
                "edges": edges,
                "bbox_2d": list(outline.bbox),
                "additive_component": True,
                "reason": reason,
            },
        ))

    # Components whose decisive profile is visible in FRONT: e.g. a hexagonal
    # prism or a horizontal cylinder seen as a circle in FRONT.  TOP supplies
    # the Y offset/span of that component.
    accepted_front_bboxes: List[Tuple[float, float, float, float]] = []
    for outline in front_outlines:
        if _outline_area(outline) < max(front.width * front.height * 0.04, tol * tol):
            continue
        circular = _outline_as_circle(outline, tol)
        if circular is not None:
            span = _best_top_y_span_for_x(top_outlines, outline.bbox, tol,
                                          prefer_high=True, full_span=top.height)
            if span is None:
                continue
            y0, y1 = span
            add_feature("XZ", "front", outline, y1 - y0, y0, "front_round_component")
            accepted_front_bboxes.append(outline.bbox)
            continue
        if _is_axis_aligned_rectangle_outline(outline, tol):
            continue
        if outline.width < width * 0.35 or outline.height < height * 0.25:
            continue
        if _bbox_is_redundant_component_candidate(outline.bbox, accepted_front_bboxes, tol):
            continue
        span = _best_top_y_span_for_x(top_outlines, outline.bbox, tol,
                          prefer_high=False, full_span=top.height)
        if span is None:
            continue
        y0, y1 = span
        add_feature("XZ", "front", outline, y1 - y0, y0, "front_polygon_component")
        accepted_front_bboxes.append(outline.bbox)

    # Components whose decisive footprint is circular in TOP: e.g. a vertical
    # small cylinder.  FRONT supplies its Z offset/span when a matching
    # rectangular side silhouette exists.
    for outline in top_outlines:
        circular = _outline_as_circle(outline, tol)
        if circular is None:
            continue
        z_span = _best_front_z_span_for_x(front_outlines, outline.bbox, tol)
        if z_span is None and left is not None:
            z_span = _best_front_z_span_for_x(_unique_significant_outlines(left), outline.bbox, tol)
        if z_span is None:
            continue
        z0, z1 = z_span
        if z1 - z0 >= height * 0.98:
            continue
        add_feature("XY", "top", outline, z1 - z0, z0, "top_round_component")

    if len(features) < 2:
        return None
    return features


def _intent_requests_additive_components(model_intent: str) -> bool:
    text = (model_intent or "").lower()
    if not text.strip():
        return False
    tokens = (
        "组合", "多个", "多体", "物体", "部件", "连接", "相接", "拼接",
        "component", "components", "multi", "assembly", "fuse", "joined",
    )
    return any(token in text for token in tokens)


def _infer_radial_stepped_cylinder_assembly(
    projected: Dict[str, ProjectedView],
    width: float,
    depth: float,
    height: float,
    model_intent: str = "",
) -> Optional[List[Feature]]:
    text = (model_intent or "").lower()
    if not any(token in text for token in (
        "圆柱杆", "较粗", "较细", "粗", "细", "垂直于轴线",
        "阶梯圆柱杆", "偏心侧向", "偏心径向", "径向", "竖直", "立式",
        "侧向杆", "支管", "凸台",
        "radial", "stepped", "rod", "branch", "boss", "vertical",
    )):
        return None
    front = projected.get("front")
    top = projected.get("top")
    left = projected.get("left") or projected.get("right")
    if front is None or top is None or left is None:
        return None

    scale = max(width, depth, height, 1.0)
    tol = max(scale * 0.015, 1e-4)
    front_circles = [
        (_outline_as_circle(outline, tol), outline)
        for outline in _component_candidate_outlines(front)
    ]
    front_circles = [(circle, outline) for circle, outline in front_circles if circle is not None]
    if not front_circles:
        return None
    main_circle, _main_outline = max(front_circles, key=lambda item: item[0][2])
    main_cx, main_z, main_radius = main_circle
    if main_radius < max(width, height) * 0.18:
        return None

    top_circles = [
        (_outline_as_circle(outline, tol), outline)
        for outline in _unique_significant_outlines(top)
    ]
    top_circles = [(circle, outline) for circle, outline in top_circles if circle is not None]
    if not top_circles:
        return None
    rod_circle, _rod_outline = max(top_circles, key=lambda item: item[0][2])
    rod_x, rod_y, coarse_radius = rod_circle
    if coarse_radius <= tol or coarse_radius >= main_radius * 0.65:
        return None

    side_branch_tokens = (
        "沿x", "x向", "x 方向", "x方向", "侧向接管", "支管", "横向伸出",
        "side branch", "branch pipe", "x-axis", "x axis",
    )
    vertical_rod_tokens = (
        "径向", "竖直", "立式", "向上", "偏心径向", "vertical",
    )
    if (any(token in text for token in side_branch_tokens)
            and not any(token in text for token in vertical_rod_tokens)):
        side_features = _infer_left_side_stepped_rod_features(
            projected, main_cx, main_z, main_radius, depth,
            rod_x, rod_y, coarse_radius, tol)
        if side_features is not None:
            return side_features

    main_top = main_z + main_radius
    fine_span_front = _fine_rod_span_from_view(front, rod_x, coarse_radius, main_top, tol)
    fine_span_left = _fine_rod_span_from_view(left, rod_y, coarse_radius, main_top, tol)
    if fine_span_front is None and fine_span_left is None:
        return None
    spans = [span for span in (fine_span_front, fine_span_left) if span is not None]
    fine_z0 = min(span[1] for span in spans)
    fine_z1 = max(span[2] for span in spans)
    fine_radius_values = [span[0] for span in spans if span[0] > tol]
    if not fine_radius_values or fine_z1 - fine_z0 <= tol:
        return None
    fine_radius = min(fine_radius_values)
    if fine_radius >= coarse_radius * 0.95:
        return None

    coarse_z0 = _coarse_rod_start_from_views(
        ((front, rod_x), (left, rod_y)), coarse_radius, fine_z0, main_top, tol)
    if coarse_z0 is None:
        coarse_z0 = max(0.0, main_top - coarse_radius * 1.25)
    coarse_z1 = fine_z0
    if coarse_z1 - coarse_z0 <= tol:
        return None

    return [
        Feature(kind="extrude_profile", params={
            "plane": "XZ",
            "depth": float(depth),
            "offset": 0.0,
            "source_view": "front",
            "edges": [{
                "kind": "CIRCLE",
                "center": [float(main_cx), float(main_z)],
                "radius": float(main_radius),
            }],
            "bbox_2d": [
                float(main_cx - main_radius), float(main_z - main_radius),
                float(main_cx + main_radius), float(main_z + main_radius),
            ],
            "additive_component": True,
            "reason": "main_horizontal_cylinder",
        }),
        Feature(kind="extrude_profile", params={
            "plane": "XY",
            "depth": float(coarse_z1 - coarse_z0),
            "offset": float(coarse_z0),
            "source_view": "top",
            "edges": [{
                "kind": "CIRCLE",
                "center": [float(rod_x), float(rod_y)],
                "radius": float(coarse_radius),
            }],
            "bbox_2d": [
                float(rod_x - coarse_radius), float(rod_y - coarse_radius),
                float(rod_x + coarse_radius), float(rod_y + coarse_radius),
            ],
            "additive_component": True,
            "reason": "radial_rod_large_segment",
        }),
        Feature(kind="extrude_profile", params={
            "plane": "XY",
            "depth": float(fine_z1 - fine_z0),
            "offset": float(fine_z0),
            "source_view": "top",
            "edges": [{
                "kind": "CIRCLE",
                "center": [float(rod_x), float(rod_y)],
                "radius": float(fine_radius),
            }],
            "bbox_2d": [
                float(rod_x - fine_radius), float(rod_y - fine_radius),
                float(rod_x + fine_radius), float(rod_y + fine_radius),
            ],
            "additive_component": True,
            "reason": "radial_rod_small_segment",
        }),
    ]


def _infer_left_side_stepped_rod_features(
    projected: Dict[str, ProjectedView],
    main_cx: float,
    main_z: float,
    main_radius: float,
    main_depth: float,
    rod_x: float,
    rod_y: float,
    coarse_radius_hint: float,
    tol: float,
) -> Optional[List[Feature]]:
    front = projected.get("front")
    left = projected.get("left") or projected.get("right")
    if front is None or left is None:
        return None

    left_circles = [
        (_outline_as_circle(outline, tol), outline)
        for outline in _unique_significant_outlines(left)
    ]
    left_circles = [(circle, outline) for circle, outline in left_circles if circle is not None]
    if not left_circles:
        return None
    side_circle, _side_outline = max(left_circles, key=lambda item: item[0][2])
    rod_y_from_left, side_z, side_radius = side_circle
    rod_z = float(side_z)
    coarse_radius = max(float(coarse_radius_hint), float(side_radius))

    fine = _fine_rod_span_from_view(front, rod_x, coarse_radius, main_z + main_radius, tol)
    fine_radius = fine[0] if fine is not None else None
    fine_length = fine[2] - fine[1] if fine is not None else None
    if fine_radius is None or fine_radius <= tol:
        fine_radius = _fine_radius_from_rectangles(left, rod_y_from_left, rod_z, coarse_radius, tol)
    if fine_length is None or fine_length <= tol:
        fine_length = main_radius
    if fine_radius is None or fine_radius <= tol or fine_radius >= coarse_radius * 0.95:
        return None

    dz = float(rod_z - main_z)
    surface_span_sq = float(main_radius * main_radius - dz * dz)
    if surface_span_sq > 0.0:
        main_left_surface_x = float(main_cx - math.sqrt(surface_span_sq))
    else:
        main_left_surface_x = float(main_cx - main_radius)
    # For a side branch fused into a round body, the coarse root is mostly
    # embedded in the host cylinder in the front projection. Keep only the
    # outer tangent at the cylinder surface instead of forcing visible overhang.
    coarse_x1 = float(main_left_surface_x)
    coarse_depth = float(coarse_radius * 2.0)
    coarse_x0 = coarse_x1 - coarse_depth
    fine_depth = float(fine_length)
    fine_x0 = float(coarse_x0 - fine_depth)

    return [
        Feature(kind="extrude_profile", params={
            "plane": "XZ",
            "depth": float(main_depth),
            "offset": 0.0,
            "source_view": "front",
            "edges": [{
                "kind": "CIRCLE",
                "center": [float(main_cx), float(main_z)],
                "radius": float(main_radius),
            }],
            "bbox_2d": [
                float(main_cx - main_radius), float(main_z - main_radius),
                float(main_cx + main_radius), float(main_z + main_radius),
            ],
            "additive_component": True,
            "reason": "main_horizontal_cylinder",
        }),
        Feature(kind="extrude_profile", params={
            "plane": "YZ",
            "depth": coarse_depth,
            "offset": coarse_x0,
            "source_view": "left",
            "edges": [{
                "kind": "CIRCLE",
                "center": [float(rod_y_from_left), float(rod_z)],
                "radius": float(coarse_radius),
            }],
            "bbox_2d": [
                float(rod_y_from_left - coarse_radius), float(rod_z - coarse_radius),
                float(rod_y_from_left + coarse_radius), float(rod_z + coarse_radius),
            ],
            "additive_component": True,
            "reason": "left_side_rod_large_segment",
        }),
        Feature(kind="extrude_profile", params={
            "plane": "YZ",
            "depth": fine_depth,
            "offset": fine_x0,
            "source_view": "left",
            "edges": [{
                "kind": "CIRCLE",
                "center": [float(rod_y_from_left), float(rod_z)],
                "radius": float(fine_radius),
            }],
            "bbox_2d": [
                float(rod_y_from_left - fine_radius), float(rod_z - fine_radius),
                float(rod_y_from_left + fine_radius), float(rod_z + fine_radius),
            ],
            "additive_component": True,
            "reason": "left_side_rod_small_segment",
        }),
    ]


def _fine_radius_from_rectangles(
    pv: ProjectedView,
    center: float,
    z_center: float,
    coarse_radius: float,
    tol: float,
) -> Optional[float]:
    candidates = []
    for outline in _unique_significant_outlines(pv):
        if not _is_axis_aligned_rectangle_outline(outline, tol):
            continue
        x0, z0, x1, z1 = outline.bbox
        rect_center = (float(x0) + float(x1)) * 0.5
        if abs(rect_center - center) > coarse_radius * 0.35:
            continue
        if float(z0) < z_center:
            continue
        radius = (float(x1) - float(x0)) * 0.5
        if tol < radius < coarse_radius:
            candidates.append(radius)
    if not candidates:
        return None
    return min(candidates)


def _fine_rod_span_from_view(
    pv: ProjectedView,
    center: float,
    coarse_radius: float,
    main_top: float,
    tol: float,
) -> Optional[Tuple[float, float, float]]:
    verticals = []
    for entity in pv.entities:
        if entity.kind != "LINE" or len(entity.points) < 2:
            continue
        (x0, z0), (x1, z1) = entity.points[:2]
        if abs(float(x0) - float(x1)) > tol * 0.25:
            continue
        lo, hi = sorted((float(z0), float(z1)))
        if hi - lo <= coarse_radius:
            continue
        if lo < main_top - coarse_radius * 0.25:
            continue
        x = float(x0)
        if abs(x - center) > coarse_radius * 1.2:
            continue
        verticals.append((x, lo, hi))
    if len(verticals) < 2:
        return None
    best = None
    for i, left_seg in enumerate(verticals):
        for right_seg in verticals[i + 1:]:
            x0, z00, z01 = left_seg
            x1, z10, z11 = right_seg
            width = abs(x1 - x0)
            if width <= tol or width >= coarse_radius * 1.9:
                continue
            lo = max(z00, z10)
            hi = min(z01, z11)
            if hi - lo <= coarse_radius:
                continue
            center_error = abs(((x0 + x1) * 0.5) - center)
            score = (hi - lo) - center_error * 5.0 - width
            if best is None or score > best[0]:
                best = (score, width * 0.5, lo, hi)
    if best is None:
        return None
    return best[1], best[2], best[3]


def _coarse_rod_start_from_views(
    views: Tuple[Tuple[ProjectedView, float], ...],
    coarse_radius: float,
    coarse_z1: float,
    main_top: float,
    tol: float,
) -> Optional[float]:
    starts: List[float] = []
    for pv, center in views:
        edge_positions = (center - coarse_radius, center + coarse_radius)
        for entity in pv.entities:
            if entity.kind != "LINE" or len(entity.points) < 2:
                continue
            (x0, z0), (x1, z1) = entity.points[:2]
            if abs(float(x0) - float(x1)) > tol * 0.25:
                continue
            x = float(x0)
            if min(abs(x - edge) for edge in edge_positions) > max(coarse_radius * 0.18, tol):
                continue
            lo, hi = sorted((float(z0), float(z1)))
            if hi < main_top - coarse_radius * 2.0 or lo > coarse_z1 + tol:
                continue
            if hi < coarse_z1 - coarse_radius * 0.6:
                continue
            starts.append(lo)
    if not starts:
        return None
    return max(0.0, min(starts))


def _bbox_is_redundant_component_candidate(
    bbox: Tuple[float, float, float, float],
    accepted: List[Tuple[float, float, float, float]],
    tol: float,
) -> bool:
    if not accepted:
        return False
    bx0, by0, bx1, by1 = bbox
    area = max((bx1 - bx0) * (by1 - by0), tol * tol)
    for ax0, ay0, ax1, ay1 in accepted:
        overlap_x = max(0.0, min(bx1, ax1) - max(bx0, ax0))
        overlap_y = max(0.0, min(by1, ay1) - max(by0, ay0))
        overlap = overlap_x * overlap_y
        if overlap >= area * 0.72:
            return True
    return False


def _unique_significant_outlines(pv: ProjectedView) -> List[Outline]:
    bundle = ViewBundle(
        name=pv.name,
        bbox=(0.0, 0.0, pv.width, pv.height),
        entities=pv.entities,
    )
    outlines, _circles = extract_closed_outlines_and_circles(bundle)
    scale = max(pv.width, pv.height, 1.0)
    tol = max(scale * 0.01, 1e-4)
    min_area = max(pv.width * pv.height * 0.015, tol * tol)
    unique: List[Outline] = []
    seen = set()
    for outline in outlines:
        area = _outline_area(outline)
        if area < min_area:
            continue
        key = tuple(round(float(v) / tol) for v in outline.bbox)
        if key in seen:
            continue
        seen.add(key)
        unique.append(outline)
    unique.sort(key=lambda item: _outline_area(item), reverse=True)
    return unique


def _component_candidate_outlines(pv: ProjectedView) -> List[Outline]:
    candidates = list(_unique_significant_outlines(pv))
    for pred in (_is_hidden_entity, lambda entity: not _is_hidden_entity(entity)):
        subset = [entity for entity in pv.entities if pred(entity)]
        if not subset:
            continue
        bundle = ViewBundle(
            name=pv.name,
            bbox=(0.0, 0.0, pv.width, pv.height),
            entities=subset,
        )
        outlines, _circles = extract_closed_outlines_and_circles(bundle)
        candidates.extend(outlines)

    scale = max(pv.width, pv.height, 1.0)
    tol = max(scale * 0.01, 1e-4)
    min_area = max(pv.width * pv.height * 0.015, tol * tol)
    unique: List[Outline] = []
    for outline in candidates:
        if _outline_area(outline) < min_area:
            continue
        outline_kind = _component_outline_kind(outline, tol)
        replaced = False
        for idx, existing in enumerate(unique):
            if outline_kind != _component_outline_kind(existing, tol):
                continue
            if _bbox_overlap_ratio(outline.bbox, existing.bbox) < 0.82:
                continue
            if _component_outline_rank(outline, tol) < _component_outline_rank(existing, tol):
                unique[idx] = outline
            replaced = True
            break
        if not replaced:
            unique.append(outline)
    unique.sort(key=lambda item: (_component_outline_rank(item, tol), -_outline_area(item)))
    return unique


def _component_outline_kind(outline: Outline, tol: float) -> str:
    if _outline_as_circle(outline, tol) is not None:
        return "circle"
    if _is_axis_aligned_rectangle_outline(outline, tol):
        return "rectangle"
    if len(outline.edges) <= 8:
        return "simple_polygon"
    return "complex_polygon"


def _component_outline_rank(outline: Outline, tol: float) -> int:
    if _outline_as_circle(outline, tol) is not None:
        return 1
    if _is_axis_aligned_rectangle_outline(outline, tol):
        return 3
    if len(outline.edges) <= 8:
        return 0
    return 2


def _bbox_overlap_ratio(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    overlap_x = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    overlap_y = max(0.0, min(ay1, by1) - max(ay0, by0))
    overlap = overlap_x * overlap_y
    area_a = max((ax1 - ax0) * (ay1 - ay0), 1e-12)
    area_b = max((bx1 - bx0) * (by1 - by0), 1e-12)
    return overlap / min(area_a, area_b)


def _outline_area(outline: Outline) -> float:
    return max(float(outline.width), 0.0) * max(float(outline.height), 0.0)


def _is_axis_aligned_rectangle_outline(outline: Outline, tol: float) -> bool:
    if len(outline.edges) != 4:
        return False
    for edge in outline.edges:
        if edge.get("kind") != "LINE":
            return False
        x0, y0 = edge.get("p0", [0.0, 0.0])
        x1, y1 = edge.get("p1", [0.0, 0.0])
        if abs(float(x0) - float(x1)) > tol and abs(float(y0) - float(y1)) > tol:
            return False
    return True


def _best_top_y_span_for_x(
    top_outlines: List[Outline],
    source_bbox: Tuple[float, float, float, float],
    tol: float,
    prefer_high: bool,
    full_span: Optional[float] = None,
) -> Optional[Tuple[float, float]]:
    sx0, _sy0, sx1, _sy1 = source_bbox
    source_width = max(sx1 - sx0, tol)
    candidates: List[Tuple[float, float, float, float]] = []
    if full_span is not None and full_span > tol:
        total_y0 = 0.0
        total_y1 = float(full_span)
    elif top_outlines:
        total_y0 = min(float(outline.bbox[1]) for outline in top_outlines)
        total_y1 = max(float(outline.bbox[3]) for outline in top_outlines)
    else:
        total_y0 = 0.0
        total_y1 = 0.0
    total_span = max(total_y1 - total_y0, tol)
    for outline in top_outlines:
        if _outline_as_circle(outline, tol) is not None:
            continue
        tx0, ty0, tx1, ty1 = outline.bbox
        overlap = max(0.0, min(sx1, tx1) - max(sx0, tx0))
        if overlap <= source_width * 0.2:
            continue
        span = ty1 - ty0
        if span <= tol:
            continue
        if span >= total_span * 0.82 and len(top_outlines) > 1:
            continue
        width_penalty = abs((tx1 - tx0) - source_width) / max(source_width, tol)
        overlap_score = overlap / source_width
        side_bias = ty0 * 100.0 if prefer_high else -ty0 * 100.0
        score = overlap_score * 10.0 - width_penalty + side_bias
        candidates.append((score, float(ty0), float(ty1), span))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    y0, y1 = candidates[0][1], candidates[0][2]
    if prefer_high and y1 < total_y1 - tol:
        return y1, total_y1
    return y0, y1


def _best_front_z_span_for_x(
    front_outlines: List[Outline],
    top_bbox: Tuple[float, float, float, float],
    tol: float,
) -> Optional[Tuple[float, float]]:
    tx0, _ty0, tx1, _ty1 = top_bbox
    top_width = max(tx1 - tx0, tol)
    candidates: List[Tuple[float, float, float]] = []
    for outline in front_outlines:
        fx0, fz0, fx1, fz1 = outline.bbox
        z_span = fz1 - fz0
        if z_span <= tol:
            continue
        overlap = max(0.0, min(tx1, fx1) - max(tx0, fx0))
        if overlap <= top_width * 0.35:
            continue
        width_penalty = abs((fx1 - fx0) - top_width) / max(top_width, tol)
        high_bias = fz0
        score = (overlap / top_width) * 10.0 - width_penalty + high_bias
        candidates.append((score, float(fz0), float(fz1)))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1], candidates[0][2]


def _apply_hidden_hole_extent(
    hole: Feature,
    projected: Dict[str, ProjectedView],
    width: float,
    depth: float,
    height: float,
) -> None:
    span = _hidden_hole_axis_span(hole, projected)
    if span is None:
        return
    axis = hole.params.get("axis", "Z")
    axis_length = {"X": width, "Y": depth, "Z": height}.get(axis, height)
    axis_length = float(axis_length)
    tol = max(axis_length * 0.01, 1e-3)
    start = max(0.0, min(float(span[0]), axis_length))
    end = max(0.0, min(float(span[1]), axis_length))
    if end - start <= tol:
        return
    if start <= tol and end >= axis_length - tol:
        return
    pos = list(hole.params.get("position", [0.0, 0.0, 0.0]))
    axis_index = {"X": 0, "Y": 1, "Z": 2}.get(axis, 2)
    pos[axis_index] = start
    hole.params["position"] = pos
    hole.params["through_length"] = end - start
    hole.params["blind"] = True
    hole.params["hidden_span"] = [start, end]


def _hidden_hole_axis_span(
    hole: Feature,
    projected: Dict[str, ProjectedView],
) -> Optional[Tuple[float, float]]:
    p = hole.params
    axis = p.get("axis", "Z")
    hx, hy, hz = p.get("position", [0.0, 0.0, 0.0])
    r = float(p.get("radius", 1.0))
    tol = max(r * 0.15, 0.5)
    if axis == "Z":
        checks = [("front", "x", hx, "y"), ("left", "x", hy, "y")]
    elif axis == "Y":
        checks = [("top", "x", hx, "y"), ("left", "y", hz, "x")]
    else:
        checks = [("top", "y", hy, "x"), ("front", "y", hz, "x")]

    spans: List[Tuple[float, float]] = []
    for view_name, overlap_axis, center, span_axis in checks:
        pv = projected.get(view_name)
        if pv is None:
            continue
        span = _hidden_span_for_projection(
            pv, overlap_axis, float(center) - r - tol,
            float(center) + r + tol, span_axis,
        )
        if span is not None:
            spans.append(span)
    if not spans:
        return None
    return min(span[0] for span in spans), max(span[1] for span in spans)


def _hidden_span_for_projection(
    pv: ProjectedView,
    overlap_axis: str,
    lo: float,
    hi: float,
    span_axis: str,
) -> Optional[Tuple[float, float]]:
    spans: List[Tuple[float, float]] = []
    for entity in pv.entities:
        if not _is_hidden_entity(entity):
            continue
        overlap = _bbox_range(entity, overlap_axis)
        span = _bbox_range(entity, span_axis)
        if overlap is None or span is None:
            continue
        if abs(float(span[1]) - float(span[0])) <= max((hi - lo) * 0.05, 1e-3):
            continue
        if _interval_overlaps(lo, hi, overlap[0], overlap[1]):
            spans.append(span)
    if not spans:
        return None
    return min(span[0] for span in spans), max(span[1] for span in spans)


def _infer_single_view_extrusion(
    projected: Dict[str, ProjectedView],
    depth: float,
) -> List[Feature]:
    if depth <= 0:
        return []
    view_name, pv = next(iter(projected.items()))
    bundle = ViewBundle(
        name=pv.name,
        bbox=(0.0, 0.0, pv.width, pv.height),
        entities=pv.entities,
    )
    outlines, circles = extract_closed_outlines_and_circles(bundle)
    outline = outlines[0] if outlines else None
    features: List[Feature] = []
    if outline is not None:
        features.append(Feature(
            kind="extrude_profile",
            params={
                "plane": "XY",
                "depth": float(depth),
                "source_view": view_name,
                "edges": [_serialize_edge(e) for e in outline.edges],
                "bbox_2d": list(outline.bbox),
                "single_view_extrude": True,
            },
        ))
        outer_bbox = outline.bbox
        inner_outlines = [
            candidate for candidate in outlines[1:]
            if _outline_inside_bbox(candidate, outer_bbox)
            and not _same_bbox(candidate.bbox, outer_bbox)
        ]
        hole_circles = circles
    else:
        visible_circles = _visible_circles(pv)
        if not visible_circles:
            return []
        outer = max(visible_circles, key=lambda e: float(e.radius or 0.0))
        if outer.center is None or outer.radius is None:
            return []
        cx, cy = outer.center
        radius = float(outer.radius)
        outer_bbox = (cx - radius, cy - radius, cx + radius, cy + radius)
        features.append(Feature(
            kind="extrude_profile",
            params={
                "plane": "XY",
                "depth": float(depth),
                "source_view": view_name,
                "edges": [{
                    "kind": "CIRCLE",
                    "center": [float(cx), float(cy)],
                    "radius": radius,
                }],
                "bbox_2d": list(outer_bbox),
                "single_view_extrude": True,
            },
        ))
        inner_outlines = []
        hole_circles = [circle for circle in visible_circles if circle is not outer]

    for inner in inner_outlines:
        features.append(Feature(
            kind="profile_cut",
            params={
                "plane": "XY",
                "depth": float(depth),
                "source_view": view_name,
                "edges": [_serialize_edge(edge) for edge in inner.edges],
                "bbox_2d": list(inner.bbox),
                "axis": "Z",
                "through_length": float(depth),
                "single_view_extrude": True,
            },
        ))

    for circle in hole_circles:
        if circle.center is None or circle.radius is None:
            continue
        if _looks_like_boundary_construction_circle(circle, outline):
            continue
        cx, cy = circle.center
        radius = float(circle.radius)
        if not _circle_inside_bbox(cx, cy, radius, outer_bbox):
            continue
        features.append(Feature(
            kind="hole",
            params={
                "diameter": 2.0 * radius,
                "radius": radius,
                "axis": "Z",
                "position": [float(cx), float(cy), 0.0],
                "through_length": float(depth),
                "source_view": view_name,
            },
        ))
    return features


def _outline_inside_bbox(
    outline: Outline,
    bbox: Tuple[float, float, float, float],
) -> bool:
    min_x, min_y, max_x, max_y = bbox
    bx0, by0, bx1, by1 = outline.bbox
    scale = max(max_x - min_x, max_y - min_y, 1.0)
    tol = max(scale * 0.01, 1e-3)
    return (
        bx0 >= min_x - tol and bx1 <= max_x + tol
        and by0 >= min_y - tol and by1 <= max_y + tol
    )


def _same_bbox(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> bool:
    scale = max(abs(v) for v in (*a, *b, 1.0))
    tol = max(scale * 1e-6, 1e-3)
    return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))


def _circle_inside_bbox(
    cx: float,
    cy: float,
    radius: float,
    bbox: Tuple[float, float, float, float],
) -> bool:
    min_x, min_y, max_x, max_y = bbox
    scale = max(max_x - min_x, max_y - min_y, radius, 1.0)
    tol = max(scale * 0.01, 1e-3)
    return (
        cx - radius >= min_x - tol
        and cx + radius <= max_x + tol
        and cy - radius >= min_y - tol
        and cy + radius <= max_y + tol
    )


def _serialize_edge(e: Dict) -> Dict:
    out = {"kind": e["kind"], "p0": list(e["p0"]), "p1": list(e["p1"])}
    if e["kind"] == "ARC":
        out["center"] = list(e["center"])
        out["radius"] = e["radius"]
        out["start_angle"] = e.get("start_angle")
        out["end_angle"] = e.get("end_angle")
        if e.get("clockwise"):
            out["clockwise"] = True
    return out


def _circle_to_hole(view_name: str, cx: float, cy: float, r: float,
                    width: float, depth: float, height: float
                    ) -> Optional[Feature]:
    if view_name == "top":
        axis = "Z"
        position = [cx, cy, 0.0]
        through_length = height
    elif view_name == "front":
        axis = "Y"
        position = [cx, 0.0, cy]
        through_length = depth
    elif view_name in {"left", "right"}:
        axis = "X"
        position = [0.0, cx, cy]
        through_length = width
    else:
        return None
    return Feature(
        kind="hole",
        params={
            "diameter": 2.0 * r,
            "radius": r,
            "axis": axis,
            "position": position,
            "through_length": through_length,
            "source_view": view_name,
        },
    )


def features_to_summary(features: List[Feature]) -> Dict:
    summary = {"total": len(features), "by_kind": {}, "items": []}
    for f in features:
        summary["by_kind"].setdefault(f.kind, 0)
        summary["by_kind"][f.kind] += 1
        summary["items"].append(f.to_dict())
    return summary
