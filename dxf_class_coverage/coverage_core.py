"""Standalone geometric coverage helpers for DXF class comparison."""
from __future__ import annotations

import math
from typing import Any, Dict, List

from dxf_loader import DxfEntity


def segments_from_entities(entities: List[DxfEntity]):
    segments = []
    for entity in entities:
        if entity.kind == "LINE" and len(entity.points) >= 2:
            segments.append((tuple(entity.points[0]), tuple(entity.points[1])))
        elif entity.kind == "CIRCLE" and entity.center is not None and entity.radius is not None:
            cx, cy = entity.center
            segments.extend(circle_segments(float(cx), float(cy), float(entity.radius)))
        elif entity.kind == "ARC" and entity.center is not None and entity.radius is not None:
            segments.extend(arc_segments(entity))
        elif entity.kind in ("LWPOLYLINE", "POLYLINE") and len(entity.points) >= 2:
            points = entity.points
            closed = bool(entity.extra.get("closed", False))
            end = len(points) if closed else len(points) - 1
            for idx in range(end):
                segments.append((tuple(points[idx]), tuple(points[(idx + 1) % len(points)])))
    return segments


def align_segments_to_origin(segments):
    """Translate segments so their bbox lower-left corner becomes (0, 0).

    This aligns front/top/left view start points independently without changing
    model size. Different-sized but similarly shaped DXFs remain distinguishable.
    """
    bbox = segments_bbox(segments)
    if bbox is None:
        return segments
    xmin, ymin, _xmax, _ymax = bbox
    aligned = []
    for a, b in segments:
        ax, ay = a
        bx, by = b
        aligned.append(
            (
                (float(ax) - xmin, float(ay) - ymin),
                (float(bx) - xmin, float(by) - ymin),
            )
        )
    return aligned


def circle_segments(cx: float, cy: float, radius: float, steps: int = 96):
    pts = [
        (cx + radius * math.cos(2.0 * math.pi * i / steps),
         cy + radius * math.sin(2.0 * math.pi * i / steps))
        for i in range(steps + 1)
    ]
    return list(zip(pts, pts[1:]))


def arc_segments(entity: DxfEntity, steps: int = 48):
    cx, cy = entity.center or (0.0, 0.0)
    radius = float(entity.radius or 0.0)
    start = math.radians(float(entity.start_angle or 0.0))
    end = math.radians(float(entity.end_angle or 0.0))
    if end < start:
        end += 2.0 * math.pi
    count = max(4, int(abs(end - start) / (2.0 * math.pi) * steps))
    pts = [
        (cx + radius * math.cos(start + (end - start) * i / count),
         cy + radius * math.sin(start + (end - start) * i / count))
        for i in range(count + 1)
    ]
    return list(zip(pts, pts[1:]))


def compare_segment_sets(input_segments, model_segments, scale: float) -> Dict[str, Any]:
    tolerance = max(scale * 0.02, 1e-6)
    input_samples = sample_segments(input_segments, tolerance)
    input_covered = coverage_ratio(input_samples, model_segments, tolerance)
    return {
        "tolerance": tolerance,
        "input_segments": len(input_segments),
        "model_segments": len(model_segments),
        "input_samples": len(input_samples),
        "coverage": round(input_covered, 4),
        "missing": round(1.0 - input_covered, 4),
        "bbox_error": bbox_error(input_segments, model_segments),
    }


def sample_segments(segments, spacing: float):
    samples = []
    for a, b in segments:
        ax, ay = float(a[0]), float(a[1])
        bx, by = float(b[0]), float(b[1])
        length = math.hypot(bx - ax, by - ay)
        count = max(1, int(math.ceil(length / max(spacing, 1e-6))))
        for idx in range(count + 1):
            t = idx / count
            samples.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    return samples


def coverage_ratio(samples, segments, tolerance: float) -> float:
    if not samples:
        return 1.0 if not segments else 0.0
    if not segments:
        return 0.0
    matched = 0
    tol_sq = tolerance * tolerance
    for point in samples:
        if any(point_segment_distance_sq(point, segment) <= tol_sq for segment in segments):
            matched += 1
    return matched / len(samples)


def point_segment_distance_sq(point, segment) -> float:
    px, py = point
    (ax, ay), (bx, by) = segment
    ax = float(ax)
    ay = float(ay)
    bx = float(bx)
    by = float(by)
    dx = bx - ax
    dy = by - ay
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    qx = ax + dx * t
    qy = ay + dy * t
    return (px - qx) ** 2 + (py - qy) ** 2


def bbox_error(input_segments, model_segments):
    input_bbox = segments_bbox(input_segments)
    model_bbox = segments_bbox(model_segments)
    if input_bbox is None or model_bbox is None:
        return None
    return [round(abs(float(a) - float(b)), 4) for a, b in zip(input_bbox, model_bbox)]


def segments_bbox(segments):
    if not segments:
        return None
    xs = [float(point[0]) for segment in segments for point in segment[:2]]
    ys = [float(point[1]) for segment in segments for point in segment[:2]]
    return [min(xs), min(ys), max(xs), max(ys)]
