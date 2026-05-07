"""Estimate outlines and dimensions from raw geometry and DIMENSION entities.

Two main jobs:
  1. extract_outline_and_holes(bundle): build the largest closed loop of
     LINE/ARC edges in a view (used as an extrusion profile) plus interior
     circles (used as through-holes).
  2. estimate_part_size(projected, bundles): infer (width, depth, height).
     Preference order:
       a) Linear DIMENSION measurements from view annotations (when bundles
          are supplied and contain type-0 linear dims with a rotation angle).
       b) View bbox averages (fallback when no usable dimensions are found).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .dxf_loader import DxfEntity
from .view_classifier import ViewBundle
from .projection_mapper import ProjectedView


# ---------------------------------------------------------------------------
# Dimension-based size extraction
# ---------------------------------------------------------------------------

# Per-view mapping:  (horizontal_axis, vertical_axis)
# FRONT (XZ): horizontal = W (along X), vertical = H (along Z)
# TOP   (XY): horizontal = W (along X), vertical = D (along Y)
# RIGHT (YZ): horizontal = D (along Y), vertical = H (along Z)
_VIEW_AXIS_MAP: Dict[str, Tuple[str, str]] = {
    "front": ("W", "H"),
    "top":   ("W", "D"),
    "right": ("D", "H"),
}


def _is_horizontal(angle_deg: float) -> bool:
    """True when a dimension line is within 15° of horizontal (0° / 180°)."""
    return abs(angle_deg % 180.0) < 15.0


def _is_vertical(angle_deg: float) -> bool:
    """True when a dimension line is within 15° of vertical (90° / 270°)."""
    return abs((angle_deg % 180.0) - 90.0) < 15.0


def _dim_measurements_by_axis(
    bundles: List[ViewBundle],
) -> Dict[str, List[float]]:
    """Return {'W': [...], 'D': [...], 'H': [...]} from DIMENSION annotations.

    Only processes **rotated/horizontal/vertical linear** dimensions
    (``dim_type & 0x0F == 0``) that carry a valid ``dim_measurement`` and
    a rotation angle stored in ``extra['rotation']``.  Aligned (type 1),
    angular, diameter, and radius dimensions are ignored because their axis
    cannot be determined without the full definition-point geometry.
    """
    result: Dict[str, List[float]] = {"W": [], "D": [], "H": []}
    for bundle in bundles:
        view = bundle.name
        if view not in _VIEW_AXIS_MAP:
            continue
        horiz_axis, vert_axis = _VIEW_AXIS_MAP[view]
        for ann in bundle.annotations:
            if ann.kind != "DIMENSION":
                continue
            meas = ann.dim_measurement
            if meas is None or meas <= 0:
                continue
            if (ann.dim_type or 0) & 0x0F != 0:
                # Not a rotated/horizontal/vertical linear dimension.
                continue
            rotation = float(ann.extra.get("rotation", 0.0) or 0.0)
            if _is_horizontal(rotation):
                result[horiz_axis].append(meas)
            elif _is_vertical(rotation):
                result[vert_axis].append(meas)
            # Oblique dimensions are skipped.
    return result


# ---------------------------------------------------------------------------
# Outline extraction
# ---------------------------------------------------------------------------

Edge = Dict[str, object]   # {"kind": "LINE"|"ARC", "p0":(x,y), "p1":(x,y), ...}


@dataclass
class Outline:
    edges: List[Edge] = field(default_factory=list)
    bbox: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    def to_dict(self) -> Dict:
        return {
            "bbox": list(self.bbox),
            "width": self.width,
            "height": self.height,
            "edges": [_edge_to_dict(e) for e in self.edges],
        }


def _edge_to_dict(e: Edge) -> Dict:
    out = {"kind": e["kind"], "p0": list(e["p0"]), "p1": list(e["p1"])}
    if e["kind"] == "ARC":
        out["center"] = list(e["center"])
        out["radius"] = e["radius"]
        out["start_angle"] = e.get("start_angle")
        out["end_angle"] = e.get("end_angle")
    return out


def extract_outline_and_holes(
    bundle: ViewBundle, tol: float = 1e-3
) -> Tuple[Optional[Outline], List[DxfEntity]]:
    """Return (outline, holes).

    outline: largest closed loop built from LINE / ARC / closed POLYLINE.
    holes:   CIRCLE entities (treated as interior through-holes).
    """
    circles = [e for e in bundle.entities if e.kind == "CIRCLE"]
    closed_polys = [
        e for e in bundle.entities
        if e.kind in ("LWPOLYLINE", "POLYLINE") and e.extra.get("closed")
        and len(e.points) >= 3
    ]

    # Prefer an explicit closed polyline if present.
    if closed_polys:
        # pick the polyline with the largest bbox
        def area(p):
            xs = [pt[0] for pt in p.points]
            ys = [pt[1] for pt in p.points]
            return (max(xs) - min(xs)) * (max(ys) - min(ys))
        pl = max(closed_polys, key=area)
        edges = []
        for i in range(len(pl.points)):
            a = pl.points[i]
            b = pl.points[(i + 1) % len(pl.points)]
            if _pt_close(a, b, tol):
                continue
            edges.append({"kind": "LINE", "p0": (float(a[0]), float(a[1])),
                          "p1": (float(b[0]), float(b[1]))})
        return _outline_from_loop(edges), circles

    # Otherwise build from individual line/arc segments.
    edges: List[Edge] = []
    for e in bundle.entities:
        if e.kind == "LINE" and len(e.points) >= 2:
            a, b = e.points[0], e.points[1]
            if _pt_close(a, b, tol):
                continue
            edges.append({"kind": "LINE",
                          "p0": (float(a[0]), float(a[1])),
                          "p1": (float(b[0]), float(b[1]))})
        elif e.kind == "ARC" and e.center is not None and e.radius is not None:
            edges.append({
                "kind": "ARC",
                "center": (float(e.center[0]), float(e.center[1])),
                "radius": float(e.radius),
                "start_angle": float(e.start_angle or 0.0),
                "end_angle": float(e.end_angle or 0.0),
                "p0": _arc_endpoint(e, "start"),
                "p1": _arc_endpoint(e, "end"),
            })

    loops = _find_closed_loops(edges, tol)
    if not loops:
        return None, circles

    # Outer loop = largest bbox area.
    outer = max(loops, key=_loop_bbox_area)
    return _outline_from_loop(outer), circles


def _arc_endpoint(arc: DxfEntity, which: str) -> Tuple[float, float]:
    cx, cy = arc.center
    ang = math.radians((arc.start_angle if which == "start" else arc.end_angle) or 0.0)
    return (cx + arc.radius * math.cos(ang), cy + arc.radius * math.sin(ang))


def _pt_close(a, b, tol: float) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _find_closed_loops(edges: List[Edge], tol: float) -> List[List[Edge]]:
    used = [False] * len(edges)
    loops: List[List[Edge]] = []

    for i, e0 in enumerate(edges):
        if used[i]:
            continue
        loop: List[Edge] = [e0]
        used_idx = {i}
        end_pt = e0["p1"]
        start_pt = e0["p0"]

        while not _pt_close(end_pt, start_pt, tol):
            found = False
            for j, e in enumerate(edges):
                if j in used_idx:
                    continue
                if _pt_close(e["p0"], end_pt, tol):
                    loop.append(e)
                    used_idx.add(j)
                    end_pt = e["p1"]
                    found = True
                    break
                if _pt_close(e["p1"], end_pt, tol):
                    rev = _reverse_edge(e)
                    loop.append(rev)
                    used_idx.add(j)
                    end_pt = rev["p1"]
                    found = True
                    break
            if not found:
                break

        if _pt_close(end_pt, start_pt, tol) and len(loop) >= 3:
            loops.append(loop)
            for k in used_idx:
                used[k] = True
    return loops


def _reverse_edge(e: Edge) -> Edge:
    if e["kind"] == "LINE":
        return {"kind": "LINE", "p0": e["p1"], "p1": e["p0"]}
    # ARC: swap start/end angles too
    return {
        "kind": "ARC",
        "center": e["center"],
        "radius": e["radius"],
        "start_angle": e.get("end_angle"),
        "end_angle": e.get("start_angle"),
        "p0": e["p1"],
        "p1": e["p0"],
    }


def _loop_bbox(loop: List[Edge]) -> Tuple[float, float, float, float]:
    xs, ys = [], []
    for e in loop:
        for p in (e["p0"], e["p1"]):
            xs.append(p[0]); ys.append(p[1])
        if e["kind"] == "ARC":
            cx, cy = e["center"]
            r = e["radius"]
            xs.extend([cx - r, cx + r])
            ys.extend([cy - r, cy + r])
    return (min(xs), min(ys), max(xs), max(ys))


def _loop_bbox_area(loop: List[Edge]) -> float:
    b = _loop_bbox(loop)
    return max(b[2] - b[0], 0.0) * max(b[3] - b[1], 0.0)


def _outline_from_loop(loop: List[Edge]) -> Outline:
    return Outline(edges=loop, bbox=_loop_bbox(loop))


# ---------------------------------------------------------------------------
# Whole-part size estimation
# ---------------------------------------------------------------------------

def estimate_part_size(
    projected: Dict[str, ProjectedView],
    bundles: Optional[List[ViewBundle]] = None,
) -> Tuple[float, float, float]:
    """Return (width X, depth Y, height Z).

    When *bundles* are provided, DIMENSION annotations are first consulted.
    For each axis (W / D / H) the **largest** measurement found across all
    matching linear dimensions is used; this reliably picks the overall-extent
    dimension even when intermediate feature dimensions are also present.
    Any axis without a usable DIMENSION falls back to the bbox average.
    """
    front = projected.get("front")
    top   = projected.get("top")
    right = projected.get("right")

    # --- 1. Attempt dimension-based estimates ---
    dim_W: Optional[float] = None
    dim_D: Optional[float] = None
    dim_H: Optional[float] = None

    if bundles is not None:
        by_axis = _dim_measurements_by_axis(bundles)
        if by_axis["W"]:
            dim_W = max(by_axis["W"])
        if by_axis["D"]:
            dim_D = max(by_axis["D"])
        if by_axis["H"]:
            dim_H = max(by_axis["H"])

    # --- 2. Bbox fallback for any axis without dimension data ---
    Ws, Ds, Hs = [], [], []
    if front is not None:
        Ws.append(front.width); Hs.append(front.height)
    if top is not None:
        Ws.append(top.width); Ds.append(top.height)
    if right is not None:
        Ds.append(right.width); Hs.append(right.height)

    bbox_W = sum(Ws) / len(Ws) if Ws else 10.0
    bbox_D = sum(Ds) / len(Ds) if Ds else 10.0
    bbox_H = sum(Hs) / len(Hs) if Hs else 10.0

    width  = dim_W if dim_W is not None else bbox_W
    depth  = dim_D if dim_D is not None else bbox_D
    height = dim_H if dim_H is not None else bbox_H
    return width, depth, height
