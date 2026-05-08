"""Artifact exporters: STEP, OBJ, PNG preview, model.json, generated_model.py.

PNG preview is rendered from the original DXF (three subplots, deterministic
matplotlib output) and therefore does NOT require FreeCADGui — it works
under headless `freecadcmd` as long as `matplotlib` is installed.

STEP / OBJ / model.json need an open FreeCAD document.
"""
from __future__ import annotations

import json
import os
import textwrap
from typing import Any, Dict, List

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
        ax.set_xlim(0.0, max(float(pv.width), 1e-6))
        ax.set_ylim(0.0, max(float(pv.height), 1e-6))
        ax.grid(True, linestyle=":", alpha=0.4)

    fig.suptitle("DXF normalized three views", fontsize=13)
    fig.tight_layout()
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


def export_model_views_png(fcstd_path: str, png_path: str) -> str:
    """Render FRONT / RIGHT / TOP orthographic views from the final solid."""
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore
    from matplotlib.collections import LineCollection  # type: ignore
    import FreeCAD as App  # type: ignore

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
            lc = LineCollection(segs, colors="#1f3b73", linewidths=1.0,
                                capstyle="round", joinstyle="round")
            ax.add_collection(lc)
            xs = [p[0] for s in segs for p in s]
            ys = [p[1] for s in segs for p in s]
            ax.set_xlim(0.0, max(max(xs), 1e-6))
            ax.set_ylim(0.0, max(max(ys), 1e-6))
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


def _project_shape_edges(shape, view_name: str):
    if _is_sphere_shape(shape):
        return _project_sphere_outline(shape, view_name)
    segs = []
    artifact_tol = max(
        float(shape.BoundBox.XLength),
        float(shape.BoundBox.YLength),
        float(shape.BoundBox.ZLength),
        1.0,
    ) * 0.075
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
        1.0,
    )
    return float(edge.Length) <= scale * 0.09


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
    xs = [p[0] for s in segs for p in s]
    ys = [p[1] for s in segs for p in s]
    xmin = min(xs)
    ymin = min(ys)
    return [((a[0] - xmin, a[1] - ymin),
             (b[0] - xmin, b[1] - ymin)) for a, b in segs]


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
    from matplotlib.collections import LineCollection  # type: ignore
    import FreeCAD as App  # type: ignore
    import Part  # type: ignore

    doc = App.openDocument(fcstd_path)
    try:
        shapes = []
        for o in doc.Objects:
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
            for edge in compound.Edges:
                if _is_short_3d_artifact_edge(edge, compound):
                    continue
                if _is_internal_3d_center_seam(edge, compound):
                    continue
                L = max(edge.Length, 1e-9)
                n = max(2, int(L * 6))
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
            if plane == "XY":
                lift = lambda u, v: App.Vector(float(u), float(v), 0.0)
                ev = App.Vector(0.0, 0.0, depth)
            elif plane == "XZ":
                lift = lambda u, v: App.Vector(float(u), 0.0, float(v))
                ev = App.Vector(0.0, depth, 0.0)
            elif plane == "YZ":
                lift = lambda u, v: App.Vector(0.0, float(u), float(v))
                ev = App.Vector(depth, 0.0, 0.0)
            else:
                raise ValueError("unknown plane: " + plane)
            fc_edges = []
            for e in edges_def:
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
                    if ea < sa: ea += 2 * math.pi
                    mid = (sa + ea) * 0.5
                    sp = lift(cx + r*math.cos(sa), cy + r*math.sin(sa))
                    mp = lift(cx + r*math.cos(mid), cy + r*math.sin(mid))
                    ep = lift(cx + r*math.cos(ea), cy + r*math.sin(ea))
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
            elif kind == "hole" and solid is not None:
                cyl = _hole_cyl(params)
                if cyl is not None:
                    try:
                        solid = solid.cut(cyl)
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
