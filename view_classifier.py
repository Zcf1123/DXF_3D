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

    clusters, spanning = _cluster_by_bbox(geom)

    # --- Fallback: centroid-based partitioning ----------------------------
    # When proximity clustering still merges everything into 1-2 groups
    # (views share border lines with no spatial gap), try to detect the two
    # orthogonal view-divider lines and use them to partition entities.
    # Spanning lines (view-dividers) are excluded from all views in this path.
    divider_mx: Optional[float] = None
    divider_my: Optional[float] = None
    if len(clusters) < 3:
        divider_result = _find_view_dividers(geom)
        if divider_result is not None:
            divider_mx, divider_my = divider_result
            all_bboxes_g = [e.bbox() for e in geom]
            all_xs_g = [c for b in all_bboxes_g if b for c in (b[0], b[2])]
            all_ys_g = [c for b in all_bboxes_g if b for c in (b[1], b[3])]
            total_w_g = max(all_xs_g) - min(all_xs_g) if all_xs_g else 0.0
            total_h_g = max(all_ys_g) - min(all_ys_g) if all_ys_g else 0.0
            # Partition only non-spanning entities; discard spanning lines.
            core_geom = [e for e in geom
                         if not _is_spanning_line(e, total_w_g, total_h_g)]
            clusters = _partition_by_dividers(core_geom, divider_mx, divider_my)
    # ----------------------------------------------------------------------

    bundles: List[ViewBundle] = []
    for i, cluster in enumerate(clusters):
        bundles.append(ViewBundle(name=f"unknown_{i}",
                                  bbox=_cluster_bbox(cluster),
                                  entities=cluster))

    if divider_mx is not None:
        # Groups from _partition_by_dividers are always in tl/tr/bl order.
        _DIVIDER_NAMES = ["front", "right", "top"]
        for i, b in enumerate(bundles):
            if i < len(_DIVIDER_NAMES):
                b.name = _DIVIDER_NAMES[i]
    else:
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

def _is_spanning_line(e: DxfEntity, total_w: float, total_h: float,
                      threshold: float = 0.75) -> bool:
    """Return True for a perfectly-horizontal or perfectly-vertical LINE whose
    length covers *threshold* fraction of the drawing's corresponding dimension.

    Such lines are projection-reference / view-divider lines that connect the
    three views on the sheet.  Including them in the pairwise proximity check
    merges otherwise-separate view clusters into one.
    """
    if e.kind != "LINE" or len(e.points) < 2:
        return False
    (x0, y0), (x1, y1) = e.points[0], e.points[1]
    dx, dy = abs(x1 - x0), abs(y1 - y0)
    # Must be axis-aligned (zero thickness in one direction)
    if dy < 1e-6 and total_w > 0 and dx / total_w >= threshold:
        return True   # horizontal spanning line
    if dx < 1e-6 and total_h > 0 and dy / total_h >= threshold:
        return True   # vertical spanning line
    return False


def _cluster_by_bbox(
    entities: List[DxfEntity],
) -> Tuple[List[List[DxfEntity]], List[DxfEntity]]:
    """Cluster entities by bbox proximity.

    Returns (clusters, spanning_lines) where spanning_lines are the
    view-divider lines that were excluded from proximity clustering.
    """
    # ------------------------------------------------------------------
    # Pre-pass: identify spanning reference lines (view-dividers) and
    # exclude them from proximity clustering so they don't bridge separate
    # view clusters.  They are reassigned to the nearest cluster afterwards.
    # ------------------------------------------------------------------
    all_bboxes = [e.bbox() for e in entities]
    all_xs = [c for b in all_bboxes if b for c in (b[0], b[2])]
    all_ys = [c for b in all_bboxes if b for c in (b[1], b[3])]
    total_w = (max(all_xs) - min(all_xs)) if all_xs else 0.0
    total_h = (max(all_ys) - min(all_ys)) if all_ys else 0.0

    spanning: List[DxfEntity] = []
    core: List[DxfEntity] = []
    for e in entities:
        if _is_spanning_line(e, total_w, total_h):
            spanning.append(e)
        else:
            core.append(e)

    n = len(core)
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

    bboxes = [e.bbox() for e in core]
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
    for i, e in enumerate(core):
        if bboxes[i] is None:
            continue
        groups.setdefault(find(i), []).append(e)

    clusters = list(groups.values())
    clusters = _merge_contained_clusters(clusters)

    # Re-assign spanning lines to the cluster whose bbox center is nearest.
    if spanning and clusters:
        cluster_bboxes = [_cluster_bbox(c) for c in clusters]
        for e in spanning:
            b = e.bbox()
            if b is None:
                clusters[0].append(e)
                continue
            ec_x = (b[0] + b[2]) * 0.5
            ec_y = (b[1] + b[3]) * 0.5
            best_idx, best_d = 0, float("inf")
            for idx, cb in enumerate(cluster_bboxes):
                cx = (cb[0] + cb[2]) * 0.5
                cy = (cb[1] + cb[3]) * 0.5
                d = (cx - ec_x) ** 2 + (cy - ec_y) ** 2
                if d < best_d:
                    best_d, best_idx = d, idx
            clusters[best_idx].append(e)

    return clusters, spanning


def _find_view_dividers(
    geom: List[DxfEntity],
) -> Optional[Tuple[float, float]]:
    """Detect the two orthogonal view-divider lines and return (x_div, y_div).

    A view-divider is a LINE that is axis-aligned AND spans at least 75 % of
    the drawing's total width (for horizontal) or height (for vertical).
    Returns the pair (x_coordinate_of_vertical_divider,
                       y_coordinate_of_horizontal_divider),
    or None if no clear pair is found.
    """
    all_bboxes = [e.bbox() for e in geom]
    all_xs = [c for b in all_bboxes if b for c in (b[0], b[2])]
    all_ys = [c for b in all_bboxes if b for c in (b[1], b[3])]
    if not all_xs:
        return None
    total_w = max(all_xs) - min(all_xs)
    total_h = max(all_ys) - min(all_ys)

    h_dividers: List[float] = []   # y-coordinates of horizontal spanning lines
    v_dividers: List[float] = []   # x-coordinates of vertical spanning lines
    for e in geom:
        if e.kind != "LINE" or len(e.points) < 2:
            continue
        (x0, y0), (x1, y1) = e.points[0], e.points[1]
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        if dy < 1e-6 and total_w > 0 and dx / total_w >= 0.75:
            h_dividers.append((y0 + y1) / 2.0)
        elif dx < 1e-6 and total_h > 0 and dy / total_h >= 0.75:
            v_dividers.append((x0 + x1) / 2.0)

    if not h_dividers or not v_dividers:
        return None
    # Use the median to be robust against multiple overlapping dividers.
    h_dividers.sort(); v_dividers.sort()
    y_div = h_dividers[len(h_dividers) // 2]
    x_div = v_dividers[len(v_dividers) // 2]
    return x_div, y_div


def _entity_centroid(e: DxfEntity) -> Optional[Tuple[float, float]]:
    b = e.bbox()
    if b is None:
        return None
    return (b[0] + b[2]) * 0.5, (b[1] + b[3]) * 0.5


def _partition_by_dividers(
    geom: List[DxfEntity],
    x_div: float,
    y_div: float,
) -> List[List[DxfEntity]]:
    """Split entities into (up to) 3 view groups using divider coordinates.

    Quadrant mapping (y-up DXF convention):
        top-left  (cx <= x_div, cy >= y_div)  → FRONT
        top-right (cx >  x_div, cy >= y_div)  → RIGHT
        bot-left  (cx <= x_div, cy <  y_div)  → TOP
        bot-right                              → ignored (empty in standard layout)

    Entities whose centroid falls exactly on a divider line are placed into
    the quadrant they overlap most.
    """
    groups: Dict[str, List[DxfEntity]] = {
        "tl": [], "tr": [], "bl": [],
    }
    for e in geom:
        c = _entity_centroid(e)
        if c is None:
            groups["tl"].append(e)
            continue
        cx, cy = c
        if cx <= x_div and cy >= y_div:
            groups["tl"].append(e)
        elif cx > x_div and cy >= y_div:
            groups["tr"].append(e)
        else:
            groups["bl"].append(e)

    groups["tl"] = _filter_divider_region(groups["tl"], "tl", x_div, y_div)
    groups["tr"] = _filter_divider_region(groups["tr"], "tr", x_div, y_div)
    groups["bl"] = _filter_divider_region(groups["bl"], "bl", x_div, y_div)

    result = []
    for key in ("tl", "tr", "bl"):
        if groups[key]:
            result.append(groups[key])
    return result


def _filter_divider_region(
    entities: List[DxfEntity],
    region: str,
    x_div: float,
    y_div: float,
    tol: float = 1e-6,
) -> List[DxfEntity]:
    out: List[DxfEntity] = []
    for e in entities:
        b = e.bbox()
        if b is None or e.kind != "LINE":
            out.append(e)
            continue
        outside = False
        if region == "tl" and (b[1] < y_div - tol or b[2] > x_div + tol):
            outside = True
        if region == "tr" and (b[1] < y_div - tol or b[0] < x_div - tol):
            outside = True
        if region == "bl" and (b[3] > y_div + tol or b[2] > x_div + tol):
            outside = True
        if outside:
            if _looks_hidden_or_center(e):
                clipped = _clip_line_to_divider_region(e, region, x_div, y_div, tol)
                if clipped is not None:
                    out.append(clipped)
            continue
        out.append(e)
    return out


def _looks_hidden_or_center(e: DxfEntity) -> bool:
    text = " ".join([
        e.linetype or "",
        str(e.extra.get("linetype_desc") or ""),
        e.layer or "",
    ]).upper()
    return any(tok in text for tok in (
        "HIDDEN", "_HID", "DASH", "PHANTOM", "CENTER", "CENTRO", "JIS_02",
    ))


def _clip_line_to_divider_region(
    e: DxfEntity,
    region: str,
    x_div: float,
    y_div: float,
    tol: float,
) -> Optional[DxfEntity]:
    if len(e.points) < 2:
        return None
    (x0, y0), (x1, y1) = e.points[0], e.points[1]
    if abs(x1 - x0) <= tol:
        if region in ("tl", "bl") and x0 > x_div + tol:
            return None
        if region == "tr" and x0 < x_div - tol:
            return None
        lo, hi = sorted((y0, y1))
        if region in ("tl", "tr"):
            lo = max(lo, y_div)
        else:
            hi = min(hi, y_div)
        if hi - lo <= tol:
            return None
        points = [(x0, lo), (x1, hi)] if y0 <= y1 else [(x0, hi), (x1, lo)]
    elif abs(y1 - y0) <= tol:
        if region in ("tl", "tr") and y0 < y_div - tol:
            return None
        if region == "bl" and y0 > y_div + tol:
            return None
        lo, hi = sorted((x0, x1))
        if region in ("tl", "bl"):
            hi = min(hi, x_div)
        else:
            lo = max(lo, x_div)
        if hi - lo <= tol:
            return None
        points = [(lo, y0), (hi, y1)] if x0 <= x1 else [(hi, y0), (lo, y1)]
    else:
        return None
    return DxfEntity(
        kind=e.kind,
        layer=e.layer,
        linetype=e.linetype,
        points=points,
        center=e.center,
        radius=e.radius,
        start_angle=e.start_angle,
        end_angle=e.end_angle,
        text=e.text,
        dim_type=e.dim_type,
        dim_text=e.dim_text,
        dim_measurement=e.dim_measurement,
        block_name=e.block_name,
        extra=dict(e.extra),
    )


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

def _assign_by_layout(
    bundles: List[ViewBundle],
    mx: Optional[float] = None,
    my: Optional[float] = None,
) -> None:
    """Assign names by quadrant against the overall drawing bbox.

       FRONT (top-left)     RIGHT (top-right)
       TOP   (bottom-left)

    *mx* and *my* are the divider coordinates.  When not supplied they are
    inferred as the midpoint of the overall bounding box of all clusters.
    """
    if not bundles:
        return

    if mx is None or my is None:
        xs0 = min(b.bbox[0] for b in bundles)
        ys0 = min(b.bbox[1] for b in bundles)
        xs1 = max(b.bbox[2] for b in bundles)
        ys1 = max(b.bbox[3] for b in bundles)
        if mx is None:
            mx = (xs0 + xs1) * 0.5
        if my is None:
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

