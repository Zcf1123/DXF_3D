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
    kind: str   # "extrude_profile" | "base_block" | "hole"
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


def _make_outline(pv: ProjectedView) -> Tuple[Optional[Outline], List[DxfEntity]]:
    bundle = ViewBundle(name=pv.name,
                        bbox=(0.0, 0.0, pv.width, pv.height),
                        entities=pv.entities)
    return extract_outline_and_holes(bundle)


# ---------------------------------------------------------------------------
# Cross-view hidden-line validation
# ---------------------------------------------------------------------------

# Tokens whose presence in a layer name (case-insensitive) marks it as
# carrying hidden / dashed lines.
_HIDDEN_LAYER_TOKENS: frozenset = frozenset({
    "HIDDEN", "HIDE", "DASH", "DASHED", "PHANTOM",
    "VERDECKT", "MASQUE", "NASCOSTA", "虚线",
})


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
    hidden = [e for e in pv.entities if _is_hidden_layer(e.layer)]
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
        score = _outline_complexity(outline)
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
    profile_holes: List[DxfEntity] = []
    if chosen is not None:
        profile_view, outline, profile_holes = chosen
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
