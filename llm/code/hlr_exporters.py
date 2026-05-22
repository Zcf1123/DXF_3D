"""Local post-processing exporters for the LLM-generated modeling route."""
from __future__ import annotations

from typing import List, Tuple


Segment = Tuple[Tuple[float, float], Tuple[float, float]]


def export_hlr_model_views_png(fcstd_path: str, png_path: str) -> str:
    """Render FRONT / LEFT / TOP model views using FreeCAD TechDraw HLR."""
    import FreeCAD as App  # type: ignore
    import TechDraw  # type: ignore
    import matplotlib  # type: ignore
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore
    from matplotlib.collections import LineCollection  # type: ignore

    doc = App.openDocument(fcstd_path)
    try:
        shape = _result_shape(doc)
        views = {
            "front": _project_hlr_segments(TechDraw, shape, App.Vector(0, -1, 0), "front"),
            "left": _project_hlr_segments(TechDraw, shape, App.Vector(1, 0, 0), "left"),
            "top": _project_hlr_segments(TechDraw, shape, App.Vector(0, 0, -1), "top"),
        }
        axis_limits = _axis_limits_from_shape(shape)
    finally:
        App.closeDocument(doc.Name)

    fig, axes = plt.subplots(2, 2, figsize=(8, 8))
    layout = {"front": axes[0][0], "left": axes[0][1], "top": axes[1][0]}
    axes[1][1].axis("off")
    axes[1][1].text(0.05, 0.95, "(empty)\nbottom-right reserved",
                    fontsize=9, va="top", color="gray")

    view_titles = {"front": "FRONT", "left": "LEFT", "top": "TOP"}
    for name in ("front", "left", "top"):
        ax = layout[name]
        solid_segs, hidden_segs = _normalize_hlr_segments(*views[name])
        if hidden_segs:
            ax.add_collection(LineCollection(
                hidden_segs, colors="#50688f", linewidths=1.0,
                linestyles=(0, (5, 3)), alpha=0.95,
                capstyle="round", joinstyle="round", zorder=1))
        if solid_segs:
            ax.add_collection(LineCollection(
                solid_segs, colors="#1f3b73", linewidths=1.0,
                capstyle="round", joinstyle="round", zorder=2))
        all_segs = solid_segs + hidden_segs
        if all_segs:
            xs = [point[0] for seg in all_segs for point in seg]
            ys = [point[1] for seg in all_segs for point in seg]
            x_max = max(max(xs), 1e-6)
            y_max = max(max(ys), 1e-6)
            pad = max(x_max, y_max, 1.0) * 0.04
            ax.set_xlim(-pad, x_max + pad)
            ax.set_ylim(-pad, y_max + pad)
        else:
            ax.set_xlim(0.0, 1.0)
            ax.set_ylim(0.0, 1.0)
        if name in axis_limits:
            x_max, y_max = axis_limits[name]
            pad = max(x_max, y_max, 1.0) * 0.04
            ax.set_xlim(-pad, x_max + pad)
            ax.set_ylim(-pad, y_max + pad)
        ax.set_aspect("equal")
        ax.set_title(view_titles.get(name, name.upper()), fontsize=11)
        ax.grid(True, linestyle=":", alpha=0.4)

    fig.suptitle("Three views", fontsize=13)
    fig.tight_layout()
    fig.savefig(png_path, dpi=120)
    plt.close(fig)
    return png_path


def _result_shape(doc):
    obj = doc.getObject("Result")
    if obj is None:
        for candidate in doc.Objects:
            if hasattr(candidate, "Shape") and getattr(candidate.Shape, "Solids", None):
                obj = candidate
                break
    if obj is None or not hasattr(obj, "Shape"):
        raise RuntimeError("FCStd does not contain a Result shape")
    return obj.Shape


def _project_hlr_segments(techdraw, shape, direction, view_name: str) -> Tuple[List[Segment], List[Segment]]:
    try:
        parts = techdraw.projectEx(shape, direction)
    except Exception:
        parts = techdraw.project(shape, direction)
        visible = _shape_segments(parts[0], view_name) + _shape_segments(parts[1], view_name)
        hidden = _shape_segments(parts[2], view_name) + _shape_segments(parts[3], view_name)
        return visible, hidden

    visible = []
    hidden = []
    for part in parts[:5]:
        visible.extend(_shape_segments(part, view_name))
    for part in parts[5:]:
        hidden.extend(_shape_segments(part, view_name))
    return visible, hidden


def _shape_segments(shape, view_name: str) -> List[Segment]:
    segments: List[Segment] = []
    for edge in getattr(shape, "Edges", []):
        try:
            points = edge.discretize(Number=_edge_sample_count(edge))
        except Exception:
            try:
                points = edge.discretize(_edge_sample_count(edge))
            except Exception:
                continue
        for start, end in zip(points, points[1:]):
            p0 = _screen_point(start, view_name)
            p1 = _screen_point(end, view_name)
            if abs(p0[0] - p1[0]) < 1e-9 and abs(p0[1] - p1[1]) < 1e-9:
                continue
            segments.append((p0, p1))
    return segments


def _edge_sample_count(edge) -> int:
    curve = getattr(edge, "Curve", None)
    curve_type = getattr(curve, "TypeId", "")
    curve_name = type(curve).__name__ if curve is not None else ""
    if curve_type == "Part::GeomLine" or curve_name == "Line":
        return 2
    return 128


def _screen_point(point, view_name: str) -> Tuple[float, float]:
    if view_name == "front":
        return (float(point.y), -float(point.x))
    if view_name == "left":
        return (-float(point.y), float(point.x))
    if view_name == "top":
        return (-float(point.x), float(point.y))
    raise ValueError(f"unknown view name: {view_name}")


def _normalize_hlr_segments(visible: List[Segment], hidden: List[Segment]) -> Tuple[List[Segment], List[Segment]]:
    all_segments = visible + hidden
    if not all_segments:
        return [], []
    min_x = min(point[0] for seg in all_segments for point in seg)
    min_y = min(point[1] for seg in all_segments for point in seg)

    def normalize(segment: Segment) -> Segment:
        return ((segment[0][0] - min_x, segment[0][1] - min_y),
                (segment[1][0] - min_x, segment[1][1] - min_y))

    return [normalize(seg) for seg in visible], [normalize(seg) for seg in hidden]


def _axis_limits_from_shape(shape) -> dict:
    bb = shape.BoundBox
    width_x = max(float(bb.XLength), 1e-6)
    depth_y = max(float(bb.YLength), 1e-6)
    height_z = max(float(bb.ZLength), 1e-6)
    return {
        "front": (width_x, height_z),
        "left": (depth_y, height_z),
        "top": (width_x, depth_y),
    }
