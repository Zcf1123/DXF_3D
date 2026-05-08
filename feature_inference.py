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
  2. CIRCLE entities become through-holes:
       circle in TOP   view -> hole axis = Z
       circle in FRONT view -> hole axis = Y
       circle in RIGHT view -> hole axis = X
  3. For prismatic polygon profiles, visible front/right arc offsets become
      a top/bottom arc-profile edge treatment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .dxf_loader import DxfEntity
from .projection_mapper import ProjectedView
from .geometry_estimator import (
    Outline, extract_outline_and_holes, estimate_part_size,
)
from .view_classifier import ViewBundle


@dataclass
class Feature:
    kind: str   # "extrude_profile" | "base_block" | "hole" | "edge_chamfer"
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


# ---------------------------------------------------------------------------
# Cross-view hidden-line validation
# ---------------------------------------------------------------------------

# Tokens whose presence in a layer name (case-insensitive) marks it as
# carrying hidden / dashed lines.
_HIDDEN_LAYER_TOKENS: frozenset = frozenset({
    "HIDDEN", "HIDE", "DASH", "DASHED", "PHANTOM",
    "VERDECKT", "MASQUE", "NASCOSTA", "虚线",
})

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
    upper = e.layer.upper()
    return any(tok in upper for tok in _HIDDEN_LAYER_TOKENS)


# Keep old name as alias so any external callers still work.
def _is_hidden_layer(layer: str) -> bool:
    upper = layer.upper()
    return any(tok in upper for tok in _HIDDEN_LAYER_TOKENS)


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
                   bundles: Optional[List[ViewBundle]] = None) -> List[Feature]:
    if not projected:
        return []

    width, depth, height = estimate_part_size(projected, bundles)
    features: List[Feature] = []

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
        features.append(Feature(
            kind="extrude_profile",
            params={
                "plane": plane,
                "depth": extrusion_depth,
                "source_view": profile_view,
                "edges": [_serialize_edge(e) for e in outline.edges],
                "bbox_2d": list(outline.bbox),
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
                hole_candidates.append(hole)

    for hole in hole_candidates:
        if _hole_has_hidden_evidence(hole, projected):
            features.append(hole)

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


def _serialize_edge(e: Dict) -> Dict:
    out = {"kind": e["kind"], "p0": list(e["p0"]), "p1": list(e["p1"])}
    if e["kind"] == "ARC":
        out["center"] = list(e["center"])
        out["radius"] = e["radius"]
        out["start_angle"] = e.get("start_angle")
        out["end_angle"] = e.get("end_angle")
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
