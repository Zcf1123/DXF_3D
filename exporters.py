"""Artifact exporters: STEP, OBJ, PNG preview, model.json, generated_model.py.

PNG preview is rendered from the original DXF (three subplots, deterministic
matplotlib output) and therefore does NOT require FreeCADGui — it works
under headless `freecadcmd` as long as `matplotlib` is installed.

STEP / OBJ / model.json need an open FreeCAD document.
"""
from __future__ import annotations

import json
import math
import os
import textwrap
from typing import Any, Dict, List, Optional

from .dxf_loader import DxfEntity
from .feature_inference import Feature, _is_hidden_entity
from .view_classifier import ViewBundle


# ---------------------------------------------------------------------------
# STEP / OBJ / model.json (need FreeCAD)
# ---------------------------------------------------------------------------

def export_step(fcstd_path: str, step_path: str) -> str:
    import FreeCAD as App  # type: ignore
    import Part  # type: ignore
    doc = App.openDocument(fcstd_path)
    try:
        shape = _result_shape(doc)
        Part.export([shape], step_path)
    finally:
        App.closeDocument(doc.Name)
    return step_path


def export_obj(fcstd_path: str, obj_path: str,
               linear_deflection: float = 0.1) -> str:
    import FreeCAD as App  # type: ignore
    import Mesh  # type: ignore
    import MeshPart  # type: ignore
    doc = App.openDocument(fcstd_path)
    try:
        shape = _result_shape(doc)
        mesh = MeshPart.meshFromShape(Shape=shape,
                                      LinearDeflection=linear_deflection,
                                      AngularDeflection=0.5)
        Mesh.export([_wrap_mesh_object(doc, mesh)], obj_path)
    finally:
        App.closeDocument(doc.Name)
    return obj_path


def _result_shape(doc):
    result = doc.getObject("Result")
    shape = getattr(result, "Shape", None)
    if shape is None or shape.isNull():
        raise RuntimeError("Result solid not found in FCStd")
    if not shape.Solids:
        raise RuntimeError("Result object has no solid geometry")
    return shape


def _wrap_mesh_object(doc, mesh):
    obj = doc.addObject("Mesh::Feature", "ExportMesh")
    obj.Mesh = mesh
    return obj


def export_model_json(fcstd_path: str, json_path: str,
                      meta: Dict[str, Any]) -> str:
    import FreeCAD as App  # type: ignore
    doc = App.openDocument(fcstd_path)
    try:
        desc: Dict[str, Any] = {
            "source_fcstd": fcstd_path,
            "objects": [],
            "meta": meta,
        }
        for o in doc.Objects:
            entry: Dict[str, Any] = {
                "name": getattr(o, "Name", None),
                "type": getattr(o, "TypeId", None),
            }
            shp = getattr(o, "Shape", None)
            if shp is not None and not shp.isNull():
                bb = shp.BoundBox
                entry["bbox"] = {
                    "x_min": bb.XMin, "y_min": bb.YMin, "z_min": bb.ZMin,
                    "x_max": bb.XMax, "y_max": bb.YMax, "z_max": bb.ZMax,
                    "x_len": bb.XLength, "y_len": bb.YLength,
                    "z_len": bb.ZLength,
                }
                entry["num_solids"] = len(shp.Solids)
                entry["num_faces"] = len(shp.Faces)
                entry["num_edges"] = len(shp.Edges)
                try:
                    entry["volume"] = float(shp.Volume)
                except Exception:
                    entry["volume"] = None
            desc["objects"].append(entry)
        desc["solid_count"] = sum(1 for o in desc["objects"]
                                  if o.get("num_solids", 0) > 0)
    finally:
        App.closeDocument(doc.Name)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(desc, f, indent=2, ensure_ascii=False)
    return json_path


# ---------------------------------------------------------------------------
# PNG preview (matplotlib, FreeCAD-free)
# ---------------------------------------------------------------------------

def export_preview_png(bundles: List[ViewBundle], png_path: str) -> str:
    """Render the three DXF views (FRONT / RIGHT / TOP) into a 2x2 grid."""
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore

    fig, axes = plt.subplots(2, 2, figsize=(8, 8))
    layout = {"front": axes[0][0], "right": axes[0][1],
              "top": axes[1][0]}
    axes[1][1].axis("off")
    axes[1][1].text(0.05, 0.95, "(empty)\nbottom-right reserved",
                    fontsize=9, va="top", color="gray")

    for b in bundles:
        ax = layout.get(b.name)
        if ax is None:
            continue
        for e in b.entities:
            _draw_entity(ax, e)
        ax.set_aspect("equal")
        ax.set_title(b.name.upper(), fontsize=11)
        ax.grid(True, linestyle=":", alpha=0.4)

    fig.suptitle("DXF three views", fontsize=13)
    fig.tight_layout()
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


def export_normalized_views_png(projected: Dict[str, Any], png_path: str) -> str:
    """Render normalized FRONT / RIGHT / TOP views with per-view 0 origins."""
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore

    fig, axes = plt.subplots(2, 2, figsize=(8, 8))
    layout = {"front": axes[0][0], "right": axes[0][1],
              "top": axes[1][0]}
    axes[1][1].axis("off")
    axes[1][1].text(0.05, 0.95, "(empty)\nbottom-right reserved",
                    fontsize=9, va="top", color="gray")

    for name in ("front", "right", "top"):
        pv = projected.get(name)
        ax = layout[name]
        if pv is None:
            ax.axis("off")
            continue
        for e in pv.entities:
            _draw_entity(ax, e)
        ax.set_aspect("equal")
        ax.set_title(f"{name.upper()} (0-origin)", fontsize=11)
        x_max = max(float(pv.width), 1e-6)
        y_max = max(float(pv.height), 1e-6)
        pad = max(x_max, y_max, 1.0) * 0.04
        ax.set_xlim(-pad, x_max + pad)
        ax.set_ylim(-pad, y_max + pad)
        ax.grid(True, linestyle=":", alpha=0.4)

    fig.suptitle("DXF normalized three views", fontsize=13)
    fig.tight_layout()
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


def validate_projection_against_views(
    fcstd_path: str,
    projected: Dict[str, Any],
    features: Optional[List[Feature]] = None,
    report_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare final-model orthographic projections against input views.

    The report is intentionally geometric and lightweight: each input/model
    segment is sampled, then samples are matched by distance in normalized
    view coordinates. It is a diagnostic score, not a replacement for exact
    topological proof.
    """
    import FreeCAD as App  # type: ignore

    model_views = _feature_model_view_segments(features or [], include_cuts=False)
    if model_views is None:
        doc = App.openDocument(fcstd_path)
        try:
            shape = _result_shape(doc)
            model_views = {
                "front": _project_shape_edges(shape, "front"),
                "right": _project_shape_edges(shape, "right"),
                "top": _project_shape_edges(shape, "top"),
            }
        finally:
            App.closeDocument(doc.Name)

    view_reports: Dict[str, Any] = {}
    for view_name in ("front", "right", "top"):
        pv = projected.get(view_name)
        if pv is None:
            continue
        input_segments = _segments_from_entities(pv.entities)
        model_segments = _strip_segment_styles(_normalize_segments(model_views.get(view_name, [])))
        view_reports[view_name] = _compare_segment_sets(
            input_segments,
            model_segments,
            max(float(pv.width), float(pv.height), 1e-6),
        )

    overall_ok = all(report.get("status") == "OK" for report in view_reports.values())
    result = {"status": "OK" if overall_ok else "WARN", "views": view_reports}
    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
    return result


def export_model_views_png(fcstd_path: str, png_path: str,
                           features: Optional[List[Feature]] = None) -> str:
    """Render FRONT / RIGHT / TOP orthographic views from the final solid."""
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore
    from matplotlib.collections import LineCollection  # type: ignore
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # type: ignore
    import FreeCAD as App  # type: ignore

    views = _feature_model_view_segments(features or [], include_cuts=True)
    if views is None:
        doc = App.openDocument(fcstd_path)
        try:
            shape = _result_shape(doc)
            views = {
                "front": _project_shape_edges(shape, "front"),
                "right": _project_shape_edges(shape, "right"),
                "top": _project_shape_edges(shape, "top"),
            }
        finally:
            App.closeDocument(doc.Name)

    fig, axes = plt.subplots(2, 2, figsize=(8, 8))
    layout = {"front": axes[0][0], "right": axes[0][1],
              "top": axes[1][0]}
    axes[1][1].axis("off")
    axes[1][1].text(0.05, 0.95, "(empty)\nbottom-right reserved",
                    fontsize=9, va="top", color="gray")

    for name in ("front", "right", "top"):
        ax = layout[name]
        segs = _normalize_segments(views[name])
        if segs:
            solid_segs = [s[:2] for s in segs if len(s) < 3 or s[2] != "hidden"]
            hidden_segs = [s[:2] for s in segs if len(s) >= 3 and s[2] == "hidden"]
            if solid_segs:
                lc = LineCollection(solid_segs, colors="#1f3b73", linewidths=1.0,
                                    capstyle="round", joinstyle="round")
                ax.add_collection(lc)
            if hidden_segs:
                lc_hidden = LineCollection(hidden_segs, colors="#1f3b73", linewidths=0.9,
                                           linestyles="dashed",
                                           capstyle="round", joinstyle="round")
                ax.add_collection(lc_hidden)
            xs = [p[0] for s in segs for p in s[:2]]
            ys = [p[1] for s in segs for p in s[:2]]
            x_max = max(max(xs), 1e-6)
            y_max = max(max(ys), 1e-6)
            pad = max(x_max, y_max, 1.0) * 0.04
            ax.set_xlim(-pad, x_max + pad)
            ax.set_ylim(-pad, y_max + pad)
        else:
            ax.set_xlim(0.0, 1.0)
            ax.set_ylim(0.0, 1.0)
        ax.set_aspect("equal")
        ax.set_title(f"{name.upper()} model (0-origin)", fontsize=11)
        ax.grid(True, linestyle=":", alpha=0.4)

    fig.suptitle("Model orthographic three views", fontsize=13)
    fig.tight_layout()
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


def _feature_model_view_segments(features: List[Feature], include_cuts: bool = False):
    if not features:
        return None
    if any(f.kind == "profile_cut" for f in features) and not include_cuts:
        return None
    stack = next((f for f in features if f.kind == "cylinder_stack"), None)
    if stack is not None:
        return _cylinder_stack_view_segments(stack, features)
    base = next((f for f in features if f.kind == "extrude_profile"), None)
    if base is None:
        return None
    params = base.params
    edges = params.get("edges", [])
    if params.get("plane") != "XY" or len(edges) != 1 or edges[0].get("kind") != "CIRCLE":
        return _generic_extrude_view_segments(base, features if include_cuts else [])
    circle = edges[0]
    cx, cy = circle.get("center", [0.0, 0.0])
    radius = float(circle.get("radius", 0.0) or 0.0)
    height = float(params.get("depth", 0.0) or 0.0)
    if radius <= 0 or height <= 0:
        return None
    front = [
        ((cx - radius, 0.0), (cx + radius, 0.0)),
        ((cx + radius, 0.0), (cx + radius, height)),
        ((cx + radius, height), (cx - radius, height)),
        ((cx - radius, height), (cx - radius, 0.0)),
    ]
    right = [
        ((cy - radius, 0.0), (cy + radius, 0.0)),
        ((cy + radius, 0.0), (cy + radius, height)),
        ((cy + radius, height), (cy - radius, height)),
        ((cy - radius, height), (cy - radius, 0.0)),
    ]
    top = _circle_segments(cx, cy, radius)
    for feature in features:
        if feature.kind != "hole":
            continue
        hp = feature.params
        if hp.get("axis") != "Z":
            continue
        hx, hy, hz = hp.get("position", [cx, cy, 0.0])
        hr = float(hp.get("radius", 0.0) or 0.0)
        length = float(hp.get("through_length", height) or 0.0)
        if hr <= 0 or length <= 0:
            continue
        z0 = float(hz)
        z1 = z0 + length
        front.extend([
            ((hx - hr, z0), (hx - hr, z1), "hidden"),
            ((hx + hr, z0), (hx + hr, z1), "hidden"),
        ])
        right.extend([
            ((hy - hr, z0), (hy - hr, z1), "hidden"),
            ((hy + hr, z0), (hy + hr, z1), "hidden"),
        ])
        if hp.get("blind"):
            front.append(((hx - hr, z0), (hx + hr, z0), "hidden"))
            right.append(((hy - hr, z0), (hy + hr, z0), "hidden"))
        else:
            front.append(((hx - hr, z1), (hx + hr, z1), "hidden"))
            right.append(((hy - hr, z1), (hy + hr, z1), "hidden"))
        top.extend(_circle_segments(float(hx), float(hy), hr))
    return {"front": front, "right": right, "top": top}


def _generic_extrude_view_segments(base: Feature, features: Optional[List[Feature]] = None):
    params = base.params
    plane = params.get("plane")
    depth = float(params.get("depth", 0.0) or 0.0)
    profile = _profile_edge_segments(params.get("edges", []))
    if not profile or depth <= 0:
        return None
    bbox = _segments_bbox(profile)
    if bbox is None:
        return None
    u0, v0, u1, v1 = bbox

    def rect(a0, b0, a1, b1):
        return [
            ((a0, b0), (a1, b0)),
            ((a1, b0), (a1, b1)),
            ((a1, b1), (a0, b1)),
            ((a0, b1), (a0, b0)),
        ]

    if plane == "XY":
        views = {
            "front": rect(u0, 0.0, u1, depth),
            "right": rect(v0, 0.0, v1, depth),
            "top": profile,
        }
    elif plane == "XZ":
        views = {
            "front": profile,
            "right": rect(0.0, v0, depth, v1),
            "top": rect(u0, 0.0, u1, depth),
        }
    elif plane == "YZ":
        views = {
            "front": rect(0.0, v0, depth, v1),
            "right": profile,
            "top": rect(0.0, u0, depth, u1),
        }
    else:
        return None
    _overlay_feature_segments(views, features or [])
    return views


def _overlay_feature_segments(views, features: List[Feature]) -> None:
    for feature in features:
        if feature.kind == "hole":
            _overlay_hole_segments(views, feature.params)
        elif feature.kind == "profile_cut":
            _overlay_profile_cut_segments(views, feature.params)


def _overlay_hole_segments(views, params) -> None:
    radius = float(params.get("radius", 0.0) or 0.0)
    if radius <= 0:
        return
    x, y, z = [float(v) for v in params.get("position", [0.0, 0.0, 0.0])]
    length = float(params.get("through_length", 0.0) or 0.0)
    axis = params.get("axis")
    if axis == "Y":
        views.setdefault("front", []).extend(_circle_segments(x, z, radius, steps=96))
        y0, y1 = y, y + length
        views.setdefault("top", []).extend([
            ((x - radius, y0), (x - radius, y1), "hidden"),
            ((x + radius, y0), (x + radius, y1), "hidden"),
        ])
        views.setdefault("right", []).extend([
            ((y0, z - radius), (y1, z - radius), "hidden"),
            ((y0, z + radius), (y1, z + radius), "hidden"),
        ])
    elif axis == "Z":
        views.setdefault("top", []).extend(_circle_segments(x, y, radius, steps=96))
        z0, z1 = z, z + length
        views.setdefault("front", []).extend([
            ((x - radius, z0), (x - radius, z1), "hidden"),
            ((x + radius, z0), (x + radius, z1), "hidden"),
        ])
        views.setdefault("right", []).extend([
            ((y - radius, z0), (y - radius, z1), "hidden"),
            ((y + radius, z0), (y + radius, z1), "hidden"),
        ])
    elif axis == "X":
        views.setdefault("right", []).extend(_circle_segments(y, z, radius, steps=96))
        x0, x1 = x, x + length
        views.setdefault("top", []).extend([
            ((x0, y - radius), (x1, y - radius), "hidden"),
            ((x0, y + radius), (x1, y + radius), "hidden"),
        ])
        views.setdefault("front", []).extend([
            ((x0, z - radius), (x1, z - radius), "hidden"),
            ((x0, z + radius), (x1, z + radius), "hidden"),
        ])


def _overlay_profile_cut_segments(views, params) -> None:
    plane = params.get("plane")
    profile = _profile_edge_segments(params.get("edges", []))
    if not profile:
        return
    offset = float(params.get("offset", 0.0) or 0.0)
    depth = float(params.get("depth", 0.0) or 0.0)
    bbox = _segments_bbox(profile)
    if bbox is None:
        return
    u0, v0, u1, v1 = bbox
    if plane == "XY":
        views.setdefault("top", []).extend(profile)
        for z in (offset, offset + depth):
            views.setdefault("front", []).extend([
                ((u0, z), (u1, z), "hidden"),
                ((u0, z), (u0, z), "hidden"),
                ((u1, z), (u1, z), "hidden"),
            ])
            views.setdefault("right", []).extend([
                ((v0, z), (v1, z), "hidden"),
            ])
    elif plane == "XZ":
        views.setdefault("front", []).extend(profile)
        for y in (offset, offset + depth):
            views.setdefault("top", []).extend([
                ((u0, y), (u1, y), "hidden"),
            ])
            views.setdefault("right", []).extend([
                ((y, v0), (y, v1), "hidden"),
            ])
    elif plane == "YZ":
        views.setdefault("right", []).extend(profile)
        for x in (offset, offset + depth):
            views.setdefault("top", []).extend([
                ((x, u0), (x, u1), "hidden"),
            ])
            views.setdefault("front", []).extend([
                ((x, v0), (x, v1), "hidden"),
            ])


def _profile_edge_segments(edges):
    segments = []
    for edge in edges:
        kind = edge.get("kind")
        if kind == "LINE":
            p0 = edge.get("p0")
            p1 = edge.get("p1")
            if p0 is not None and p1 is not None:
                segments.append(((float(p0[0]), float(p0[1])),
                                 (float(p1[0]), float(p1[1]))))
        elif kind == "CIRCLE":
            cx, cy = edge.get("center", [0.0, 0.0])
            radius = float(edge.get("radius", 0.0) or 0.0)
            if radius > 0:
                segments.extend(_circle_segments(float(cx), float(cy), radius))
        elif kind == "ARC":
            cx, cy = edge.get("center", [0.0, 0.0])
            radius = float(edge.get("radius", 0.0) or 0.0)
            start = math.radians(float(edge.get("start_angle", 0.0) or 0.0))
            end = math.radians(float(edge.get("end_angle", 0.0) or 0.0))
            if end < start:
                end += 2.0 * math.pi
            if radius > 0:
                steps = max(8, int(abs(end - start) / (2.0 * math.pi) * 96))
                points = [
                    (float(cx) + radius * math.cos(start + (end - start) * i / steps),
                     float(cy) + radius * math.sin(start + (end - start) * i / steps))
                    for i in range(steps + 1)
                ]
                segments.extend(zip(points, points[1:]))
    return segments


def _cylinder_stack_view_segments(stack: Feature, features: List[Feature]):
    params = stack.params
    if params.get("axis") != "Z":
        return None
    cx, cy = params.get("center", [0.0, 0.0])
    segments = params.get("segments", [])
    if not segments:
        return None
    front = []
    right = []
    top = []
    max_radius = 0.0
    for segment in segments:
        z0 = float(segment.get("z_min", 0.0))
        z1 = float(segment.get("z_max", z0))
        radius = float(segment.get("radius", 0.0))
        if radius <= 0 or z1 <= z0:
            continue
        max_radius = max(max_radius, radius)
        front.extend([
            ((cx - radius, z0), (cx + radius, z0)),
            ((cx + radius, z0), (cx + radius, z1)),
            ((cx + radius, z1), (cx - radius, z1)),
            ((cx - radius, z1), (cx - radius, z0)),
        ])
        right.extend([
            ((cy - radius, z0), (cy + radius, z0)),
            ((cy + radius, z0), (cy + radius, z1)),
            ((cy + radius, z1), (cy - radius, z1)),
            ((cy - radius, z1), (cy - radius, z0)),
        ])
    if max_radius <= 0:
        return None
    top.extend(_circle_segments(float(cx), float(cy), max_radius))
    for radius in sorted({round(float(s.get("radius", 0.0)), 6) for s in segments}):
        if radius > 0 and abs(radius - max_radius) > 1e-6:
            top.extend((a, b, "hidden") for a, b in _circle_segments(float(cx), float(cy), radius))
    return {"front": front, "right": right, "top": top}


def _circle_segments(cx: float, cy: float, radius: float, steps: int = 96):
    pts = [
        (cx + radius * math.cos(2.0 * math.pi * i / steps),
         cy + radius * math.sin(2.0 * math.pi * i / steps))
        for i in range(steps + 1)
    ]
    return list(zip(pts, pts[1:]))


def _segments_from_entities(entities: List[DxfEntity]):
    segments = []
    for entity in entities:
        if entity.kind == "LINE" and len(entity.points) >= 2:
            segments.append((tuple(entity.points[0]), tuple(entity.points[1])))
        elif entity.kind == "CIRCLE" and entity.center is not None and entity.radius is not None:
            cx, cy = entity.center
            segments.extend(_circle_segments(float(cx), float(cy), float(entity.radius)))
        elif entity.kind == "ARC" and entity.center is not None and entity.radius is not None:
            segments.extend(_arc_segments(entity))
        elif entity.kind in ("LWPOLYLINE", "POLYLINE") and len(entity.points) >= 2:
            points = entity.points
            closed = bool(entity.extra.get("closed", False))
            end = len(points) if closed else len(points) - 1
            for idx in range(end):
                segments.append((tuple(points[idx]), tuple(points[(idx + 1) % len(points)])))
    return segments


def _arc_segments(entity: DxfEntity, steps: int = 48):
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


def _strip_segment_styles(segments):
    return [segment[:2] for segment in segments]


def _compare_segment_sets(input_segments, model_segments, scale: float) -> Dict[str, Any]:
    tolerance = max(scale * 0.02, 1e-6)
    input_samples = _sample_segments(input_segments, tolerance)
    model_samples = _sample_segments(model_segments, tolerance)
    input_covered = _coverage_ratio(input_samples, model_segments, tolerance)
    model_matched = _coverage_ratio(model_samples, input_segments, tolerance)
    bbox_error = _bbox_error(input_segments, model_segments)
    status = "OK" if input_covered >= 0.88 and model_matched >= 0.75 else "WARN"
    return {
        "status": status,
        "tolerance": tolerance,
        "input_segments": len(input_segments),
        "model_segments": len(model_segments),
        "input_samples": len(input_samples),
        "model_samples": len(model_samples),
        "input_coverage": round(input_covered, 4),
        "model_match": round(model_matched, 4),
        "model_extra_ratio": round(1.0 - model_matched, 4),
        "bbox_error": bbox_error,
        "unmatched_input_segments": _unmatched_segments(input_segments, model_segments, tolerance),
    }


def _sample_segments(segments, spacing: float):
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


def _coverage_ratio(samples, segments, tolerance: float) -> float:
    if not samples:
        return 1.0 if not segments else 0.0
    if not segments:
        return 0.0
    matched = 0
    tol_sq = tolerance * tolerance
    for point in samples:
        if any(_point_segment_distance_sq(point, segment) <= tol_sq for segment in segments):
            matched += 1
    return matched / len(samples)


def _unmatched_segments(input_segments, model_segments, tolerance: float, limit: int = 12):
    unmatched = []
    for index, segment in enumerate(input_segments):
        samples = _sample_segments([segment], tolerance)
        if not samples:
            continue
        coverage = _coverage_ratio(samples, model_segments, tolerance)
        if coverage >= 0.75:
            continue
        (x0, y0), (x1, y1) = segment
        unmatched.append({
            "index": index,
            "p0": [round(float(x0), 4), round(float(y0), 4)],
            "p1": [round(float(x1), 4), round(float(y1), 4)],
            "coverage": round(coverage, 4),
        })
    unmatched.sort(key=lambda item: item["coverage"])
    return unmatched[:limit]


def _point_segment_distance_sq(point, segment) -> float:
    px, py = point
    (ax, ay), (bx, by) = segment
    ax = float(ax); ay = float(ay); bx = float(bx); by = float(by)
    dx = bx - ax
    dy = by - ay
    denom = dx * dx + dy * dy
    if denom <= 1e-12:
        return (px - ax) ** 2 + (py - ay) ** 2
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denom))
    qx = ax + dx * t
    qy = ay + dy * t
    return (px - qx) ** 2 + (py - qy) ** 2


def _bbox_error(input_segments, model_segments):
    input_bbox = _segments_bbox(input_segments)
    model_bbox = _segments_bbox(model_segments)
    if input_bbox is None or model_bbox is None:
        return None
    return [round(abs(float(a) - float(b)), 4) for a, b in zip(input_bbox, model_bbox)]


def _segments_bbox(segments):
    if not segments:
        return None
    xs = [float(point[0]) for segment in segments for point in segment[:2]]
    ys = [float(point[1]) for segment in segments for point in segment[:2]]
    return [min(xs), min(ys), max(xs), max(ys)]


def _project_shape_edges(shape, view_name: str):
    if _is_sphere_shape(shape):
        return _project_sphere_outline(shape, view_name)
    segs = []
    model_scale = max(
        float(shape.BoundBox.XLength),
        float(shape.BoundBox.YLength),
        float(shape.BoundBox.ZLength),
    )
    artifact_tol = max(
        model_scale * 0.075,
        1e-6,
    )
    for edge in shape.Edges:
        try:
            if _is_full_height_internal_seam_line(edge, view_name, shape):
                continue
            if _is_short_projected_artifact_edge(edge, view_name, artifact_tol):
                continue
            length = max(float(edge.Length), 1e-9)
            points = edge.discretize(max(2, int(length * 12)))
        except Exception:
            continue
        for i in range(len(points) - 1):
            a = _project_point(points[i], view_name)
            b = _project_point(points[i + 1], view_name)
            if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
                continue
            segs.append((a, b))
    return segs


def _is_sphere_shape(shape) -> bool:
    if not getattr(shape, "Solids", None):
        return False
    bb = shape.BoundBox
    scale = max(float(bb.XLength), float(bb.YLength), float(bb.ZLength), 1.0)
    if abs(float(bb.XLength) - float(bb.YLength)) > scale * 0.02:
        return False
    if abs(float(bb.XLength) - float(bb.ZLength)) > scale * 0.02:
        return False
    for face in getattr(shape, "Faces", []):
        surface_name = type(face.Surface).__name__ if hasattr(face, "Surface") else ""
        if surface_name == "Sphere":
            return True
    return False


def _project_sphere_outline(shape, view_name: str):
    import math

    bb = shape.BoundBox
    center = (
        (float(bb.XMin) + float(bb.XMax)) * 0.5,
        (float(bb.YMin) + float(bb.YMax)) * 0.5,
        (float(bb.ZMin) + float(bb.ZMax)) * 0.5,
    )
    radius = min(float(bb.XLength), float(bb.YLength), float(bb.ZLength)) * 0.5
    segs = []
    points = []
    for i in range(97):
        t = 2.0 * math.pi * i / 96.0
        if view_name == "front":
            points.append((center[0] + radius * math.cos(t),
                           center[2] + radius * math.sin(t)))
        elif view_name == "right":
            points.append((center[1] + radius * math.cos(t),
                           center[2] + radius * math.sin(t)))
        elif view_name == "top":
            points.append((center[0] + radius * math.cos(t),
                           center[1] + radius * math.sin(t)))
        else:
            raise ValueError(f"unknown view name: {view_name}")
    for a, b in zip(points, points[1:]):
        segs.append((a, b))
    return segs


def _is_full_height_internal_seam_line(edge, view_name: str, shape) -> bool:
    if view_name not in {"front", "right"} or len(edge.Vertexes) != 2:
        return False
    curve_name = type(edge.Curve).__name__ if hasattr(edge, "Curve") else ""
    if curve_name != "Line":
        return False
    p0 = edge.Vertexes[0].Point
    p1 = edge.Vertexes[1].Point
    chord_3d = p0.distanceToPoint(p1)
    if chord_3d <= 1e-9:
        return False
    if abs(float(edge.Length) - chord_3d) > max(1e-6, chord_3d * 1e-5):
        return False
    bb = shape.BoundBox
    span_x = max(float(bb.XLength), 1.0)
    span_y = max(float(bb.YLength), 1.0)
    span_z = max(float(bb.ZLength), 1.0)
    if abs(float(p0.x) - float(p1.x)) > span_x * 0.001:
        return False
    if abs(float(p0.y) - float(p1.y)) > span_y * 0.001:
        return False
    z_min = float(bb.ZMin)
    z_max = float(bb.ZMax)
    touches_bottom = min(abs(float(p0.z) - z_min), abs(float(p1.z) - z_min)) <= span_z * 0.02
    touches_top = min(abs(float(p0.z) - z_max), abs(float(p1.z) - z_max)) <= span_z * 0.02
    if not (touches_bottom and touches_top):
        return False
    if view_name == "front":
        projected_x = float(p0.x)
        return (projected_x - float(bb.XMin)) > span_x * 0.05 and (float(bb.XMax) - projected_x) > span_x * 0.05
    projected_y = float(p0.y)
    return (projected_y - float(bb.YMin)) > span_y * 0.05 and (float(bb.YMax) - projected_y) > span_y * 0.05


def _is_internal_right_center_line(edge, view_name: str, shape) -> bool:
    if view_name != "right" or len(edge.Vertexes) != 2:
        return False
    p0 = edge.Vertexes[0].Point
    p1 = edge.Vertexes[1].Point
    chord_3d = p0.distanceToPoint(p1)
    if chord_3d <= 1e-9:
        return False
    if abs(float(edge.Length) - chord_3d) > max(1e-6, chord_3d * 1e-5):
        return False
    # RIGHT view projects (world Y, world Z). The nut's hex vertex at the
    # centre Y creates an internal vertical seam; it is not a silhouette.
    span_y = max(float(shape.BoundBox.YLength), 1.0)
    center_y = (float(shape.BoundBox.YMin) + float(shape.BoundBox.YMax)) * 0.5
    if abs(float(p0.y) - center_y) > span_y * 0.01:
        return False
    if abs(float(p1.y) - center_y) > span_y * 0.01:
        return False
    z_min = float(shape.BoundBox.ZMin)
    z_max = float(shape.BoundBox.ZMax)
    touches_bottom = min(abs(float(p0.z) - z_min), abs(float(p1.z) - z_min)) <= span_y * 0.01
    touches_top = min(abs(float(p0.z) - z_max), abs(float(p1.z) - z_max)) <= span_y * 0.01
    if touches_bottom and touches_top:
        return False
    return abs(float(p0.z) - float(p1.z)) > span_y * 0.05


def _is_short_3d_artifact_edge(edge, shape) -> bool:
    curve_name = type(edge.Curve).__name__ if hasattr(edge, "Curve") else ""
    if curve_name != "Circle":
        return False
    scale = max(
        float(shape.BoundBox.XLength),
        float(shape.BoundBox.YLength),
        float(shape.BoundBox.ZLength),
    )
    return float(edge.Length) <= max(scale * 0.09, 1e-6)


def _is_internal_3d_center_seam(edge, shape) -> bool:
    if len(edge.Vertexes) != 2:
        return False
    curve_name = type(edge.Curve).__name__ if hasattr(edge, "Curve") else ""
    if curve_name != "Line":
        return False
    p0 = edge.Vertexes[0].Point
    p1 = edge.Vertexes[1].Point
    chord_3d = p0.distanceToPoint(p1)
    if chord_3d <= 1e-9:
        return False
    if abs(float(edge.Length) - chord_3d) > max(1e-6, chord_3d * 1e-5):
        return False
    bb = shape.BoundBox
    span_x = max(float(bb.XLength), 1.0)
    span_y = max(float(bb.YLength), 1.0)
    span_z = max(float(bb.ZLength), 1.0)
    center_y = (float(bb.YMin) + float(bb.YMax)) * 0.5
    if abs(float(p0.y) - center_y) > span_y * 0.01:
        return False
    if abs(float(p1.y) - center_y) > span_y * 0.01:
        return False
    if abs(float(p0.x) - float(p1.x)) > span_x * 0.01:
        return False
    if abs(float(p0.z) - float(p1.z)) <= span_z * 0.1:
        return False
    # Keep silhouette vertex lines at the left/right extrema; remove only
    # internal seams such as the boolean split through the middle of a face.
    x = float(p0.x)
    return (x - float(bb.XMin)) > span_x * 0.1 and (float(bb.XMax) - x) > span_x * 0.1


def _is_short_projected_artifact_edge(edge, view_name: str, tol: float) -> bool:
    if len(edge.Vertexes) != 2:
        return False
    p0 = edge.Vertexes[0].Point
    p1 = edge.Vertexes[1].Point
    chord_3d = p0.distanceToPoint(p1)
    if chord_3d <= 1e-9:
        return True
    a = _project_point(p0, view_name)
    b = _project_point(p1, view_name)
    projected = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
    if view_name == "top":
        return False
    curve_name = type(edge.Curve).__name__ if hasattr(edge, "Curve") else ""
    if curve_name == "Circle" and float(edge.Length) <= tol * 1.1:
        return 1e-9 < projected <= tol
    if abs(float(edge.Length) - chord_3d) > max(1e-6, chord_3d * 1e-5):
        return False
    return 1e-9 < projected <= tol


def _project_point(point, view_name: str):
    if view_name == "front":
        return (float(point.x), float(point.z))
    if view_name == "top":
        return (float(point.x), float(point.y))
    if view_name == "right":
        return (float(point.y), float(point.z))
    raise ValueError(f"unknown view name: {view_name}")


def _normalize_segments(segs):
    if not segs:
        return []
    xs = [p[0] for s in segs for p in s[:2]]
    ys = [p[1] for s in segs for p in s[:2]]
    xmin = min(xs)
    ymin = min(ys)
    normalized = []
    for seg in segs:
        a, b = seg[:2]
        item = ((a[0] - xmin, a[1] - ymin),
                (b[0] - xmin, b[1] - ymin))
        if len(seg) >= 3:
            normalized.append((item[0], item[1], seg[2]))
        else:
            normalized.append(item)
    return normalized


def export_iso_overview_png(fcstd_path: str, png_path: str) -> str:
    """Render an isometric edge-only overview of the solid.

    Output style: white background, black wireframe lines, no axes / ticks /
    title — mirroring `pic_to_3d/qwen/outputs/nut02/nut_qwen.png`.
    Edges are discretized and projected onto a 2D plane along an isometric
    view direction; we draw plain 2D line segments with matplotlib.
    """
    import math
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore
    from matplotlib.collections import LineCollection, PolyCollection  # type: ignore
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection  # type: ignore
    import FreeCAD as App  # type: ignore
    import Part  # type: ignore

    doc = App.openDocument(fcstd_path)
    try:
        shapes = []
        result = doc.getObject("Result")
        result_shape = getattr(result, "Shape", None) if result is not None else None
        if result_shape is not None and not result_shape.isNull() and result_shape.Solids:
            shapes.append(result_shape)
        for o in doc.Objects:
            if shapes:
                break
            s = getattr(o, "Shape", None)
            if s is not None and not s.isNull() and s.Solids:
                shapes.append(s)
        if not shapes:
            for o in doc.Objects:
                s = getattr(o, "Shape", None)
                if s is not None and not s.isNull():
                    shapes.append(s)
        if not shapes:
            raise RuntimeError("no usable shape in fcstd")
        compound = Part.Compound(shapes)
        bb = compound.BoundBox
        bbox_vals = (
            float(bb.XMin), float(bb.XMax),
            float(bb.YMin), float(bb.YMax),
            float(bb.ZMin), float(bb.ZMax),
        )

        # Isometric-ish view basis (same convention as qwen renderer).
        def _norm(v):
            L = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
            return (v[0] / L, v[1] / L, v[2] / L)

        d = _norm((1.0, -1.0, 0.7))
        up = (0.0, 0.0, 1.0)
        u = _norm((up[1] * d[2] - up[2] * d[1],
                   up[2] * d[0] - up[0] * d[2],
                   up[0] * d[1] - up[1] * d[0]))
        v = (d[1] * u[2] - d[2] * u[1],
             d[2] * u[0] - d[0] * u[2],
             d[0] * u[1] - d[1] * u[0])

        def proj(p):
            return (p.x * u[0] + p.y * u[1] + p.z * u[2],
                    p.x * v[0] + p.y * v[1] + p.z * v[2])

        def depth(p):
            return p.x * d[0] + p.y * d[1] + p.z * d[2]

        face_polys = []
        face_verts = []
        try:
            verts, facets = compound.tessellate(0.01)
        except Exception:
            verts, facets = [], []
        for facet in facets:
            if len(facet) < 3:
                continue
            points = [verts[int(idx)] for idx in facet]
            p0, p1, p2 = points[0], points[1], points[2]
            ax1 = p1.x - p0.x; ay1 = p1.y - p0.y; az1 = p1.z - p0.z
            ax2 = p2.x - p0.x; ay2 = p2.y - p0.y; az2 = p2.z - p0.z
            nx = ay1 * az2 - az1 * ay2
            ny = az1 * ax2 - ax1 * az2
            nz = ax1 * ay2 - ay1 * ax2
            normal_len = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            shade = abs((nx * d[0] + ny * d[1] + nz * d[2]) / normal_len)
            gray = 0.68 - 0.16 * shade
            face_polys.append((
                sum(depth(point) for point in points) / len(points),
                [proj(point) for point in points],
                (gray, gray, gray, 1.0),
            ))
            face_verts.append((
                [(float(point.x), float(point.y), float(point.z)) for point in points],
                (gray, gray, gray, 1.0),
            ))

        segs = []
        if _is_sphere_shape(compound):
            import FreeCAD as App  # type: ignore
            bb = compound.BoundBox
            center = App.Vector(
                (float(bb.XMin) + float(bb.XMax)) * 0.5,
                (float(bb.YMin) + float(bb.YMax)) * 0.5,
                (float(bb.ZMin) + float(bb.ZMax)) * 0.5,
            )
            radius = min(float(bb.XLength), float(bb.YLength), float(bb.ZLength)) * 0.5
            circles = []
            for plane in ("xy", "xz", "yz"):
                pts = []
                for i in range(145):
                    t = 2.0 * math.pi * i / 144.0
                    if plane == "xy":
                        pts.append(App.Vector(center.x + radius * math.cos(t),
                                              center.y + radius * math.sin(t),
                                              center.z))
                    elif plane == "xz":
                        pts.append(App.Vector(center.x + radius * math.cos(t),
                                              center.y,
                                              center.z + radius * math.sin(t)))
                    else:
                        pts.append(App.Vector(center.x,
                                              center.y + radius * math.cos(t),
                                              center.z + radius * math.sin(t)))
                circles.append(pts)
            for pts in circles:
                for i in range(len(pts) - 1):
                    segs.append((proj(pts[i]), proj(pts[i + 1])))
        else:
            bb = compound.BoundBox
            model_diag = max(
                math.sqrt(float(bb.XLength) ** 2 + float(bb.YLength) ** 2 + float(bb.ZLength) ** 2),
                1e-9,
            )
            for edge in compound.Edges:
                if _is_short_3d_artifact_edge(edge, compound):
                    continue
                if _is_internal_3d_center_seam(edge, compound):
                    continue
                curve = getattr(edge, "Curve", None)
                curve_type = getattr(curve, "TypeId", "")
                is_line = curve_type == "Part::GeomLine"
                if is_line:
                    n = 2
                else:
                    n = max(32, min(160, int((float(edge.Length) / model_diag) * 160)))
                try:
                    pts = edge.discretize(Number=n)
                except Exception:
                    try:
                        pts = edge.discretize(n)
                    except Exception:
                        continue
                for i in range(len(pts) - 1):
                    a = proj(pts[i])
                    b = proj(pts[i + 1])
                    segs.append((a, b))
        if not segs:
            raise RuntimeError("no edges to project")
    finally:
        App.closeDocument(doc.Name)

    if face_verts:
        xmin3, xmax3, ymin3, ymax3, zmin3, zmax3 = bbox_vals
        sx = max(xmax3 - xmin3, 1e-3)
        sy = max(ymax3 - ymin3, 1e-3)
        sz = max(zmax3 - zmin3, 1e-3)
        span = max(sx, sy, sz)
        cx = (xmin3 + xmax3) * 0.5
        cy = (ymin3 + ymax3) * 0.5
        cz = (zmin3 + zmax3) * 0.5
        margin3 = span * 0.02

        fig = plt.figure(figsize=(10, 7), facecolor="white")
        ax3 = fig.add_subplot(111, projection="3d")
        ax3.set_position([0.0, 0.0, 1.0, 1.0])
        ax3.set_facecolor("white")
        collection = Poly3DCollection(
            [poly for poly, _ in face_verts],
            facecolors=[color for _, color in face_verts],
            edgecolors="none",
            linewidths=0.0,
            antialiaseds=True,
            zsort="average",
        )
        ax3.add_collection3d(collection)
        ax3.set_xlim(cx - span * 0.5 - margin3, cx + span * 0.5 + margin3)
        ax3.set_ylim(cy - span * 0.5 - margin3, cy + span * 0.5 + margin3)
        ax3.set_zlim(cz - span * 0.5 - margin3, cz + span * 0.5 + margin3)
        try:
            ax3.set_box_aspect((sx, sy, sz), zoom=1.65)
            ax3.set_proj_type("ortho")
        except Exception:
            try:
                ax3.set_box_aspect((sx, sy, sz))
                ax3.dist = 6
            except Exception:
                pass
        ax3.view_init(elev=22, azim=-55)
        ax3.set_axis_off()
        fig.savefig(png_path, dpi=150, facecolor="white", bbox_inches="tight", pad_inches=0)
        plt.close(fig)
        return png_path

    if face_polys:
        xs = [p[0] for _, poly, _ in face_polys for p in poly]
        ys = [p[1] for _, poly, _ in face_polys for p in poly]
    else:
        xs = [p[0] for s in segs for p in s]
        ys = [p[1] for s in segs for p in s]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    W = max(xmax - xmin, 1e-3)
    H = max(ymax - ymin, 1e-3)
    margin = 0.08 * max(W, H)

    fig, ax = plt.subplots(figsize=(9, 9 * H / W))
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    if face_polys:
        ordered = sorted(face_polys, key=lambda item: item[0])
        pc = PolyCollection([poly for _, poly, _ in ordered],
                            facecolors=[color for _, _, color in ordered],
                            edgecolors="none", linewidths=0.0,
                            antialiaseds=True)
        ax.add_collection(pc)
    else:
        lc = LineCollection(segs, colors="black", linewidths=0.8,
                            capstyle="round", joinstyle="round")
        ax.add_collection(lc)
    ax.set_xlim(xmin - margin, xmax + margin)
    ax.set_ylim(ymin - margin, ymax + margin)
    ax.set_aspect("equal")
    ax.set_axis_off()
    fig.tight_layout(pad=0)
    fig.savefig(png_path, dpi=150, facecolor="white")
    plt.close(fig)
    return png_path



def _draw_entity(ax, e: DxfEntity) -> None:
    if _is_hidden_entity(e):
        color, lw, ls = "#1f3b73", 0.8, "--"
    else:
        color, lw, ls = "#1f3b73", 1.0, "-"
    if e.kind == "LINE" and len(e.points) == 2:
        (x0, y0), (x1, y1) = e.points
        ax.plot([x0, x1], [y0, y1], color=color, linewidth=lw, linestyle=ls)
    elif e.kind == "CIRCLE" and e.center is not None and e.radius is not None:
        import numpy as np  # type: ignore
        t = np.linspace(0, 2 * np.pi, 64)
        cx, cy = e.center
        ax.plot(cx + e.radius * np.cos(t),
                cy + e.radius * np.sin(t),
                color="#aa3333", linewidth=lw, linestyle=ls)
    elif e.kind == "ARC" and e.center is not None and e.radius is not None:
        import numpy as np  # type: ignore
        sa = (e.start_angle or 0.0) * np.pi / 180.0
        ea = (e.end_angle or 0.0) * np.pi / 180.0
        if ea < sa:
            ea += 2 * np.pi
        t = np.linspace(sa, ea, 32)
        cx, cy = e.center
        ax.plot(cx + e.radius * np.cos(t),
                cy + e.radius * np.sin(t),
                color=color, linewidth=lw, linestyle=ls)
    elif e.kind in ("LWPOLYLINE", "POLYLINE") and len(e.points) >= 2:
        xs = [p[0] for p in e.points]
        ys = [p[1] for p in e.points]
        if e.extra.get("closed"):
            xs.append(xs[0]); ys.append(ys[0])
        ax.plot(xs, ys, color=color, linewidth=lw, linestyle=ls)


# ---------------------------------------------------------------------------
# generated_model.py — standalone reproduction script
# ---------------------------------------------------------------------------

def export_generated_python(features: List[Feature], py_path: str,
                            base_name: str, fcstd_path: str) -> str:
    feats_lit = json.dumps([f.to_dict() for f in features],
                           indent=2, ensure_ascii=False)
    body = textwrap.dedent(f'''\
        # -*- coding: utf-8 -*-
        # Auto-generated by DXF_3D pipeline.
        # Re-run with:
        #     freecadcmd generated_model.py
        # or paste into the FreeCAD Python console.
        import os, math, json
        import FreeCAD as App
        import Part

        BASE_NAME = "{base_name}"
        FCSTD_PATH = r"{fcstd_path}"

        FEATURES = json.loads(r"""{feats_lit}""")

        def _extrude_profile(params):
            edges_def = params["edges"]
            depth = float(params["depth"])
            plane = params.get("plane", "XZ")
            offset = float(params.get("offset", 0.0) or 0.0)
            if plane == "XY":
                lift = lambda u, v: App.Vector(float(u), float(v), offset)
                ev = App.Vector(0.0, 0.0, depth)
            elif plane == "XZ":
                lift = lambda u, v: App.Vector(float(u), offset, float(v))
                ev = App.Vector(0.0, depth, 0.0)
            elif plane == "YZ":
                lift = lambda u, v: App.Vector(offset, float(u), float(v))
                ev = App.Vector(depth, 0.0, 0.0)
            else:
                raise ValueError("unknown plane: " + plane)
            fc_edges = []
            for e in edges_def:
                if e["kind"] == "CIRCLE":
                    cx, cy = e["center"]
                    c = lift(cx, cy)
                    axis_v = {{"XY": App.Vector(0,0,1), "XZ": App.Vector(0,1,0),
                              "YZ": App.Vector(1,0,0)}}.get(plane, App.Vector(0,0,1))
                    return Part.Face(Part.Wire(Part.Circle(c, axis_v, float(e["radius"])).toShape())).extrude(ev)
                if e["kind"] == "LINE":
                    x0, y0 = e["p0"]; x1, y1 = e["p1"]
                    a = lift(x0, y0); b = lift(x1, y1)
                    if a.distanceToPoint(b) < 1e-9:
                        continue
                    fc_edges.append(Part.LineSegment(a, b).toShape())
                elif e["kind"] == "ARC":
                    cx, cy = e["center"]; r = float(e["radius"])
                    sa = math.radians(float(e["start_angle"] or 0.0))
                    ea = math.radians(float(e["end_angle"] or 0.0))
                    if e.get("clockwise"):
                        if ea > sa: ea -= 2 * math.pi
                    elif ea < sa: ea += 2 * math.pi
                    mid = (sa + ea) * 0.5
                    x0, y0 = e.get("p0", [cx + r*math.cos(sa), cy + r*math.sin(sa)])
                    x1, y1 = e.get("p1", [cx + r*math.cos(ea), cy + r*math.sin(ea)])
                    sp = lift(x0, y0)
                    mp = lift(cx + r*math.cos(mid), cy + r*math.sin(mid))
                    ep = lift(x1, y1)
                    try:
                        fc_edges.append(Part.Arc(sp, mp, ep).toShape())
                    except Exception:
                        fc_edges.append(Part.LineSegment(sp, ep).toShape())
            if not fc_edges: return None
            try:
                wire = Part.Wire(fc_edges)
            except Exception:
                wire = Part.Wire(Part.__sortEdges__(fc_edges))
            if not wire.isClosed():
                v0 = wire.OrderedVertexes[0].Point
                v1 = wire.OrderedVertexes[-1].Point
                if v0.distanceToPoint(v1) > 1e-9:
                    fc_edges.append(Part.LineSegment(v1, v0).toShape())
                    wire = Part.Wire(fc_edges)
            return Part.Face(wire).extrude(ev)

        def _hole_cyl(p):
            r = float(p["radius"]); axis = p["axis"]
            x, y, z = p["position"]; length = float(p["through_length"])
            v = {{"X": App.Vector(1,0,0), "Y": App.Vector(0,1,0),
                 "Z": App.Vector(0,0,1)}}.get(axis, App.Vector(0,0,1))
            cyl = Part.makeCylinder(r, length, App.Vector(x,y,z), v)
            cyl.rotate(App.Vector(x,y,z), v, 180.0)
            return cyl

        def _cylinder_stack(p):
            if p.get("axis", "Z") != "Z":
                return None
            cx, cy = p.get("center", [0.0, 0.0])
            solids = []
            for seg in p.get("segments", []):
                z0 = float(seg.get("z_min", 0.0)); z1 = float(seg.get("z_max", z0))
                r = float(seg.get("radius", 0.0)); h = z1 - z0
                if r <= 0 or h <= 0:
                    continue
                solids.append(Part.makeCylinder(r, h, App.Vector(float(cx), float(cy), z0), App.Vector(0,0,1)))
            if not solids:
                return None
            out = solids[0]
            for item in solids[1:]:
                out = out.fuse(item)
            try:
                return out.removeSplitter()
            except Exception:
                return out

        def _edge_chamfer(shape, p):
            distance = float(p.get("distance", 0.0) or 0.0)
            if distance <= 0 or p.get("scope", "outer_z_edges") != "outer_z_edges":
                return shape
            if p.get("profile") == "arc_revolve":
                top_radius = float(p.get("top_radius", 0.0) or 0.0)
                bb = shape.BoundBox
                height = float(bb.ZLength)
                outer_radius = max(float(bb.XLength), float(bb.YLength)) * 0.5
                if top_radius <= 0 or top_radius >= outer_radius or distance >= height * 0.5:
                    return shape
                center_x = (float(bb.XMin) + float(bb.XMax)) * 0.5
                center_y = (float(bb.YMin) + float(bb.YMax)) * 0.5
                bottom_mid = App.Vector((top_radius + outer_radius) * 0.5, 0.0, distance * 0.35)
                top_mid = App.Vector((top_radius + outer_radius) * 0.5, 0.0, height - distance * 0.35)
                edges = [
                    Part.LineSegment(App.Vector(0,0,0), App.Vector(top_radius,0,0)).toShape(),
                    Part.Arc(App.Vector(top_radius,0,0), bottom_mid, App.Vector(outer_radius,0,distance)).toShape(),
                    Part.LineSegment(App.Vector(outer_radius,0,distance), App.Vector(outer_radius,0,height-distance)).toShape(),
                    Part.Arc(App.Vector(outer_radius,0,height-distance), top_mid, App.Vector(top_radius,0,height)).toShape(),
                    Part.LineSegment(App.Vector(top_radius,0,height), App.Vector(0,0,height)).toShape(),
                    Part.LineSegment(App.Vector(0,0,height), App.Vector(0,0,0)).toShape(),
                ]
                env = Part.Face(Part.Wire(edges)).revolve(
                    App.Vector(0,0,0), App.Vector(0,0,1), 360.0)
                if not env.Solids and env.Shells:
                    env = Part.Solid(env.Shells[0])
                env.rotate(App.Vector(0,0,0), App.Vector(0,0,1), 30.0)
                env.translate(App.Vector(center_x, center_y, float(bb.ZMin)))
                return shape.common(env).removeSplitter()
            bb = shape.BoundBox
            z_min = float(bb.ZMin); z_max = float(bb.ZMax)
            scale = max(float(bb.XLength), float(bb.YLength), float(bb.ZLength), 1.0)
            tol = max(scale * 1e-7, 1e-6)
            profile = p.get("profile", "line")
            edges = []
            for edge in shape.Edges:
                ebb = edge.BoundBox
                same_end = ((abs(float(ebb.ZMin) - z_min) <= tol and abs(float(ebb.ZMax) - z_min) <= tol) or
                            (abs(float(ebb.ZMin) - z_max) <= tol and abs(float(ebb.ZMax) - z_max) <= tol))
                if not same_end:
                    continue
                if profile == "arc":
                    edges.append(edge)
                    continue
                if len(edge.Vertexes) != 2:
                    continue
                p0 = edge.Vertexes[0].Point; p1 = edge.Vertexes[1].Point
                chord = p0.distanceToPoint(p1)
                if chord > tol and abs(float(edge.Length) - chord) <= max(tol, chord * 1e-6):
                    edges.append(edge)
            if not edges:
                return shape
            if profile == "arc":
                return shape.makeFillet(distance, edges)
            return shape.makeChamfer(distance, edges)

        doc = App.newDocument(BASE_NAME)
        solid = None
        for f in FEATURES:
            kind, params = f["kind"], f["params"]
            if kind == "extrude_profile":
                solid = _extrude_profile(params)
            elif kind == "base_block":
                ox, oy, oz = params.get("origin", [0,0,0])
                solid = Part.makeBox(params["width"], params["depth"],
                                     params["height"],
                                     App.Vector(ox, oy, oz))
            elif kind == "sphere":
                x, y, z = params.get("center", [0,0,0])
                solid = Part.makeSphere(float(params["radius"]),
                                        App.Vector(float(x), float(y), float(z)))
            elif kind == "cylinder_stack":
                solid = _cylinder_stack(params)
            elif kind == "hole" and solid is not None:
                cyl = _hole_cyl(params)
                if cyl is not None:
                    try:
                        solid = solid.cut(cyl)
                    except Exception:
                        pass
            elif kind == "profile_cut" and solid is not None:
                cutter = _extrude_profile(params)
                if cutter is not None:
                    try:
                        solid = solid.cut(cutter)
                    except Exception:
                        pass
            elif kind == "edge_chamfer" and solid is not None:
                try:
                    solid = _edge_chamfer(solid, params)
                except Exception:
                    pass
        if solid is not None:
            obj = doc.addObject("Part::Feature", "Result")
            obj.Shape = solid
        doc.recompute()
        doc.saveAs(FCSTD_PATH)
        print("Saved:", FCSTD_PATH)
    ''')
    with open(py_path, "w", encoding="utf-8") as f:
        f.write(body)
    return py_path
