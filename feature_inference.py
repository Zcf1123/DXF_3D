"""Infer 3D features from projected views.

Strategy:
  1. Among the three views, pick the **most informative** outline (most
     edges, or any outline containing ARC / non-rectangular shape) as the
     extrusion profile. Extrude it along the axis perpendicular to that
     view by the perpendicular extent of the part.
       - top   outline -> profile in XY, extrude along +Z by H
       - front outline -> profile in XZ, extrude along +Y by D
       - right outline -> profile in YZ, extrude along +X by W
     Falls back to a bounding-box block if no closed outline is found.
  2. A single same-radius CIRCLE in all three canonical views becomes a sphere
      when the projected centers agree across TOP/FRONT/RIGHT.
  3. CIRCLE entities become through-holes:
       circle in TOP   view -> hole axis = Z
       circle in FRONT view -> hole axis = Y
       circle in RIGHT view -> hole axis = X
  4. For prismatic polygon profiles, visible front/right arc offsets become
      a top/bottom arc-profile edge treatment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .dxf_loader import DxfEntity
from .projection_mapper import ProjectedView
from .geometry_estimator import (
    Outline, extract_outline_and_holes, extract_closed_outlines_and_circles,
    estimate_part_size,
)
from .view_classifier import ViewBundle


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
    "right": "YZ",
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
    """Infer top/bottom chamfer distance from FRONT/RIGHT side views.

    In this nut drawing, the side views keep short vertical side segments
    inset from z=0 and z=H and connect them to the end faces with arcs. The
    inset gives a stable radius for a FreeCAD fillet on the horizontal outer
    edges of the vertical prism.
    """
    if height <= 0:
        return None
    candidates: List[float] = []
    for view_name in ("front", "right"):
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
    TOP/FRONT/RIGHT coordinate mapping:
        TOP   circle center -> (X, Y)
        FRONT circle center -> (X, Z)
        RIGHT circle center -> (Y, Z)
    """
    required = {name: _visible_circles(projected.get(name))
                for name in ("top", "front", "right")}
    if any(len(circles) != 1 for circles in required.values()):
        return None
    if any(
        any(e.kind != "CIRCLE" for e in projected[name].entities)
        for name in ("top", "front", "right")
    ):
        return None

    top = required["top"][0]
    front = required["front"][0]
    right = required["right"][0]
    assert top.center and front.center and right.center
    radius = float(top.radius or 0.0)
    if radius <= 0:
        return None
    scale = max(width, depth, height, radius * 2.0, 1.0)
    tol = max(scale * 0.03, 1e-3)
    for circle in (front, right):
        if abs(float(circle.radius or 0.0) - radius) > tol:
            return None
    if any(abs(dim - 2.0 * radius) > tol for dim in (width, depth, height)):
        return None

    x_from_top, y_from_top = top.center
    x_from_front, z_from_front = front.center
    y_from_right, z_from_right = right.center
    if abs(float(x_from_top) - float(x_from_front)) > tol:
        return None
    if abs(float(y_from_top) - float(y_from_right)) > tol:
        return None
    if abs(float(z_from_front) - float(z_from_right)) > tol:
        return None

    return Feature(
        kind="sphere",
        params={
            "radius": radius,
            "center": [
                (float(x_from_top) + float(x_from_front)) * 0.5,
                (float(y_from_top) + float(y_from_right)) * 0.5,
                (float(z_from_front) + float(z_from_right)) * 0.5,
            ],
            "source_views": ["top", "front", "right"],
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
      RIGHT checks bbox-x ∈ [hy ± r]   (draw.x → world Y)

    axis Y (source=front):
      TOP   checks bbox-x ∈ [hx ± r]   (draw.x → world X)
      RIGHT checks bbox-y ∈ [hz ± r]   (draw.y → world Z)

    axis X (source=right):
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
        checks = [("front", "x", hx), ("right", "x", hy)]
    elif axis == "Y":
        checks = [("top",   "x", hx), ("right", "y", hz)]
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
                   single_view_extrude_depth: Optional[float] = None) -> List[Feature]:
    if not projected:
        return []

    if single_view_extrude_depth is not None and len(projected) == 1:
        return _infer_single_view_extrusion(projected, single_view_extrude_depth)

    width, depth, height = estimate_part_size(projected, bundles)
    features: List[Feature] = []

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
    for view_name in ("top", "front", "right"):
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
        # of (top, front, right) — top first because end-face profiles are
        # the most natural extrusion source for prismatic parts.
        order = {"top": 0, "front": 1, "right": 2}
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
        else:  # right
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
                    "source_views": ["front", "right"],
                    **({"top_radius": top_radius} if top_radius else {}),
                },
            ))

    return features


def _infer_internal_profile_cuts(
    projected: Dict[str, ProjectedView],
    profile_view: Optional[str],
    width: float,
    depth: float,
    height: float,
) -> List[Feature]:
    axis_depth = {"top": height, "front": depth, "right": width}
    cuts: List[Feature] = []
    seen: set = set()
    for view_name in ("front", "top", "right"):
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
        "right": ("YZ", "X", width),
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
    right = projected.get("right")
    if top is None or front is None or right is None:
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
    right_bands = _stepped_side_bands(right)
    if len(front_bands) < 2 or len(front_bands) != len(right_bands):
        return None

    scale = max(width, depth, height, float(outer.radius) * 2.0, 1.0)
    tol = max(scale * 0.03, 1e-3)
    segments: List[Dict] = []
    for front_band, right_band in zip(front_bands, right_bands):
        z0, z1, cx, radius_x = front_band
        rz0, rz1, cy, radius_y = right_band
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
    center_y = sum(band[2] for band in right_bands) / len(right_bands)
    if abs(float(top_cx) - center_x) > tol or abs(float(top_cy) - center_y) > tol:
        return None

    return Feature(
        kind="cylinder_stack",
        params={
            "axis": "Z",
            "center": [float(center_x), float(center_y)],
            "segments": segments,
            "source_views": ["top", "front", "right"],
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
        checks = [("front", "x", hx, "y"), ("right", "x", hy, "y")]
    elif axis == "Y":
        checks = [("top", "x", hx, "y"), ("right", "y", hz, "x")]
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
    elif view_name == "right":
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
