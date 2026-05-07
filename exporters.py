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
from .feature_inference import Feature
from .view_classifier import ViewBundle


# ---------------------------------------------------------------------------
# STEP / OBJ / model.json (need FreeCAD)
# ---------------------------------------------------------------------------

def export_step(fcstd_path: str, step_path: str) -> str:
    import FreeCAD as App  # type: ignore
    import Part  # type: ignore
    doc_name = os.path.splitext(os.path.basename(fcstd_path))[0] + "_step"
    doc = App.openDocument(fcstd_path)
    try:
        shapes = [o.Shape for o in doc.Objects
                  if hasattr(o, "Shape") and not o.Shape.isNull()]
        if shapes:
            Part.export(shapes, step_path)
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
        meshes = []
        for o in doc.Objects:
            if not (hasattr(o, "Shape") and not o.Shape.isNull()):
                continue
            try:
                m = MeshPart.meshFromShape(Shape=o.Shape,
                                           LinearDeflection=linear_deflection,
                                           AngularDeflection=0.5)
                meshes.append(m)
            except Exception:
                pass
        if meshes:
            merged = meshes[0]
            for m in meshes[1:]:
                merged.addMesh(m)
            Mesh.export([_wrap_mesh_object(doc, merged)], obj_path)
    finally:
        App.closeDocument(doc.Name)
    return obj_path


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
        for edge in compound.Edges:
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
    if e.kind == "LINE" and len(e.points) == 2:
        (x0, y0), (x1, y1) = e.points
        ax.plot([x0, x1], [y0, y1], color="#1f3b73", linewidth=1.0)
    elif e.kind == "CIRCLE" and e.center is not None and e.radius is not None:
        import numpy as np  # type: ignore
        t = np.linspace(0, 2 * np.pi, 64)
        cx, cy = e.center
        ax.plot(cx + e.radius * np.cos(t),
                cy + e.radius * np.sin(t),
                color="#aa3333", linewidth=1.0)
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
                color="#1f3b73", linewidth=1.0)
    elif e.kind in ("LWPOLYLINE", "POLYLINE") and len(e.points) >= 2:
        xs = [p[0] for p in e.points]
        ys = [p[1] for p in e.points]
        if e.extra.get("closed"):
            xs.append(xs[0]); ys.append(ys[0])
        ax.plot(xs, ys, color="#1f3b73", linewidth=1.0)


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
            return Part.makeCylinder(r, length, App.Vector(x,y,z), v)

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
            elif kind == "hole" and solid is not None:
                cyl = _hole_cyl(params)
                if cyl is not None:
                    try:
                        solid = solid.cut(cyl)
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
