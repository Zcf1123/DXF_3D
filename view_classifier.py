"""Cluster DXF entities into the three views (front / top / right).

View-layout convention (see ../README.md), FIXED:

       +----------------+----------------+
       |  FRONT (TL)    |  RIGHT (TR)    |
       +----------------+----------------+
       |  TOP   (BL)    |   (empty)      |
       +----------------+----------------+

Classification is deterministic: clusters are placed into one of three
quadrants (top-left / bottom-left / top-right) of the overall drawing
bbox. Cluster *centers* — not their bboxes — decide the quadrant; the
larger cluster wins on ties.

Annotations (DIMENSION/TEXT/MTEXT/HATCH/SOLID) are attached to the
nearest geometry cluster.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .dxf_loader import DxfEntity


GEOMETRY_KINDS = {"LINE", "CIRCLE", "ARC", "LWPOLYLINE",
                  "POLYLINE", "SPLINE", "ELLIPSE"}
ANNOTATION_KINDS = {"TEXT", "MTEXT", "DIMENSION", "HATCH", "SOLID"}


@dataclass
class ViewBundle:
    name: str                                # "front" | "top" | "right" | "unknown_<i>"
    bbox: Tuple[float, float, float, float]
    entities: List[DxfEntity] = field(default_factory=list)
    annotations: List[DxfEntity] = field(default_factory=list)

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def center(self) -> Tuple[float, float]:
        return ((self.bbox[0] + self.bbox[2]) * 0.5,
                (self.bbox[1] + self.bbox[3]) * 0.5)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "bbox": list(self.bbox),
            "width": self.width,
            "height": self.height,
            "entity_count": len(self.entities),
            "annotation_count": len(self.annotations),
        }


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def classify_views(entities: List[DxfEntity]) -> List[ViewBundle]:
    geom = [e for e in entities if e.kind in GEOMETRY_KINDS]
    ann = [e for e in entities if e.kind in ANNOTATION_KINDS]
    if not geom:
        return []

    clusters = _cluster_by_bbox(geom)
    bundles: List[ViewBundle] = []
    for i, cluster in enumerate(clusters):
        bundles.append(ViewBundle(name=f"unknown_{i}",
                                  bbox=_cluster_bbox(cluster),
                                  entities=cluster))

    _assign_by_layout(bundles)

    # Attach annotations to nearest cluster
    for a in ann:
        b = a.bbox()
        if b is None:
            continue
        cx, cy = (b[0] + b[2]) * 0.5, (b[1] + b[3]) * 0.5
        nearest = _nearest_bundle(bundles, (cx, cy))
        if nearest is not None:
            nearest.annotations.append(a)

    return bundles


# ---------------------------------------------------------------------------
# Clustering (bbox proximity + nested-cluster merge)
# ---------------------------------------------------------------------------

def _cluster_by_bbox(entities: List[DxfEntity]) -> List[List[DxfEntity]]:
    n = len(entities)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    bboxes = [e.bbox() for e in entities]
    diags = []
    for b in bboxes:
        if b is None:
            diags.append(0.0)
        else:
            dx, dy = b[2] - b[0], b[3] - b[1]
            diags.append((dx * dx + dy * dy) ** 0.5)

    for i in range(n):
        if bboxes[i] is None:
            continue
        for j in range(i + 1, n):
            if bboxes[j] is None:
                continue
            tol = 0.1 * min(diags[i] or 1.0, diags[j] or 1.0)
            if _bbox_close(bboxes[i], bboxes[j], tol):
                union(i, j)
            elif _bbox_contains(bboxes[i], bboxes[j]) or \
                 _bbox_contains(bboxes[j], bboxes[i]):
                union(i, j)

    groups: Dict[int, List[DxfEntity]] = {}
    for i, e in enumerate(entities):
        if bboxes[i] is None:
            continue
        groups.setdefault(find(i), []).append(e)

    clusters = list(groups.values())
    return _merge_contained_clusters(clusters)


def _merge_contained_clusters(clusters: List[List[DxfEntity]]
                              ) -> List[List[DxfEntity]]:
    if len(clusters) < 2:
        return clusters
    bboxes = [_cluster_bbox(c) for c in clusters]
    order = sorted(range(len(clusters)), key=lambda i: _bbox_area(bboxes[i]))
    merged_into: Dict[int, int] = {}
    for idx, i in enumerate(order):
        for j in order[idx + 1:]:
            if j in merged_into:
                continue
            if _bbox_contains(bboxes[j], bboxes[i]):
                merged_into[i] = j
                break
    if not merged_into:
        return clusters
    out: List[List[DxfEntity]] = []
    for i, c in enumerate(clusters):
        if i in merged_into:
            continue
        items = list(c)
        for k, target in merged_into.items():
            if target == i:
                items.extend(clusters[k])
        out.append(items)
    return out


def _bbox_area(b: Tuple[float, float, float, float]) -> float:
    return max(b[2] - b[0], 0.0) * max(b[3] - b[1], 0.0)


def _bbox_close(a, b, tol: float) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    if ax1 + tol < bx0 or bx1 + tol < ax0:
        return False
    if ay1 + tol < by0 or by1 + tol < ay0:
        return False
    return True


def _bbox_contains(outer, inner, tol: float = 1e-6) -> bool:
    ox0, oy0, ox1, oy1 = outer
    ix0, iy0, ix1, iy1 = inner
    return (ox0 - tol <= ix0 and ix1 <= ox1 + tol and
            oy0 - tol <= iy0 and iy1 <= oy1 + tol)


def _cluster_bbox(cluster: List[DxfEntity]
                  ) -> Tuple[float, float, float, float]:
    xs0, ys0, xs1, ys1 = [], [], [], []
    for e in cluster:
        b = e.bbox()
        if b is None:
            continue
        xs0.append(b[0]); ys0.append(b[1])
        xs1.append(b[2]); ys1.append(b[3])
    return (min(xs0), min(ys0), max(xs1), max(ys1))


def _nearest_bundle(bundles: List[ViewBundle],
                    pt: Tuple[float, float]) -> Optional[ViewBundle]:
    cx, cy = pt
    best, best_d = None, float("inf")
    for b in bundles:
        bx0, by0, bx1, by1 = b.bbox
        dx = max(bx0 - cx, 0, cx - bx1)
        dy = max(by0 - cy, 0, cy - by1)
        d = dx * dx + dy * dy
        if d < best_d:
            best_d = d
            best = b
    return best


# ---------------------------------------------------------------------------
# Fixed-layout assignment
# ---------------------------------------------------------------------------

def _assign_by_layout(bundles: List[ViewBundle]) -> None:
    """Assign names by quadrant against the overall drawing bbox.

       FRONT (top-left)     RIGHT (top-right)
       TOP   (bottom-left)
    """
    if not bundles:
        return

    # Overall bbox of all clusters.
    xs0 = min(b.bbox[0] for b in bundles)
    ys0 = min(b.bbox[1] for b in bundles)
    xs1 = max(b.bbox[2] for b in bundles)
    ys1 = max(b.bbox[3] for b in bundles)
    mx = (xs0 + xs1) * 0.5
    my = (ys0 + ys1) * 0.5

    quad: Dict[str, List[ViewBundle]] = {"front": [], "top": [], "right": []}
    for b in bundles:
        cx, cy = b.center
        if cx <= mx and cy >= my:
            quad["front"].append(b)
        elif cx <= mx and cy < my:
            quad["top"].append(b)
        elif cx > mx and cy >= my:
            quad["right"].append(b)
        # bottom-right quadrant is unused per spec; leave such clusters
        # with their default unknown name.

    for name, group in quad.items():
        if not group:
            continue
        # Pick the largest cluster (most entities, then largest bbox area).
        chosen = max(group, key=lambda b: (len(b.entities),
                                           _bbox_area(b.bbox)))
        chosen.name = name

