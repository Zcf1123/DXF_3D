"""Direct LLM-to-FreeCAD script helper for the auto modeling route."""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import signal
from typing import Any, Dict, List, Optional, Tuple

from ...dxf_loader import DxfEntity
from ...geometry_estimator import extract_closed_outlines_and_circles
from ...llm_client import Prompt, load_prompt_from_dir, render_template
from ...view_classifier import ViewBundle


HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(os.path.dirname(HERE), "prompts")


def load_prompt(name: str) -> Prompt:
    return load_prompt_from_dir(PROMPTS_DIR, name)


_MAX_VISIBLE_ENTITIES_PER_VIEW = 0
_MAX_HIDDEN_ENTITIES_PER_VIEW = 4
_MAX_OUTLINES_PER_VIEW = 4
_MAX_EDGES_PER_OUTLINE = 12
_MAX_FULL_EDGES_PER_OUTLINE = 64
_MAX_PROFILE_SAMPLE_POINTS = 240

_BANNED_TEXT = (
    "os.system", "subprocess", "shutil", "socket", "requests", "urllib",
    "http.client", "eval(", "exec(", "compile(", "__import__", "open(",
    "remove(", "unlink(", "rmdir(", "rename(", "replace(", "system(",
    "popen(", "Popen", "check_call", "check_output", "run(",
)

_ALLOWED_IMPORT_ROOTS = {"FreeCAD", "Part", "math"}
_INVALID_FREECAD_CALLS = {
    "Part.Extrude": "FreeCAD Part has no Part.Extrude; create a Part.Face and call face.extrude(App.Vector(...)) instead",
    "Part.setMeasurePrecision": "FreeCAD Part has no Part.setMeasurePrecision; remove this no-op line",
    "Part.cut": "FreeCAD Part has no Part.cut module function; call shape.cut(other_shape) instead",
    "Part.common": "FreeCAD Part has no Part.common module function; call shape.common(other_shape) instead",
    "Part.Fuse": "FreeCAD Part has no Part.Fuse; call shape1.fuse(shape2) instead",
    "Part.makeRevolution": "Do not use Part.makeRevolution; create a closed Part.Face and call face.revolve(axis_point, axis_dir, 360) instead",
    "Part.makePrism": "FreeCAD Part has no stable Part.makePrism helper here; create Part.Face(wire).extrude(vector) instead",
    "App.setActiveDocument": "Do not call App.setActiveDocument; keep and use the document returned by App.newDocument(...) instead",
    "doc.close": "FreeCAD document objects have no close() method; remove doc.close() after saveAs",
    "doc.Name": "FreeCAD document Name is read-only; pass the name to App.newDocument(...) instead",
}
_REQUEST_TIMEOUT_SECONDS = 180
_MAX_SCRIPT_TOKENS = 9000
_SCRIPT_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "outputs", ".llm_script_cache")

_AUXILIARY_TOKENS = frozenset({
    "CENTER", "CENTRE", "CENTRO", "AXIS", "AXES", "CONSTRUCTION",
    "PROJECTION", "AUX", "GUIDE", "REF", "REFERENCE", "DIM", "ANNO",
    "TEXT", "NOTE", "DEFPOINTS", "坐标", "轴线", "中心", "辅助",
    "投影", "参考", "标注", "尺寸",
})


def build_auto_context(
    dxf_path: str,
    bundles: List[ViewBundle],
    projected: Dict[str, Any],
    model_intent: str = "",
) -> Dict[str, Any]:
    """Build a compact, geometry-first context for direct script generation."""
    intent = (model_intent or "").strip()
    part_knowledge = _load_part_knowledge()
    context = {
        "input_file": os.path.basename(dxf_path),
        "model_intent": intent or "（无）",
        "intent_mode": {
            "enabled": bool(intent),
            "instruction": (
                "用户提供的 model_intent 只作为弱参考和歧义消解线索。必须先根据 projected_views/views 自主识别三视图表达的零件类型，"
                "再把识别结果与 part_knowledge 中的零件族和建模策略匹配；不得因为用户简短术语而覆盖三视图证据。"
                if intent else
                "未提供建模意图。必须根据 projected_views/views 自主识别三视图表达的零件类型，再在 part_knowledge 中匹配建模策略。"
            ),
        },
        "coordinate_convention": {
            "front": "XZ plane: drawing x -> world X, drawing y -> world Z",
            "top": "XY plane: drawing x -> world X, drawing y -> world Y",
            "left": "YZ plane: drawing x -> world Y, drawing y -> world Z",
        },
        "part_knowledge": part_knowledge,
        "views": [_bundle_summary(bundle) for bundle in bundles],
        "projected_views": {
            name: _projected_view_summary(name, pv)
            for name, pv in projected.items()
        },
    }
    context["model_understanding_hints"] = _model_understanding_hints(context["projected_views"])
    context["hole_hints"] = _through_hole_hints(
        context["projected_views"],
        context["model_understanding_hints"],
    )
    context["dimension_constraints"] = _dimension_constraints(context["projected_views"])
    return context


def _dimension_constraints(projected_views: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    front = projected_views.get("front") or {}
    top = projected_views.get("top") or {}
    left = projected_views.get("left") or {}
    width_x = _view_extent(front, "width", _view_extent(top, "width", 0.0))
    depth_y = _view_extent(left, "width", _view_extent(top, "height", 0.0))
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    tolerance = max(max(width_x, depth_y, height_z) * 0.02, 1e-6)
    return {
        "overall_size": {
            "width_x": _round_num(width_x),
            "depth_y": _round_num(depth_y),
            "height_z": _round_num(height_z),
            "tolerance": _round_num(tolerance),
        },
        "sources": {
            "width_x": "front.width, cross-check top.width",
            "depth_y": "left.width, cross-check top.height",
            "height_z": "front.height, cross-check left.height",
        },
        "required_rules": [
            "All main lengths, radii, offsets, hole positions and extrusion depths must come from projected_views, hole_hints, model_understanding_hints, or this dimension_constraints object.",
            "Do not invent round numbers or rescale the part unless a provided hint explicitly says so.",
            "Final model bbox XLength must match width_x within tolerance when the view evidence defines the full X extent.",
            "Final model bbox YLength must match depth_y within tolerance when the view evidence defines the full Y extent.",
            "Final model bbox ZLength must match height_z within tolerance when the view evidence defines the full Z extent.",
            "The script must define DIMENSIONS_USED as a Python dict recording the JSON-derived values used for key dimensions.",
        ],
    }


def _model_understanding_hints(projected_views: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    hints: List[Dict[str, Any]] = []
    front_arc_plate_hint = _front_arc_profile_plate_with_y_holes_hint(projected_views)
    if front_arc_plate_hint is not None:
        hints.append(front_arc_plate_hint)
    smooth_front_plate_hint = _smooth_front_profile_plate_with_y_holes_hint(projected_views)
    if smooth_front_plate_hint is not None:
        hints.append(smooth_front_plate_hint)
    top_polygon_prism_hint = _polygon_prism_from_top_outline_hint(projected_views)
    if top_polygon_prism_hint is not None:
        hints.append(top_polygon_prism_hint)
    simple_top_cylinder_hint = _simple_z_cylinder_from_top_circle_hint(projected_views)
    if simple_top_cylinder_hint is not None:
        hints.append(simple_top_cylinder_hint)
    stacked_y_cylinders_hint = _stacked_flat_cylinders_along_y_hint(projected_views)
    if stacked_y_cylinders_hint is not None:
        hints.append(stacked_y_cylinders_hint)
    cylinder_on_hex_hint = _central_cylinder_on_hex_prism_hint(projected_views)
    if cylinder_on_hex_hint is not None:
        hints.append(cylinder_on_hex_hint)
    tube_hint = _hollow_cylinder_from_left_hint(projected_views)
    if tube_hint is not None:
        hints.append(tube_hint)
    hex_prism_hint = _regular_hex_prism_from_top_hint(projected_views)
    if hex_prism_hint is not None:
        hints.append(hex_prism_hint)
    nut_hint = _hex_nut_arc_revolve_hint(projected_views)
    if nut_hint is not None:
        hints.append(nut_hint)
    gear_hint = _toothed_disk_from_top_hint(projected_views)
    if gear_hint is not None:
        hints.append(gear_hint)
    return hints


def _polygon_prism_from_top_outline_hint(projected_views: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    top = projected_views.get("top") or {}
    front = projected_views.get("front") or {}
    left = projected_views.get("left") or {}
    top_width = _view_extent(top, "width", 0.0)
    top_height = _view_extent(top, "height", 0.0)
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    if top_width <= 0.0 or top_height <= 0.0 or height_z <= 0.0:
        return None

    outlines = top.get("visible_closed_outlines") or []
    if not outlines:
        return None
    outline = max(outlines, key=lambda item: float(item.get("width") or 0.0) * float(item.get("height") or 0.0))
    edge_count = int(outline.get("edge_count") or 0)
    edges = outline.get("edges") or []
    bbox = outline.get("bbox") or []
    if len(bbox) != 4 or not edges or outline.get("edges_complete") is not True:
        return None
    if edge_count < 5 or edge_count > 16:
        return None
    if any(edge.get("kind") != "LINE" for edge in edges):
        return None
    bbox_width = float(bbox[2]) - float(bbox[0])
    bbox_height = float(bbox[3]) - float(bbox[1])
    if bbox_width < top_width * 0.85 or bbox_height < top_height * 0.85:
        return None
    if top.get("visible_circles"):
        return None

    vertices: List[List[float]] = []
    for edge in edges:
        p0 = edge.get("p0") or []
        if len(p0) == 2:
            vertices.append([float(p0[0]), float(p0[1])])
    if len(vertices) < 5:
        return None

    return {
        "kind": "polygon_prism_from_top_outline",
        "confidence": "high",
        "understanding": "多边形棱柱：TOP 的低边数直线闭合多边形是真实 footprint，FRONT/LEFT 给高度。",
        "source_view": "top",
        "construction": {
            "profile_plane": "XY",
            "vertices_2d": _round_json(vertices),
            "edge_count": edge_count,
            "height_z": _round_num(height_z),
            "operation": "Create a closed XY polygon from vertices_2d and extrude along Z by height_z. Do not replace this low-edge polygon with an approximated circle/cylinder.",
        },
        "evidence": [
            f"top largest closed outline has {edge_count} complete LINE edges",
            "top has no true visible circle entity",
            "front/left provide height and side projections only",
        ],
    }


def _simple_z_cylinder_from_top_circle_hint(projected_views: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    top = projected_views.get("top") or {}
    front = projected_views.get("front") or {}
    left = projected_views.get("left") or {}
    width_x = _view_extent(front, "width", _view_extent(top, "width", 0.0))
    depth_y = _view_extent(left, "width", _view_extent(top, "height", 0.0))
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    top_width = _view_extent(top, "width", 0.0)
    top_height = _view_extent(top, "height", 0.0)
    if width_x <= 0.0 or depth_y <= 0.0 or height_z <= 0.0 or top_width <= 0.0 or top_height <= 0.0:
        return None

    front_outlines = front.get("visible_closed_outlines") or []
    left_outlines = left.get("visible_closed_outlines") or []
    if not front_outlines or not left_outlines:
        return None
    front_full = max(front_outlines, key=lambda item: float(item.get("width") or 0.0) * float(item.get("height") or 0.0))
    left_full = max(left_outlines, key=lambda item: float(item.get("width") or 0.0) * float(item.get("height") or 0.0))
    if int(front_full.get("edge_count") or 0) != 4 or int(left_full.get("edge_count") or 0) != 4:
        return None

    low_edge_polygon = False
    for outline in top.get("visible_closed_outlines") or []:
        edge_count = int(outline.get("edge_count") or 0)
        if outline.get("edges_complete") is True and 5 <= edge_count <= 16:
            edges = outline.get("edges") or []
            if edges and all(edge.get("kind") == "LINE" for edge in edges):
                low_edge_polygon = True
                break

    circles = [circle for circle in top.get("visible_circles") or [] if not circle.get("hidden")]
    if not circles and not low_edge_polygon:
        circles += [curve for curve in top.get("approximated_curves") or []
                    if curve.get("kind") == "approximated_circle" and int(curve.get("edge_count") or 0) >= 24]
    if not circles:
        return None
    circle = max(circles, key=lambda item: float(item.get("radius") or 0.0))
    center = circle.get("center") or []
    radius = float(circle.get("radius") or 0.0)
    bbox = circle.get("bbox") or []
    if len(center) != 2 or len(bbox) != 4 or radius <= 0.0:
        return None
    diameter = radius * 2.0
    tol = max(top_width, top_height, width_x, depth_y, 1.0) * 0.03
    spans_top = abs(diameter - top_width) <= tol and abs(diameter - top_height) <= tol
    centered = abs(float(center[0]) - top_width * 0.5) <= tol and abs(float(center[1]) - top_height * 0.5) <= tol
    matches_rect = abs(width_x - diameter) <= tol and abs(depth_y - diameter) <= tol
    if not (spans_top and centered and matches_rect):
        return None

    return {
        "kind": "simple_z_cylinder_from_top_circle",
        "confidence": "high",
        "understanding": "简单竖直圆柱：TOP 为完整圆形 footprint，FRONT/LEFT 为矩形投影且只给高度。",
        "source_view": "top",
        "construction": {
            "axis": "Z",
            "center_xy": [_round_num(float(center[0])), _round_num(float(center[1]))],
            "radius": _round_num(radius),
            "height_z": _round_num(height_z),
            "operation": "Create a Z-axis solid cylinder from TOP circle center/radius and extrude height_z. Do not replace it with a box just because FRONT/LEFT are rectangular projections.",
        },
        "evidence": [
            "top.visible_circles contains a centered circle spanning the TOP bbox",
            "front and left are rectangular projections with width/depth matching the TOP circle diameter",
            "front/left rectangles provide cylinder height only",
        ],
    }


def _front_arc_profile_plate_with_y_holes_hint(projected_views: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    front = projected_views.get("front") or {}
    top = projected_views.get("top") or {}
    left = projected_views.get("left") or {}
    width_x = _view_extent(front, "width", _view_extent(top, "width", 0.0))
    depth_y = _view_extent(left, "width", _view_extent(top, "height", 0.0))
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    if width_x <= 0.0 or depth_y <= 0.0 or height_z <= 0.0:
        return None

    edges = front.get("ordered_profile_edges") or []
    if not edges:
        for outline in front.get("visible_closed_outlines") or []:
            outline_edges = outline.get("edges") or []
            if outline.get("edges_complete") is True and outline_edges:
                edges = outline_edges
                break
    if len(edges) < 3 or not any(edge.get("kind") == "ARC" for edge in edges):
        return None

    circles = front.get("visible_circles") or []
    visible_circles = [circle for circle in circles if not circle.get("hidden")]
    if len(visible_circles) < 1:
        return None

    return {
        "kind": "front_arc_profile_plate_with_y_holes",
        "confidence": "high",
        "understanding": "等厚真实圆弧轮廓连接板：FRONT 完整 ARC 外轮廓沿 Y 拉伸，FRONT 圆为沿 Y 贯穿孔。",
        "source_view": "front",
        "construction": {
            "outer_profile_plane": "XZ",
            "ordered_profile_edges": _round_json(edges),
            "extrude_axis": "Y",
            "depth_y": _round_num(depth_y),
            "width_x": _round_num(width_x),
            "height_z": _round_num(height_z),
            "hole_axis": "Y",
            "hole_circle_candidates": _round_json(visible_circles),
            "operation": "Use the FRONT ordered_profile_edges as the true outer profile in XZ, build each ARC with Part.Arc on points (x, 0, z), make a face, extrude along Y by depth_y, then cut Y-axis holes using hole_hints. Do not use TOP rectangular sub-outlines as the body footprint.",
        },
        "evidence": [
            "front.ordered_profile_edges contains true ARC edges for a closed outer profile",
            "front.visible_circles provide through-hole evidence",
            "top/left provide Y depth and height cross-checks",
        ],
    }


def _smooth_front_profile_plate_with_y_holes_hint(projected_views: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    front = projected_views.get("front") or {}
    top = projected_views.get("top") or {}
    left = projected_views.get("left") or {}
    width_x = _view_extent(front, "width", _view_extent(top, "width", 0.0))
    depth_y = _view_extent(left, "width", _view_extent(top, "height", 0.0))
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    if width_x <= 0.0 or depth_y <= 0.0 or height_z <= 0.0:
        return None

    outlines = front.get("visible_closed_outlines") or []
    if not outlines:
        return None
    full_outline = max(outlines, key=lambda item: int(item.get("edge_count") or 0))
    edge_count = int(full_outline.get("edge_count") or 0)
    if edge_count < 80:
        return None

    bbox = full_outline.get("bbox") or []
    if len(bbox) != 4:
        return None
    bbox_width = float(bbox[2]) - float(bbox[0])
    bbox_height = float(bbox[3]) - float(bbox[1])
    if bbox_width < width_x * 0.85 or bbox_height < height_z * 0.85:
        return None

    profile_points = full_outline.get("profile_points_2d") or full_outline.get("sample_points_2d") or []
    if len(profile_points) < 24:
        return None

    rounded_slots = [curve for curve in front.get("approximated_curves") or []
                     if curve.get("kind") == "approximated_rounded_slot"]
    hole_circles = [curve for curve in front.get("approximated_curves") or []
                    if curve.get("kind") == "approximated_circle"]
    if not rounded_slots or not hole_circles:
        return None

    return {
        "kind": "smooth_front_profile_plate_with_y_holes",
        "confidence": "medium",
        "understanding": "等厚平滑轮廓连接板：FRONT 高密度外轮廓沿 Y 拉伸，FRONT 圆为沿 Y 贯穿孔。",
        "source_view": "front",
        "construction": {
            "outer_profile_plane": "XZ",
            "outer_profile_points_2d": _round_json(profile_points),
            "extrude_axis": "Y",
            "depth_y": _round_num(depth_y),
            "width_x": _round_num(width_x),
            "height_z": _round_num(height_z),
            "hole_axis": "Y",
            "hole_circle_candidates": _round_json(hole_circles),
            "operation": "Create one closed Part.BSplineCurve from outer_profile_points_2d in the FRONT/XZ plane, make a face and extrude along Y by depth_y, then cut Y-axis holes using hole_hints. Do not replace the outer profile with a standard rounded slot/capsule. Do not create hundreds of LineSegment edges from these points unless the BSpline face fails, because that creates vertical seam lines on the side surface.",
        },
        "evidence": [
            f"front largest outline has {edge_count} short LINE edges and spans the full FRONT bbox",
            "front.approximated_curves includes an approximated_rounded_slot for the outer contour",
            "front.approximated_curves includes approximated circles that match Y-axis through holes",
            "top/left provide the extrusion depth and height cross-check for an equal-thickness plate",
        ],
    }


def _stacked_flat_cylinders_along_y_hint(projected_views: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    front = projected_views.get("front") or {}
    top = projected_views.get("top") or {}
    left = projected_views.get("left") or {}
    width_x = _view_extent(front, "width", _view_extent(top, "width", 0.0))
    depth_y = _view_extent(left, "width", _view_extent(top, "height", 0.0))
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    if width_x <= 0.0 or depth_y <= 0.0 or height_z <= 0.0:
        return None
    circles = [curve for curve in front.get("approximated_curves") or []
               if curve.get("kind") == "approximated_circle"]
    if not circles:
        return None
    circle = max(circles, key=lambda item: float(item.get("radius") or 0.0))
    radius = float(circle.get("radius") or 0.0)
    center = circle.get("center") or []
    if len(center) != 2 or radius <= 0.0:
        return None
    if abs(radius * 2.0 - width_x) > max(width_x * 0.08, 1e-6):
        return None
    if abs(radius * 2.0 - height_z) > max(height_z * 0.08, 1e-6):
        return None
    y_breaks = _axis_break_positions_from_views(top, left, depth_y)
    if len(y_breaks) < 4:
        return None
    segments = []
    for y0, y1 in zip(y_breaks, y_breaks[1:]):
        thickness = y1 - y0
        if thickness <= max(depth_y * 0.03, 1e-6):
            continue
        segment_radius = _segment_radius_from_projection_spans(top, left, y0, y1, depth_y, radius)
        segments.append({
            "y0": _round_num(y0),
            "y1": _round_num(y1),
            "depth_y": _round_num(thickness),
            "radius": _round_num(segment_radius),
        })
    if len(segments) < 3:
        return None
    return {
        "kind": "stacked_flat_cylinders_along_y",
        "confidence": "high",
        "understanding": "同轴多个扁圆柱沿 Y 方向堆叠：FRONT 圆表示最大外圆截面，TOP/LEFT 的多条平行隐藏线/窄矩形给出轴向分段和各段直径，不是单个长圆柱。",
        "construction": {
            "axis": "Y",
            "center_xz": [_round_num(float(center[0])), _round_num(float(center[1]))],
            "radius": _round_num(radius),
            "segments": segments,
            "operation": "For each segment, create a Y-axis cylinder using that segment's radius and the common center_xz, from y0 to y1, then fuse all segments. Preserve visible/hidden segment boundaries as circular end faces; do not replace with one long cylinder or one radius.",
        },
        "evidence": [
            "front.approximated_curves contains one circle spanning width_x and height_z, used as the maximum outer radius",
            "top/left contain multiple parallel hidden or visible boundary lines along the Y direction",
            "line spans in top/left give per-segment X/Z diameters, so segments may have different radii",
        ],
    }


def _segment_radius_from_projection_spans(
    top: Dict[str, Any],
    left: Dict[str, Any],
    y0: float,
    y1: float,
    depth_y: float,
    default_radius: float,
) -> float:
    mid_y = (y0 + y1) * 0.5
    tol = max(depth_y * 0.035, 1e-6)
    x_spans: List[float] = []
    z_spans: List[float] = []
    for outline in top.get("visible_closed_outlines") or []:
        bbox = outline.get("bbox") or []
        if len(bbox) != 4:
            continue
        if _range_overlaps(mid_y, float(bbox[1]), float(bbox[3]), tol):
            x_spans.append(float(bbox[2]) - float(bbox[0]))
    for entity in top.get("key_hidden_entities") or []:
        bbox = entity.get("bbox") or []
        if len(bbox) != 4:
            continue
        if abs(float(bbox[1]) - float(bbox[3])) <= tol and abs(float(bbox[1]) - mid_y) <= max((y1 - y0) * 0.55, tol):
            x_spans.append(float(bbox[2]) - float(bbox[0]))
    for group in ((top.get("hidden_line_groups") or {}).get("horizontal") or []):
        coord = float(group.get("coord") or 0.0)
        if abs(coord - mid_y) <= max((y1 - y0) * 0.55, tol):
            span = float(group.get("max_span") or 0.0)
            if span > 0.0:
                x_spans.append(span)
    for outline in left.get("visible_closed_outlines") or []:
        bbox = outline.get("bbox") or []
        if len(bbox) != 4:
            continue
        yl0 = depth_y - float(bbox[2])
        yl1 = depth_y - float(bbox[0])
        if _range_overlaps(mid_y, yl0, yl1, tol):
            z_spans.append(float(bbox[3]) - float(bbox[1]))
    for entity in left.get("key_hidden_entities") or []:
        bbox = entity.get("bbox") or []
        if len(bbox) != 4:
            continue
        if abs(float(bbox[0]) - float(bbox[2])) <= tol:
            y = depth_y - float(bbox[0])
            if abs(y - mid_y) <= max((y1 - y0) * 0.55, tol):
                z_spans.append(float(bbox[3]) - float(bbox[1]))
    for group in ((left.get("hidden_line_groups") or {}).get("vertical") or []):
        y = depth_y - float(group.get("coord") or 0.0)
        if abs(y - mid_y) <= max((y1 - y0) * 0.55, tol):
            span = float(group.get("max_span") or 0.0)
            if span > 0.0:
                z_spans.append(span)
    radii = []
    if x_spans:
        radii.append(max(x_spans) * 0.5)
    if z_spans:
        radii.append(max(z_spans) * 0.5)
    if not radii:
        return default_radius
    radius = min(max(radii), default_radius)
    return radius if radius > 1e-9 else default_radius


def _range_overlaps(value: float, start: float, end: float, tol: float) -> bool:
    lo = min(start, end) - tol
    hi = max(start, end) + tol
    return lo <= value <= hi


def _axis_break_positions_from_views(top: Dict[str, Any], left: Dict[str, Any], depth_y: float) -> List[float]:
    positions = [0.0, depth_y]
    tol = max(depth_y * 0.02, 1e-6)
    for outline in top.get("visible_closed_outlines") or []:
        bbox = outline.get("bbox") or []
        if len(bbox) == 4:
            positions.extend([float(bbox[1]), float(bbox[3])])
    for entity in top.get("key_hidden_entities") or []:
        bbox = entity.get("bbox") or []
        if len(bbox) != 4:
            continue
        if abs(float(bbox[1]) - float(bbox[3])) <= tol:
            positions.append(float(bbox[1]))
    for group in ((top.get("hidden_line_groups") or {}).get("horizontal") or []):
        positions.append(float(group.get("coord") or 0.0))
    left_width = _view_extent(left, "width", depth_y)
    for outline in left.get("visible_closed_outlines") or []:
        bbox = outline.get("bbox") or []
        if len(bbox) == 4:
            positions.extend([depth_y - float(bbox[0]), depth_y - float(bbox[2])])
    for entity in left.get("key_hidden_entities") or []:
        bbox = entity.get("bbox") or []
        if len(bbox) != 4:
            continue
        if abs(float(bbox[0]) - float(bbox[2])) <= tol:
            positions.append(depth_y - float(bbox[0]))
    for group in ((left.get("hidden_line_groups") or {}).get("vertical") or []):
        positions.append(depth_y - float(group.get("coord") or 0.0))
    positions = [min(max(pos, 0.0), depth_y) for pos in positions]
    positions.sort()
    deduped: List[float] = []
    for pos in positions:
        if not deduped or abs(pos - deduped[-1]) > tol:
            deduped.append(pos)
    if deduped and deduped[0] > tol:
        deduped.insert(0, 0.0)
    if deduped and abs(deduped[-1] - depth_y) > tol:
        deduped.append(depth_y)
    return deduped


def _central_cylinder_on_hex_prism_hint(projected_views: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    top = projected_views.get("top") or {}
    front = projected_views.get("front") or {}
    left = projected_views.get("left") or {}
    polygons = [hint for hint in top.get("regular_polygon_hints") or []
                if hint.get("kind") == "regular_hexagon_bbox"]
    if not polygons:
        return None
    circle = _largest_centered_top_solid_circle(top, polygons[0])
    if circle is None:
        return None
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    if height_z <= 0.0:
        return None
    base_height = _support_plate_height_from_side_views(front, left, height_z)
    if base_height is None:
        return None
    cylinder_height = height_z - base_height
    if cylinder_height <= max(height_z * 0.08, 1e-6):
        return None
    center = circle.get("center") or []
    if len(center) != 2:
        return None
    return {
        "kind": "central_cylinder_on_hex_prism",
        "confidence": "high",
        "understanding": "组合件：TOP 六边形是下部六棱柱外轮廓，中间大圆是叠加的实心圆柱，不是切除孔。",
        "construction": {
            "base": {
                "source_view": "top",
                "plane": "XY",
                "vertices_2d": polygons[0].get("recommended_vertices_2d"),
                "height_z": _round_num(base_height),
                "operation": "Create a closed XY hexagon face and extrude along Z from z=0 to base_height.",
            },
            "cylinder": {
                "axis": "Z",
                "center": _round_json(center),
                "radius": _round_num(float(circle.get("radius") or 0.0)),
                "base_z": _round_num(base_height),
                "height_z": _round_num(cylinder_height),
                "operation": "Create a solid Z-axis cylinder on top of the hex prism and fuse it with the base. Do not cut this circle as a hole.",
            },
        },
        "evidence": [
            "top.regular_polygon_hints contains a regular hexagon outer footprint",
            "top contains one large centered approximated circle inside the hexagon",
            "front/left show a higher, narrower rectangular projection over the lower hex prism projection",
        ],
    }


def _largest_centered_top_solid_circle(top: Dict[str, Any], hex_hint: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    center = hex_hint.get("center") or []
    inradius = float(hex_hint.get("inradius") or 0.0)
    if len(center) != 2 or inradius <= 0.0:
        return None
    candidates = []
    for circle in _deduped_circle_sources(top):
        circle_center = circle.get("center") or []
        radius = float(circle.get("radius") or 0.0)
        if len(circle_center) != 2 or radius <= 0.0:
            continue
        center_tol = max(inradius * 0.08, 1e-6)
        if abs(float(circle_center[0]) - float(center[0])) > center_tol:
            continue
        if abs(float(circle_center[1]) - float(center[1])) > center_tol:
            continue
        if radius < inradius * 0.75 or radius > inradius * 1.02:
            continue
        candidates.append(circle)
    if not candidates:
        return None
    return max(candidates, key=lambda item: float(item.get("radius") or 0.0))


def _support_plate_height_from_side_views(front: Dict[str, Any], left: Dict[str, Any], height_z: float) -> Optional[float]:
    candidates: List[float] = []
    for view in (front, left):
        for outline in view.get("visible_closed_outlines") or []:
            bbox = outline.get("bbox") or []
            if len(bbox) != 4:
                continue
            z_max = float(bbox[3])
            if height_z * 0.35 <= z_max <= height_z * 0.85:
                candidates.append(z_max)
        for entity in view.get("key_hidden_entities") or []:
            bbox = entity.get("bbox") or []
            if len(bbox) != 4:
                continue
            if abs(float(bbox[1]) - float(bbox[3])) > max(height_z * 0.01, 1e-6):
                continue
            z = float(bbox[1])
            if height_z * 0.35 <= z <= height_z * 0.85:
                candidates.append(z)
    if not candidates:
        return None
    candidates.sort()
    return candidates[len(candidates) // 2]


def _hollow_cylinder_from_left_hint(projected_views: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    front = projected_views.get("front") or {}
    top = projected_views.get("top") or {}
    left = projected_views.get("left") or {}
    width_x = _view_extent(front, "width", _view_extent(top, "width", 0.0))
    depth_y = _view_extent(left, "width", _view_extent(top, "height", 0.0))
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    if width_x <= 0.0 or depth_y <= 0.0 or height_z <= 0.0:
        return None
    circles = [curve for curve in left.get("approximated_curves") or []
               if curve.get("kind") == "approximated_circle"]
    if not circles:
        return None
    outer = max(circles, key=lambda item: float(item.get("radius") or 0.0))
    outer_center = outer.get("center") or []
    outer_radius = float(outer.get("radius") or 0.0)
    if len(outer_center) != 2 or outer_radius <= 0.0:
        return None
    if outer_radius < min(depth_y, height_z) * 0.35:
        return None
    inner = _inner_circle_from_left_outlines(left, outer_center, outer_radius)
    if inner is None:
        return None
    center_u = float(outer_center[0])
    center_z = float(outer_center[1])
    return {
        "kind": "hollow_cylinder_from_left_annulus",
        "confidence": "high",
        "understanding": "空心圆筒/套筒：LEFT 的同心圆环给出 YZ 截面，FRONT/TOP 的矩形和虚线只给出 X 向长度与内孔投影。",
        "construction": {
            "axis": "X",
            "length_x": _round_num(width_x),
            "center_world": [0.0, _round_num(depth_y - center_u), _round_num(center_z)],
            "outer_radius": _round_num(outer_radius),
            "inner_radius": _round_num(inner["radius"]),
            "base_world": [0.0, _round_num(depth_y - center_u), _round_num(center_z)],
            "direction": [1.0, 0.0, 0.0],
            "operation": "Create an X-axis outer cylinder from base_world with length_x and outer_radius, then cut a coaxial X-axis cylinder using inner_radius. Do not build a solid cylinder and do not place the YZ circle in the XY plane.",
        },
        "evidence": [
            "left.approximated_curves contains a large centered circle spanning the view height/depth",
            "left.visible_closed_outlines contains a smaller centered square-like closed loop for the bore",
            "front/top are rectangles with hidden horizontal lines, consistent with a through bore along X",
        ],
    }


def _inner_circle_from_left_outlines(
    left: Dict[str, Any],
    outer_center: List[float],
    outer_radius: float,
) -> Optional[Dict[str, float]]:
    candidates = []
    for outline in left.get("visible_closed_outlines") or []:
        bbox = outline.get("bbox") or []
        if len(bbox) != 4:
            continue
        width = float(bbox[2]) - float(bbox[0])
        height = float(bbox[3]) - float(bbox[1])
        if width <= 0.0 or height <= 0.0:
            continue
        radius = (width + height) * 0.25
        if radius <= 0.0 or radius >= outer_radius * 0.9:
            continue
        if abs(width - height) > max(radius * 0.08, 1e-6):
            continue
        center = [(float(bbox[0]) + float(bbox[2])) * 0.5,
                  (float(bbox[1]) + float(bbox[3])) * 0.5]
        tol = max(outer_radius * 0.08, 1e-6)
        if abs(center[0] - float(outer_center[0])) > tol or abs(center[1] - float(outer_center[1])) > tol:
            continue
        candidates.append({"center_u": center[0], "center_z": center[1], "radius": radius})
    if not candidates:
        return None
    return max(candidates, key=lambda item: item["radius"])


def _regular_hex_prism_from_top_hint(projected_views: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    top = projected_views.get("top") or {}
    front = projected_views.get("front") or {}
    left = projected_views.get("left") or {}
    polygons = [hint for hint in top.get("regular_polygon_hints") or []
                if hint.get("kind") == "regular_hexagon_bbox"]
    if not polygons:
        return None
    has_circle = bool(top.get("visible_circles") or top.get("approximated_curves") or _deduped_circle_sources(top))
    has_arc = any(
        edge.get("kind") == "ARC"
        for view in (front, left)
        for outline in view.get("visible_closed_outlines") or []
        for edge in outline.get("edges") or []
    )
    if has_circle or has_arc:
        return None
    hex_hint = polygons[0]
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    if height_z <= 0.0:
        return None
    return {
        "kind": "regular_hex_prism_from_top",
        "confidence": "high",
        "understanding": "简单六棱柱：TOP 六边形是实体外轮廓，FRONT/LEFT 矩形内竖线是六边形侧棱投影，不是孔、槽或倒角。",
        "construction": {
            "source_view": "top",
            "plane": "XY",
            "vertices_2d": hex_hint.get("recommended_vertices_2d"),
            "height_z": _round_num(height_z),
            "operation": "Create a closed XY hexagon face from vertices_2d and extrude along Z by height_z. Do not add center holes, chamfers, fillets, revolved envelopes, or cuts unless an explicit circle/arc hint exists.",
        },
        "evidence": [
            "top.regular_polygon_hints contains a regular hexagon",
            "top has no visible or approximated circles",
            "front/left contain only rectangular projections without circular arc evidence",
        ],
    }


def _toothed_disk_from_top_hint(projected_views: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    top = projected_views.get("top") or {}
    front = projected_views.get("front") or {}
    left = projected_views.get("left") or {}
    top_width = _view_extent(top, "width", 0.0)
    top_height = _view_extent(top, "height", 0.0)
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    if top_width <= 0.0 or top_height <= 0.0 or height_z <= 0.0:
        return None
    outlines = top.get("visible_closed_outlines") or []
    if not outlines:
        return _toothed_disk_from_open_top_profile(top, front, left, top_width, top_height, height_z)
    full_outline = max(outlines, key=lambda item: int(item.get("edge_count") or 0))
    edge_count = int(full_outline.get("edge_count") or 0)
    if edge_count < 80:
        return None
    bbox = full_outline.get("bbox") or []
    if len(bbox) != 4:
        return None
    bbox_width = float(bbox[2]) - float(bbox[0])
    bbox_height = float(bbox[3]) - float(bbox[1])
    if bbox_width <= 0.0 or bbox_height <= 0.0:
        return None
    if bbox_width < top_width * 0.85 or bbox_height < top_height * 0.85:
        return None
    if abs(bbox_width - bbox_height) > max(top_width, top_height) * 0.08:
        return None
    bore = _largest_centered_top_circle(top, bbox)
    if bore is None:
        return None
    profile_points = full_outline.get("profile_points_2d") or full_outline.get("sample_points_2d") or []
    profile_points = _ensure_profile_points_cover_bbox(
        profile_points, bbox, full_outline.get("sample_points_2d") or [])
    if len(profile_points) < 12:
        return None
    center = [_round_num((float(bbox[0]) + float(bbox[2])) * 0.5),
              _round_num((float(bbox[1]) + float(bbox[3])) * 0.5)]
    return {
        "kind": "toothed_disk_from_top_profile",
        "confidence": "medium",
        "understanding": "齿轮/带齿圆盘：TOP 给出真实齿形外轮廓和中心孔，FRONT/LEFT 主要给出厚度，不应拆成多个矩形块。",
        "source_view": "top",
        "construction": {
            "outer_profile_plane": "XY",
            "outer_profile_points_2d": _round_json(profile_points),
            "extrude_axis": "Z",
            "height_z": _round_num(height_z),
            "center": center,
            "bore_hole": bore,
            "operation": "Create a closed XY wire from outer_profile_points_2d, make a face, extrude along Z by height_z, then cut a Z-axis center bore. Ignore front/left rectangular tooth projections as separate blocks.",
        },
        "evidence": [
            f"top largest closed outline has {edge_count} short LINE edges and spans the full view bbox",
            "top contains a centered approximated circle used as the bore hole",
            "front/left height is small compared with top width/depth, consistent with a thin extruded gear disk",
        ],
    }


def _toothed_disk_from_open_top_profile(
    top: Dict[str, Any],
    front: Dict[str, Any],
    left: Dict[str, Any],
    top_width: float,
    top_height: float,
    height_z: float,
) -> Optional[Dict[str, Any]]:
    arcs = [entity for entity in top.get("key_profile_entities") or [] if entity.get("kind") == "ARC"]
    segments = [entity for entity in top.get("key_profile_entities") or [] if entity.get("kind") in {"LINE", "LWPOLYLINE", "POLYLINE"}]
    if len(arcs) < 6 or len(segments) < 12:
        return None
    bore_candidates = _deduped_circle_sources(top)
    if not bore_candidates:
        return None
    center = [_round_num(top_width * 0.5), _round_num(top_height * 0.5)]
    bore = max(bore_candidates, key=lambda item: float(item.get("radius") or 0.0))
    profile_points = _gear_points_from_radial_entities(arcs + segments, center)
    if len(profile_points) < 24:
        return None
    bbox = _points_bbox_2d(profile_points)
    if bbox[2] - bbox[0] < top_width * 0.85 or bbox[3] - bbox[1] < top_height * 0.85:
        return None
    return {
        "kind": "toothed_disk_from_open_top_profile",
        "confidence": "high",
        "understanding": "齿轮/带齿圆盘：TOP 由开口线段和圆弧给出齿顶/齿根外轮廓，中心圆为贯穿孔，FRONT/LEFT 给厚度。",
        "source_view": "top",
        "construction": {
            "outer_profile_plane": "XY",
            "outer_profile_points_2d": _round_json(profile_points),
            "extrude_axis": "Z",
            "height_z": _round_num(height_z),
            "center": center,
            "bore_hole": {
                "axis": "Z",
                "center": _round_json(bore.get("center") or center),
                "radius": _round_num(float(bore.get("radius") or 0.0)),
            },
            "operation": "Create a closed XY polygon from outer_profile_points_2d, extrude along Z by height_z, then cut the Z-axis center bore. Do not replace the gear by a box.",
        },
        "evidence": [
            "top has no single closed outline, but contains repeated radial tooth line segments and arcs around the same center",
            "front/left are thin rectangular projections, consistent with an extruded gear disk",
            "top contains one centered visible circle used as the bore hole",
        ],
    }


def _largest_centered_top_circle(top: Dict[str, Any], bbox: List[float]) -> Optional[Dict[str, Any]]:
    center_x = (float(bbox[0]) + float(bbox[2])) * 0.5
    center_y = (float(bbox[1]) + float(bbox[3])) * 0.5
    max_radius = max(float(bbox[2]) - float(bbox[0]), float(bbox[3]) - float(bbox[1])) * 0.5
    candidates = []
    for circle in _deduped_circle_sources(top):
        center = circle.get("center") or []
        radius = float(circle.get("radius") or 0.0)
        if len(center) != 2 or radius <= 0.0 or radius >= max_radius * 0.85:
            continue
        tol = max(max_radius * 0.08, 1e-6)
        if abs(float(center[0]) - center_x) > tol or abs(float(center[1]) - center_y) > tol:
            continue
        candidates.append(circle)
    if not candidates:
        return None
    circle = max(candidates, key=lambda item: float(item.get("radius") or 0.0))
    return {
        "axis": "Z",
        "center": _round_json(circle.get("center") or []),
        "radius": _round_num(float(circle.get("radius") or 0.0)),
    }


def _gear_points_from_radial_entities(entities: List[Dict[str, Any]], center: List[float]) -> List[List[float]]:
    cx, cy = float(center[0]), float(center[1])
    candidates: List[Tuple[float, float, float, float]] = []
    for entity in entities:
        for point in _entity_profile_points(entity):
            x, y = float(point[0]), float(point[1])
            radius = math.hypot(x - cx, y - cy)
            if radius <= 1e-9:
                continue
            angle = math.atan2(y - cy, x - cx) % (2.0 * math.pi)
            candidates.append((angle, radius, x, y))
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item[0], -item[1]))
    deduped: List[Tuple[float, float, float, float]] = []
    angle_tol = math.radians(0.4)
    for item in candidates:
        if deduped and abs(item[0] - deduped[-1][0]) <= angle_tol:
            if item[1] > deduped[-1][1]:
                deduped[-1] = item
        else:
            deduped.append(item)
    # Merge wrap-around duplicate angles.
    if len(deduped) > 1 and abs((deduped[0][0] + 2.0 * math.pi) - deduped[-1][0]) <= angle_tol:
        if deduped[-1][1] > deduped[0][1]:
            deduped[0] = (deduped[0][0], deduped[-1][1], deduped[-1][2], deduped[-1][3])
        deduped.pop()
    return [[item[2], item[3]] for item in deduped]


def _entity_profile_points(entity: Dict[str, Any]) -> List[List[float]]:
    if entity.get("kind") == "ARC" and len(entity.get("center") or []) == 2:
        center = entity.get("center") or []
        radius = float(entity.get("radius") or 0.0)
        start = float(entity.get("start_angle") or 0.0)
        end = float(entity.get("end_angle") or 0.0)
        if radius <= 0.0:
            return []
        start_rad = math.radians(start)
        end_rad = math.radians(end)
        if end_rad < start_rad:
            end_rad += 2.0 * math.pi
        span = max(end_rad - start_rad, 0.0)
        # Dense enough that each original outer arc remains visibly curved in
        # the generated polygon; 5-degree steps are a good compact compromise.
        steps = max(4, int(math.ceil(span / math.radians(5.0))))
        angles = [start_rad + span * i / float(steps) for i in range(steps + 1)]
        return [[
            float(center[0]) + radius * math.cos(angle),
            float(center[1]) + radius * math.sin(angle),
        ] for angle in angles]
    points: List[List[float]] = []
    if len(entity.get("points") or []) >= 2:
        points.extend([[float(p[0]), float(p[1])] for p in entity.get("points") or [] if len(p or []) == 2])
    for key in ("p0", "p1"):
        point = entity.get(key) or []
        if len(point) == 2:
            points.append([float(point[0]), float(point[1])])
    return points


def _points_bbox_2d(points: List[List[float]]) -> List[float]:
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def _ensure_profile_points_cover_bbox(
    points: List[Any],
    bbox: List[float],
    extra_points: Optional[List[Any]] = None,
) -> List[List[float]]:
    """Add sampled outline points nearest to bbox extremes.

    Long toothed outlines are down-sampled before being sent to the LLM.  A
    uniform sample can miss the true left/right/top/bottom extreme vertices,
    so scripts generated from the sampled polygon get a smaller bbox and then
    fail the dimension contract.  Keep the compact profile, but force the
    sampled polygon to include points near all four bbox extremes.
    """
    if len(bbox) != 4:
        return [[float(p[0]), float(p[1])] for p in points if len(p or []) == 2]
    cleaned = [[float(p[0]), float(p[1])] for p in points if len(p or []) == 2]
    candidates = list(cleaned)
    if extra_points:
        candidates.extend([[float(p[0]), float(p[1])] for p in extra_points if len(p or []) == 2])
    if not cleaned:
        return []
    min_x, min_y, max_x, max_y = [float(value) for value in bbox]
    targets = [
        (min_x, (min_y + max_y) * 0.5),
        (max_x, (min_y + max_y) * 0.5),
        ((min_x + max_x) * 0.5, min_y),
        ((min_x + max_x) * 0.5, max_y),
    ]
    result = list(cleaned)
    for tx, ty in targets:
        nearest = min(candidates, key=lambda p: (p[0] - tx) ** 2 + (p[1] - ty) ** 2)
        if all(abs(nearest[0] - p[0]) > 1e-9 or abs(nearest[1] - p[1]) > 1e-9 for p in result):
            insert_at = min(range(len(result)), key=lambda idx: (result[idx][0] - tx) ** 2 + (result[idx][1] - ty) ** 2)
            # Insert after the closest predecessor in the existing order to
            # preserve the outline winding as much as possible.
            result.insert(min(insert_at + 1, len(result)), nearest)
    return result


def _hex_nut_arc_revolve_hint(projected_views: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    top = projected_views.get("top") or {}
    front = projected_views.get("front") or {}
    left = projected_views.get("left") or {}
    polygons = [hint for hint in top.get("regular_polygon_hints") or []
                if hint.get("kind") == "regular_hexagon_bbox"]
    if not polygons:
        return None
    hex_hint = polygons[0]
    circles = top.get("visible_circles") or []
    boundary_circles = [circle for circle in circles if _circle_touches_view_boundary(circle, top)]
    hole_circles = [circle for circle in circles if not _circle_touches_view_boundary(circle, top)]
    if not boundary_circles or not hole_circles:
        return None
    chamfer_distance = _side_arc_chamfer_distance(front, left)
    if chamfer_distance is None:
        return None
    boundary_circle = max(boundary_circles, key=lambda item: float(item.get("radius") or 0.0))
    hole_circle = min(hole_circles, key=lambda item: float(item.get("radius") or 0.0))
    top_radius = float(boundary_circle.get("radius") or 0.0)
    outer_radius = float(hex_hint.get("circumradius") or 0.0)
    if top_radius <= 0.0 or outer_radius <= 0.0 or top_radius >= outer_radius:
        return None
    center = hex_hint.get("center") or hole_circle.get("center") or [0.0, 0.0]
    height = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    return {
        "kind": "hex_nut_arc_revolve_chamfer",
        "confidence": "high",
        "understanding": "六角螺母：TOP 为六边形主体和中心通孔，大同心圆是端面圆弧倒角参考；FRONT/LEFT 给出上下圆弧包络。",
        "evidence": [
            "top.regular_polygon_hints contains regular_hexagon_bbox",
            "top.visible_circles contains one inner hole circle and one boundary/reference circle",
            "front/left visible outlines contain end arcs and inset side lines",
        ],
        "base_profile": {
            "source_view": "top",
            "plane": "XY",
            "vertices_2d": hex_hint.get("recommended_vertices_2d"),
            "center": center,
            "height_z": _round_num(height),
            "outer_radius": _round_num(outer_radius),
        },
        "through_hole": {
            "axis": "Z",
            "center": hole_circle.get("center"),
            "radius": _round_num(float(hole_circle.get("radius") or 0.0)),
        },
        "arc_revolve_chamfer": {
            "distance": _round_num(chamfer_distance),
            "top_radius": _round_num(top_radius),
            "outer_radius": _round_num(outer_radius),
            "operation": "Build an R-Z arc envelope, revolve it 360 degrees around Z through the hex center, then use final_shape = prism_with_hole.common(envelope).",
            "avoid": "Do not use shape.makeChamfer or shape.makeFillet for this feature; those create straight/edge fillets and do not match the FRONT/LEFT circular arc envelope.",
            "rz_profile_template": [
                {"kind": "line", "from": [0.0, 0.0], "to": [_round_num(top_radius), 0.0]},
                {"kind": "arc", "from": [_round_num(top_radius), 0.0], "mid": [_round_num((top_radius + outer_radius) * 0.5), _round_num(chamfer_distance * 0.35)], "to": [_round_num(outer_radius), _round_num(chamfer_distance)]},
                {"kind": "line", "from": [_round_num(outer_radius), _round_num(chamfer_distance)], "to": [_round_num(outer_radius), _round_num(height - chamfer_distance)]},
                {"kind": "arc", "from": [_round_num(outer_radius), _round_num(height - chamfer_distance)], "mid": [_round_num((top_radius + outer_radius) * 0.5), _round_num(height - chamfer_distance * 0.35)], "to": [_round_num(top_radius), _round_num(height)]},
                {"kind": "line", "from": [_round_num(top_radius), _round_num(height)], "to": [0.0, _round_num(height)]},
                {"kind": "line", "from": [0.0, _round_num(height)], "to": [0.0, 0.0]},
            ],
            "implementation_note": "R-Z points are radial distance from the hex center and Z height. Convert each [r,z] to App.Vector(center_x + r, center_y, z), make a closed Wire/Face, then call env_face.revolve(App.Vector(center_x, center_y, 0), App.Vector(0,0,1), 360).",
        },
    }


def _side_arc_chamfer_distance(*views: Dict[str, Any]) -> Optional[float]:
    candidates: List[float] = []
    for view in views:
        height = float(view.get("height") or 0.0)
        if height <= 0.0:
            continue
        has_arc = any(
            edge.get("kind") == "ARC"
            for outline in view.get("visible_closed_outlines") or []
            for edge in outline.get("edges") or []
        )
        if not has_arc:
            continue
        for outline in view.get("visible_closed_outlines") or []:
            for edge in outline.get("edges") or []:
                if edge.get("kind") != "LINE":
                    continue
                for point in (edge.get("p0"), edge.get("p1")):
                    if not point or len(point) != 2:
                        continue
                    z = float(point[1])
                    if 1e-6 < z < height * 0.45:
                        candidates.append(z)
                    upper = height - z
                    if 1e-6 < upper < height * 0.45:
                        candidates.append(upper)
    if not candidates:
        return None
    candidates.sort()
    return float(candidates[len(candidates) // 2])


def _through_hole_hints(
    projected_views: Dict[str, Dict[str, Any]],
    model_understanding_hints: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    top = projected_views.get("top") or {}
    front = projected_views.get("front") or {}
    left = projected_views.get("left") or {}
    width_x = _view_extent(front, "width", _view_extent(top, "width", 1.0))
    depth_y = _view_extent(left, "width", _view_extent(top, "height", 1.0))
    height_z = _view_extent(front, "height", _view_extent(left, "height", 1.0))
    margin = max(width_x, depth_y, height_z, 1.0) * 0.05
    hints: List[Dict[str, Any]] = []
    solid_top_circles = _solid_top_circle_keys(model_understanding_hints or [])
    for circle in _deduped_circle_sources(top):
        center = circle.get("center") or []
        radius = float(circle.get("radius") or 0.0)
        if len(center) == 2 and radius > 0.0:
            cx, cy = float(center[0]), float(center[1])
            if _circle_key(cx, cy, radius) in solid_top_circles:
                continue
            hints.append({
                "source_view": "top",
                "axis": "Z",
                "radius": _round_num(radius),
                "center_world": [_round_num(cx), _round_num(cy), 0.0],
                "base_world": [_round_num(cx), _round_num(cy), _round_num(-margin)],
                "height": _round_num(height_z + 2.0 * margin),
                "rule": "base.z < solid_z_min and base.z + height > solid_z_max",
            })
    for circle in _deduped_circle_sources(front):
        center = circle.get("center") or []
        radius = float(circle.get("radius") or 0.0)
        if len(center) == 2 and radius > 0.0:
            cx, cz = float(center[0]), float(center[1])
            hints.append({
                "source_view": "front",
                "axis": "Y",
                "radius": _round_num(radius),
                "center_world": [_round_num(cx), 0.0, _round_num(cz)],
                "base_world": [_round_num(cx), _round_num(-margin), _round_num(cz)],
                "height": _round_num(depth_y + 2.0 * margin),
                "rule": "base.y < solid_y_min and base.y + height > solid_y_max",
            })
    for circle in _deduped_circle_sources(left):
        center = circle.get("center") or []
        radius = float(circle.get("radius") or 0.0)
        if len(center) == 2 and radius > 0.0:
            uy, cz = float(center[0]), float(center[1])
            cy = depth_y - uy
            hints.append({
                "source_view": "left",
                "axis": "X",
                "radius": _round_num(radius),
                "center_world": [0.0, _round_num(cy), _round_num(cz)],
                "base_world": [_round_num(-margin), _round_num(cy), _round_num(cz)],
                "height": _round_num(width_x + 2.0 * margin),
                "rule": "base.x < solid_x_min and base.x + height > solid_x_max",
            })
    return hints


def _solid_top_circle_keys(model_understanding_hints: List[Dict[str, Any]]) -> set[Tuple[float, float, float]]:
    keys: set[Tuple[float, float, float]] = set()
    for hint in model_understanding_hints:
        if hint.get("kind") != "central_cylinder_on_hex_prism":
            continue
        cylinder = ((hint.get("construction") or {}).get("cylinder") or {})
        center = cylinder.get("center") or []
        radius = float(cylinder.get("radius") or 0.0)
        if len(center) == 2 and radius > 0.0:
            keys.add(_circle_key(float(center[0]), float(center[1]), radius))
    return keys


def _circle_key(cx: float, cy: float, radius: float) -> Tuple[float, float, float]:
    return (round(cx, 5), round(cy, 5), round(radius, 5))


def _deduped_circle_sources(view: Dict[str, Any]) -> List[Dict[str, Any]]:
    circles: List[Dict[str, Any]] = []
    for circle in view.get("visible_circles") or []:
        if not _circle_touches_view_boundary(circle, view):
            circles.append(circle)
    for curve in view.get("approximated_curves") or []:
        if curve.get("kind") != "approximated_circle":
            continue
        if not _circle_touches_view_boundary(curve, view):
            circles.append(curve)

    grouped: List[Dict[str, Any]] = []
    for circle in sorted(circles, key=lambda item: float(item.get("radius") or 0.0)):
        center = circle.get("center") or []
        radius = float(circle.get("radius") or 0.0)
        if len(center) != 2 or radius <= 0.0:
            continue
        if any(_same_circle_center(circle, existing) for existing in grouped):
            continue
        grouped.append(circle)
    return grouped


def _same_circle_center(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    ac = a.get("center") or []
    bc = b.get("center") or []
    if len(ac) != 2 or len(bc) != 2:
        return False
    ar = float(a.get("radius") or 0.0)
    br = float(b.get("radius") or 0.0)
    tol = max(ar, br, 1e-6) * 0.2
    return abs(float(ac[0]) - float(bc[0])) <= tol and abs(float(ac[1]) - float(bc[1])) <= tol


def _circle_touches_view_boundary(circle: Dict[str, Any], view: Dict[str, Any]) -> bool:
    bbox = circle.get("bbox") or []
    if len(bbox) != 4:
        return False
    width = float(view.get("width") or 0.0)
    height = float(view.get("height") or 0.0)
    if width <= 0.0 or height <= 0.0:
        return False
    tol = max(width, height, 1.0) * 0.01
    touches_x = abs(float(bbox[0])) <= tol and abs(float(bbox[2]) - width) <= tol
    touches_y = abs(float(bbox[1])) <= tol and abs(float(bbox[3]) - height) <= tol
    return touches_x or touches_y


def _load_part_knowledge() -> str:
    path = os.path.join(os.path.dirname(os.path.dirname(HERE)), "prompts", "part_knowledge.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "（无）"


def generate_freecad_script(
    llm: Any,
    context: Dict[str, Any],
    base_name: str,
    fcstd_path: str,
    debug_dir: Optional[str] = None,
    use_cache: bool = True,
) -> Tuple[Optional[str], str]:
    """Return (script_or_none, message) from the direct FreeCAD generator."""
    if not getattr(llm, "enabled", False):
        return None, f"LLM 已禁用：{getattr(llm, 'disabled_reason', None)}"

    try:
        prompt = load_prompt("freecad_script_generator")
    except Exception as exc:
        return None, f"提示词加载失败：{exc}"

    if use_cache:
        cache_key = _script_cache_key(llm, prompt, context, base_name)
        cached = _read_cached_script(cache_key, base_name, fcstd_path)
        if cached is not None:
            ok, reason = validate_generated_script(cached)
            if ok:
                _write_debug_text(debug_dir, "llm_cache_key.txt", cache_key)
                return cached, f"复用 LLM 成功脚本缓存（{llm.model}）"

    user_msg = render_template(prompt.user_template, {
        "base_name": base_name,
        "fcstd_path": fcstd_path,
        "auto_context": context,
    })
    messages: List[Dict[str, str]] = [{"role": "system", "content": prompt.system}]
    for inp, out in prompt.examples:
        messages.append({"role": "user", "content": inp})
        messages.append({"role": "assistant", "content": out})
    messages.append({"role": "user", "content": user_msg})

    try:
        with _alarm_timeout(_REQUEST_TIMEOUT_SECONDS):
            content = llm.complete_text(
                messages,
                max_tokens=_MAX_SCRIPT_TOKENS,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
    except Exception as exc:
        return None, f"LLM 请求失败：{exc}"

    _write_debug_text(debug_dir, "llm_raw_response.txt", content)
    script = _sanitize_generated_script(strip_code_fence(content))
    ok, reason = validate_generated_script(script)
    if not ok:
        repaired, repair_msg = _repair_generated_script(
            llm, messages, content, reason, debug_dir)
        if repaired is None:
            return None, f"LLM 脚本未通过安全/结构校验：{reason}；{repair_msg}"
        return repaired, repair_msg
    return script, f"LLM 直接建模脚本生成完成（{llm.model}）"


def repair_freecad_script_after_execution(
    llm: Any,
    context: Dict[str, Any],
    base_name: str,
    fcstd_path: str,
    bad_script: str,
    reason: str,
    debug_dir: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """Ask the LLM to repair a script that executed but violated model checks."""
    if not getattr(llm, "enabled", False):
        return None, f"LLM 已禁用：{getattr(llm, 'disabled_reason', None)}"
    try:
        prompt = load_prompt("freecad_script_generator")
    except Exception as exc:
        return None, f"提示词加载失败：{exc}"
    user_msg = render_template(prompt.user_template, {
        "base_name": base_name,
        "fcstd_path": fcstd_path,
        "auto_context": context,
    })
    messages: List[Dict[str, str]] = [{"role": "system", "content": prompt.system}]
    for inp, out in prompt.examples:
        messages.append({"role": "user", "content": inp})
        messages.append({"role": "assistant", "content": out})
    messages.append({"role": "user", "content": user_msg})
    messages.append({"role": "assistant", "content": bad_script[:6000]})
    messages.append({
        "role": "user",
        "content": (
            "上一次脚本没有通过执行后校验：" + reason + "\n"
            "请重新输出一份完整 Python 脚本，不要解释。必须修正实际建模代码，而不是只修改 DIMENSIONS_USED。\n"
            "硬性要求：\n"
            "1. 如果 TOP 给出 footprint，而 FRONT/LEFT 给出完整高度，沿 Z 的实体高度必须使用 dimension_constraints.overall_size.height_z。\n"
            "2. DIMENSIONS_USED 中记录的 width_x/depth_y/height_z 必须真实反映 Result.Shape.BoundBox 的 X/Y/Z 长度。\n"
            "3. 不要用局部闭合轮廓的小高度、视觉线宽、0.0075/0.033 等小数替代 FRONT/LEFT 的完整拉伸长度。\n"
            "4. 保持原有孔、圆、轮廓位置来自上下文 JSON；只修正错误的拉伸轴和拉伸长度。\n"
            "5. 尺寸修复时不得改变 TOP 最大外轮廓的形状；矩形/正方形仍必须用矩形/正方形顶点，不要替换成圆或 12 边形。\n"
            "6. 如果错误原因是 Python/FreeCAD API 不存在，改用稳定 API；不要调用 Vector.rotate、Part.Vertex 造 Wire 或 _edge(Part)。\n"
            "7. 只输出最终正确代码，不要 Markdown 解释。"
        ),
    })
    try:
        with _alarm_timeout(_REQUEST_TIMEOUT_SECONDS):
            content = llm.complete_text(
                messages,
                max_tokens=_MAX_SCRIPT_TOKENS,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
    except Exception as exc:
        return None, f"尺寸校验后自动修复失败：{exc}"

    _write_debug_text(debug_dir, "llm_raw_response_dimension_retry.txt", content)
    script = _sanitize_generated_script(strip_code_fence(content))
    ok, repair_reason = validate_generated_script(script)
    if not ok:
        return None, f"尺寸校验后自动修复仍未通过脚本校验：{repair_reason}"
    return script, f"LLM 直接建模脚本生成完成（{llm.model}，尺寸校验后自动修复一次）"


def validate_fcstd_dimensions(fcstd_path: str, context: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate Result.Shape bbox lengths against dimension_constraints."""
    constraints = (context.get("dimension_constraints") or {}).get("overall_size") or {}
    expected = {
        "width_x": float(constraints.get("width_x") or 0.0),
        "depth_y": float(constraints.get("depth_y") or 0.0),
        "height_z": float(constraints.get("height_z") or 0.0),
    }
    tolerance = float(constraints.get("tolerance") or 0.0)
    if tolerance <= 0.0:
        tolerance = max(max(expected.values(), default=0.0) * 0.02, 1e-6)
    import FreeCAD as App  # type: ignore
    doc = App.openDocument(fcstd_path)
    try:
        result = doc.getObject("Result")
        if result is None:
            return False, "Result object not found", {}
        shape = getattr(result, "Shape", None)
        if shape is None or shape.isNull() or not shape.Solids:
            return False, "Result object has no solid geometry", {}
        bb = shape.BoundBox
        actual = {
            "width_x": float(bb.XLength),
            "depth_y": float(bb.YLength),
            "height_z": float(bb.ZLength),
        }
    finally:
        App.closeDocument(doc.Name)
    failures = []
    for key in ("width_x", "depth_y", "height_z"):
        exp = expected[key]
        if exp <= 0.0:
            continue
        diff = abs(actual[key] - exp)
        if diff > tolerance:
            failures.append(f"{key}: expected {exp:.6g}, actual {actual[key]:.6g}, diff {diff:.6g}, tolerance {tolerance:.6g}")
    details = {"expected": expected, "actual": actual, "tolerance": tolerance, "failures": failures}
    if failures:
        return False, "; ".join(failures), details
    return True, "OK", details


def validate_fcstd_arc_edges(fcstd_path: str, context: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Validate that ARC-bearing source profiles remain circular edges."""
    expected_arc_count = _expected_profile_arc_count(context)
    if expected_arc_count <= 0:
        return True, "SKIP", {"expected_arc_count": 0, "actual_circular_edge_count": 0}
    import FreeCAD as App  # type: ignore
    doc = App.openDocument(fcstd_path)
    try:
        result = doc.getObject("Result")
        if result is None:
            return False, "Result object not found", {"expected_arc_count": expected_arc_count}
        shape = getattr(result, "Shape", None)
        if shape is None or shape.isNull():
            return False, "Result object has no geometry", {"expected_arc_count": expected_arc_count}
        type_ids = []
        circular_edge_count = 0
        for edge in getattr(shape, "Edges", []) or []:
            curve = getattr(edge, "Curve", None)
            type_id = str(getattr(curve, "TypeId", ""))
            if type_id:
                type_ids.append(type_id)
            if type_id == "Part::GeomCircle":
                circular_edge_count += 1
    finally:
        App.closeDocument(doc.Name)
    details = {
        "expected_arc_count": expected_arc_count,
        "actual_circular_edge_count": circular_edge_count,
        "curve_type_counts": {type_id: type_ids.count(type_id) for type_id in sorted(set(type_ids))},
    }
    if circular_edge_count < expected_arc_count:
        return False, f"expected at least {expected_arc_count} circular arc edges, actual {circular_edge_count}", details
    return True, "OK", details


def _expected_profile_arc_count(context: Dict[str, Any]) -> int:
    projected = context.get("projected_views") or {}
    top = projected.get("top") or {}
    ordered_edges = top.get("ordered_profile_edges") or []
    count = sum(1 for edge in ordered_edges if edge.get("kind") == "ARC")
    if count > 0:
        return count
    for outline in top.get("visible_closed_outlines") or []:
        edges = outline.get("edges") or []
        count = sum(1 for edge in edges if edge.get("kind") == "ARC")
        if count > 0:
            return count
    return 0


def normalize_fcstd_dimensions(fcstd_path: str, context: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Scale the generated Result shape to the dimension contract when possible."""
    constraints = (context.get("dimension_constraints") or {}).get("overall_size") or {}
    expected = {
        "width_x": float(constraints.get("width_x") or 0.0),
        "depth_y": float(constraints.get("depth_y") or 0.0),
        "height_z": float(constraints.get("height_z") or 0.0),
    }
    tolerance = float(constraints.get("tolerance") or 0.0)
    if tolerance <= 0.0:
        tolerance = max(max(expected.values(), default=0.0) * 0.02, 1e-6)
    import FreeCAD as App  # type: ignore
    doc = App.openDocument(fcstd_path)
    try:
        result = doc.getObject("Result")
        if result is None:
            return False, "Result object not found", {}
        shape = getattr(result, "Shape", None)
        if shape is None or shape.isNull() or not shape.Solids:
            return False, "Result object has no solid geometry", {}
        bb = shape.BoundBox
        actual = {
            "width_x": float(bb.XLength),
            "depth_y": float(bb.YLength),
            "height_z": float(bb.ZLength),
        }
        zero_axes = [key for key in ("width_x", "depth_y", "height_z")
                     if expected[key] > 0.0 and actual[key] <= 1e-9]
        if zero_axes:
            thickened, thicken_reason = _thicken_zero_axis_shape(result, shape, zero_axes, expected)
            if not thickened:
                return False, thicken_reason, {
                    "expected": expected,
                    "actual_before": actual,
                    "tolerance": tolerance,
                    "failures": [thicken_reason],
                }
            doc.recompute()
            shape = result.Shape
            bb = shape.BoundBox
            actual = {
                "width_x": float(bb.XLength),
                "depth_y": float(bb.YLength),
                "height_z": float(bb.ZLength),
            }
        scale = {}
        failures = []
        for key in ("width_x", "depth_y", "height_z"):
            exp = expected[key]
            act = actual[key]
            if exp <= 0.0:
                scale[key] = 1.0
                continue
            if act <= 1e-9:
                failures.append(f"{key}: expected {exp:.6g}, actual zero-length axis")
                scale[key] = 1.0
                continue
            scale[key] = exp / act
        if failures:
            return False, "; ".join(failures), {
                "expected": expected,
                "actual_before": actual,
                "tolerance": tolerance,
                "failures": failures,
            }
        matrix = App.Matrix(
            scale["width_x"], 0.0, 0.0, float(bb.XMin) * (1.0 - scale["width_x"]),
            0.0, scale["depth_y"], 0.0, float(bb.YMin) * (1.0 - scale["depth_y"]),
            0.0, 0.0, scale["height_z"], float(bb.ZMin) * (1.0 - scale["height_z"]),
            0.0, 0.0, 0.0, 1.0,
        )
        result.Shape = shape.transformGeometry(matrix)
        doc.recompute()
        doc.saveAs(fcstd_path)
    finally:
        App.closeDocument(doc.Name)
    ok, reason, details = validate_fcstd_dimensions(fcstd_path, context)
    details["normalization"] = {
        "expected": expected,
        "actual_before": actual,
        "scale": scale,
    }
    if not ok:
        return False, reason, details
    return True, "OK", details


def _thicken_zero_axis_shape(
    result: Any,
    shape: Any,
    zero_axes: List[str],
    expected: Dict[str, float],
) -> Tuple[bool, str]:
    if len(zero_axes) != 1:
        return False, "multiple zero-length axes cannot be repaired by thickness extrusion"
    axis = zero_axes[0]
    vector_by_axis = {
        "width_x": (expected[axis], 0.0, 0.0),
        "depth_y": (0.0, expected[axis], 0.0),
        "height_z": (0.0, 0.0, expected[axis]),
    }
    import FreeCAD as App  # type: ignore
    vector = App.Vector(*vector_by_axis[axis])
    faces = list(getattr(shape, "Faces", []) or [])
    if not faces:
        return False, f"{axis}: expected {expected[axis]:.6g}, actual zero-length axis and no face to extrude"
    candidates = []
    for face in faces:
        bb = face.BoundBox
        lengths = {
            "width_x": float(bb.XLength),
            "depth_y": float(bb.YLength),
            "height_z": float(bb.ZLength),
        }
        if lengths[axis] > 1e-8:
            continue
        other_lengths = [length for key, length in lengths.items() if key != axis]
        if any(length <= 1e-9 for length in other_lengths):
            continue
        candidates.append(face)
    if not candidates:
        return False, f"{axis}: expected {expected[axis]:.6g}, actual zero-length axis and no planar face matches missing thickness axis"
    face = max(candidates, key=lambda item: float(getattr(item, "Area", 0.0)))
    try:
        thickened = face.extrude(vector)
    except Exception as exc:
        return False, f"{axis}: failed to extrude zero-thickness face: {exc}"
    if thickened.isNull() or not thickened.Solids:
        return False, f"{axis}: face extrusion did not produce solid geometry"
    result.Shape = thickened
    return True, "OK"


def cache_successful_freecad_script(
    llm: Any,
    context: Dict[str, Any],
    base_name: str,
    script: str,
) -> None:
    try:
        prompt = load_prompt("freecad_script_generator")
    except Exception:
        return
    key = _script_cache_key(llm, prompt, context, base_name)
    _write_cached_script(key, script)


def generate_outline_fallback_script(
    context: Dict[str, Any],
    base_name: str,
    fcstd_path: str,
) -> Optional[str]:
    """Generate a deterministic script for a closed LINE/ARC outline."""
    projected = context.get("projected_views") or {}
    view_name = "top"
    view = projected.get("top") or {}

    for hint in context.get("model_understanding_hints") or []:
        if hint.get("kind") == "front_arc_profile_plate_with_y_holes":
            view_name = "front"
            view = projected.get("front") or {}
            break

    if view_name != "front":
        front = projected.get("front") or {}
        front_edges = front.get("ordered_profile_edges") or []
        front_circles = front.get("visible_circles") or []
        if any(edge.get("kind") == "ARC" for edge in front_edges) and front_circles:
            view_name = "front"
            view = front

    edges = view.get("ordered_profile_edges") or []
    outlines = view.get("visible_closed_outlines") or []
    if not edges:
        for candidate in outlines:
            candidate_edges = candidate.get("edges") or []
            if candidate.get("edges_complete") is True and candidate_edges:
                edges = candidate_edges
                break
    if edges:
        points = _discretize_outline_edges(edges)
    else:
        points = _outline_points_from_model_hint(context)
    if len(points) < 4:
        return None

    dims = ((context.get("dimension_constraints") or {}).get("overall_size") or {})
    width_x = float(dims.get("width_x") or (projected.get("top") or {}).get("width") or 0.0)
    depth_y = float(dims.get("depth_y") or (projected.get("top") or {}).get("height") or 0.0)
    height_z = float(dims.get("height_z") or 0.0)
    if width_x <= 0.0 or depth_y <= 0.0 or height_z <= 0.0:
        return None

    if view_name == "front":
        extrusion = (0.0, depth_y, 0.0)
        plane = "XZ"
        holes = [h for h in context.get("hole_hints") or [] if h.get("axis") == "Y"]
        understanding = "基于FRONT有序线弧轮廓沿Y拉伸的异形连接板，孔按hole_hints切除"
    else:
        extrusion = (0.0, 0.0, height_z)
        plane = "XY"
        holes = [h for h in context.get("hole_hints") or [] if h.get("axis") == "Z"]
        understanding = "基于TOP有序线弧轮廓拉伸的异形板，中心孔按hole_hints切除"

    edges_literal = repr(_round_json(edges))
    points_literal = repr(_round_json(points))
    hole_lines = "final_shape = body\n"
    dim_extra = ""
    if holes:
        cut_lines: List[str] = []
        hole_dims: List[str] = []
        for idx, hole in enumerate(holes):
            radius = float(hole.get("radius") or 0.0)
            base = hole.get("base_world") or []
            center = hole.get("center_world") or []
            cutter_height = float(hole.get("height") or 0.0)
            if radius <= 0.0 or len(base) != 3 or len(center) < 3 or cutter_height <= 0.0:
                continue
            axis = hole.get("axis")
            direction = (0, 1, 0) if axis == "Y" else (0, 0, 1)
            cut_lines.append(
                f"cutter_{idx} = Part.makeCylinder({radius!r}, {cutter_height!r}, "
                f"App.Vector({float(base[0])!r}, {float(base[1])!r}, {float(base[2])!r}), "
                f"App.Vector({direction[0]}, {direction[1]}, {direction[2]}))"
            )
            cut_lines.append(f"final_shape = final_shape.cut(cutter_{idx})")
            hole_dims.append(f'{{"radius": {radius!r}, "center": [{float(center[0])!r}, {float(center[1])!r}, {float(center[2])!r}]}}')
        if cut_lines:
            hole_lines = "final_shape = body\n" + "\n".join(cut_lines) + "\n"
            dim_extra = f', "holes": [{", ".join(hole_dims)}]'
    ex, ey, ez = extrusion
    return f'''import FreeCAD as App
import Part
import math

BASE_NAME = {base_name!r}
FCSTD_PATH = {fcstd_path!r}
MODEL_UNDERSTANDING = {understanding!r}
DIMENSIONS_USED = {{"width_x": {width_x!r}, "depth_y": {depth_y!r}, "height_z": {height_z!r}{dim_extra}}}


def _vec(point):
    if {plane!r} == "XZ":
        return App.Vector(float(point[0]), 0.0, float(point[1]))
    return App.Vector(float(point[0]), float(point[1]), 0.0)


def _arc_midpoint(edge):
    center = edge["center"]
    radius = float(edge["radius"])
    p0 = edge["p0"]
    p1 = edge["p1"]
    a0 = math.atan2(float(p0[1]) - float(center[1]), float(p0[0]) - float(center[0]))
    a1 = math.atan2(float(p1[1]) - float(center[1]), float(p1[0]) - float(center[0]))
    if edge.get("clockwise"):
        span = (a0 - a1) % (2.0 * math.pi)
        angle = a0 - span * 0.5
    else:
        span = (a1 - a0) % (2.0 * math.pi)
        angle = a0 + span * 0.5
    return _vec([
        float(center[0]) + radius * math.cos(angle),
        float(center[1]) + radius * math.sin(angle),
    ])


def _point_gap(a, b):
    return math.hypot(float(a[0]) - float(b[0]), float(a[1]) - float(b[1]))


def _copy_edge(edge):
    item = dict(edge)
    if edge.get("p0"):
        item["p0"] = [float(edge["p0"][0]), float(edge["p0"][1])]
    if edge.get("p1"):
        item["p1"] = [float(edge["p1"][0]), float(edge["p1"][1])]
    return item


def _snap_profile_edges(profile_edges, tol):
    snapped = [_copy_edge(edge) for edge in profile_edges]
    if len(snapped) < 3:
        return snapped
    for idx in range(len(snapped)):
        cur = snapped[idx]
        nxt = snapped[(idx + 1) % len(snapped)]
        p = cur.get("p1")
        q = nxt.get("p0")
        if not p or not q:
            continue
        gap = _point_gap(p, q)
        if gap <= tol:
            merged = [(float(p[0]) + float(q[0])) * 0.5, (float(p[1]) + float(q[1])) * 0.5]
            cur["p1"] = merged
            nxt["p0"] = merged
        else:
            raise ValueError("线弧轮廓断点过大，不能吸附")
    return snapped


def _wire_from_edges(profile_edges):
    fc_edges = []
    for edge in profile_edges:
        p0 = edge.get("p0")
        p1 = edge.get("p1")
        if not p0 or not p1:
            continue
        v0 = _vec(p0)
        v1 = _vec(p1)
        if v0.distanceToPoint(v1) <= 1e-9:
            continue
        if edge.get("kind") == "ARC" and edge.get("center") and float(edge.get("radius") or 0.0) > 0.0:
            vm = _arc_midpoint(edge)
            try:
                if edge.get("clockwise"):
                    fc_edges.append(Part.Arc(v1, vm, v0).toShape())
                else:
                    fc_edges.append(Part.Arc(v0, vm, v1).toShape())
            except Exception:
                fc_edges.append(Part.LineSegment(v0, v1).toShape())
        else:
            fc_edges.append(Part.LineSegment(v0, v1).toShape())
    return Part.Wire(fc_edges)

doc = App.newDocument(BASE_NAME)
profile_edges = {edges_literal}
profile_points = {points_literal}
try:
    if profile_edges and any(edge.get("kind") == "ARC" for edge in profile_edges):
        snap_tol = max({width_x!r}, {depth_y!r}, {height_z!r}, 1.0) * 1e-5
        profile_edges = _snap_profile_edges(profile_edges, snap_tol)
        wire = _wire_from_edges(profile_edges)
    else:
        raise ValueError("无可用圆弧边")
    if not wire.isClosed():
        raise ValueError("线弧轮廓未闭合")
except Exception:
    vectors = [App.Vector(float(x), float(y), 0.0) for x, y in profile_points]
    if vectors[0].distanceToPoint(vectors[-1]) > 1e-9:
        vectors.append(vectors[0])
    wire = Part.makePolygon(vectors)
face = Part.Face(wire)
body = face.extrude(App.Vector({ex!r}, {ey!r}, {ez!r}))
{hole_lines}result = doc.addObject("Part::Feature", "Result")
result.Shape = final_shape
doc.recompute()
doc.saveAs(FCSTD_PATH)
'''


def _discretize_outline_edges(edges: List[Dict[str, Any]]) -> List[List[float]]:
    points: List[List[float]] = []
    for edge in edges:
        p0 = edge.get("p0") or []
        p1 = edge.get("p1") or []
        if len(p0) != 2 or len(p1) != 2:
            continue
        if not points:
            points.append([float(p0[0]), float(p0[1])])
        if edge.get("kind") == "ARC" and len(edge.get("center") or []) == 2 and float(edge.get("radius") or 0.0) > 0.0:
            center = edge.get("center") or []
            radius = float(edge.get("radius") or 0.0)
            a0 = math.atan2(float(p0[1]) - float(center[1]), float(p0[0]) - float(center[0]))
            a1 = math.atan2(float(p1[1]) - float(center[1]), float(p1[0]) - float(center[0]))
            if edge.get("clockwise"):
                span = (a0 - a1) % (2.0 * math.pi)
                if span > math.pi:
                    span = 2.0 * math.pi - span
                    clockwise = False
                else:
                    clockwise = True
            else:
                span = (a1 - a0) % (2.0 * math.pi)
                if span > math.pi:
                    span = 2.0 * math.pi - span
                    clockwise = True
                else:
                    clockwise = False
            steps = max(4, int(math.ceil(span / (math.pi / 36.0))))
            for i in range(1, steps):
                t = i / float(steps)
                angle = a0 - span * t if clockwise else a0 + span * t
                points.append([
                    float(center[0]) + radius * math.cos(angle),
                    float(center[1]) + radius * math.sin(angle),
                ])
        points.append([float(p1[0]), float(p1[1])])
    return points


def _outline_points_from_model_hint(context: Dict[str, Any]) -> List[List[float]]:
    for hint in context.get("model_understanding_hints") or []:
        if hint.get("kind") not in {"toothed_disk_from_open_top_profile", "toothed_disk_from_top_profile"}:
            continue
        construction = hint.get("construction") or {}
        points = construction.get("outer_profile_points_2d") or []
        if len(points) >= 4:
            return [[float(p[0]), float(p[1])] for p in points if len(p or []) == 2]
    return []


def _script_cache_key(llm: Any, prompt: Prompt, context: Dict[str, Any], base_name: str) -> str:
    payload = {
        "model": getattr(llm, "model", ""),
        "base_name": base_name,
        "prompt_system": prompt.system,
        "prompt_user_template": prompt.user_template,
        "context": context,
    }
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _read_cached_script(cache_key: str, base_name: str, fcstd_path: str) -> Optional[str]:
    path = os.path.join(_SCRIPT_CACHE_DIR, f"{cache_key}.py")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            script = fh.read()
    except Exception:
        return None
    return _retarget_script_constants(script, base_name, fcstd_path)


def _write_cached_script(cache_key: str, script: str) -> None:
    try:
        os.makedirs(_SCRIPT_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_SCRIPT_CACHE_DIR, f"{cache_key}.py"), "w", encoding="utf-8") as fh:
            fh.write(script)
            if script and not script.endswith("\n"):
                fh.write("\n")
    except Exception:
        pass


def _retarget_script_constants(script: str, base_name: str, fcstd_path: str) -> str:
    script = re.sub(r'(?m)^\s*BASE_NAME\s*=\s*(["\']).*?\1', f'BASE_NAME = {base_name!r}', script, count=1)
    script = re.sub(r'(?m)^\s*FCSTD_PATH\s*=\s*(["\']).*?\1', f'FCSTD_PATH = {fcstd_path!r}', script, count=1)
    return script


def _repair_generated_script(
    llm: Any,
    original_messages: List[Dict[str, str]],
    bad_content: str,
    reason: str,
    debug_dir: Optional[str],
) -> Tuple[Optional[str], str]:
    repair_messages = list(original_messages)
    repair_messages.append({"role": "assistant", "content": bad_content[:6000]})
    repair_messages.append({
        "role": "user",
        "content": (
            "上一次输出没有通过校验：" + reason + "。\n"
            "请重新输出一份完整 Python 脚本，不要解释。硬性要求：\n"
            "1. 必须包含 `import FreeCAD as App` 和 `import Part`。\n"
            "2. 必须创建对象 `result = doc.addObject('Part::Feature', 'Result')`。\n"
            "3. 必须给 `result.Shape` 赋最终 solid。\n"
            "4. 脚本末尾必须包含 `doc.recompute()` 和 `doc.saveAs(FCSTD_PATH)`。\n"
            "4a. 必须定义 `DIMENSIONS_USED = {...}`，记录关键尺寸来自上下文 JSON 的数值。\n"
            "5. 不要使用不存在的 `Part.Extrude`；拉伸必须使用 `Part.Face(...).extrude(App.Vector(...))`。\n"
            "6. `Part.makeCylinder` 正确签名是 `Part.makeCylinder(radius, height, base, direction)` 或带第 5 个 angle；第 3 个参数必须是 App.Vector base，第 4 个参数必须是方向向量。\n"
            "7. 直接给短脚本，不要写工程图推理过程，不要写长段注释，避免输出被截断。\n"
            "8. 不要输出 Markdown 解释文字。\n"
            "9. 不要保留错误代码和修正代码的两个版本；只输出最终正确版本。\n"
            "10. 如需保留少量注释，注释必须使用中文。\n"
            "11. 构造 R-Z 圆弧包络时，每个 template 条目只生成一条边；不要把 arc 的 mid 点同时作为折线点，否则 Wire 会自交。\n"
            "12. R-Z 点必须映射为 App.Vector(center_x + r, center_y, z)，包络必须用 common 裁剪主体，禁止 fuse 倒角包络。"
        ),
    })
    try:
        with _alarm_timeout(_REQUEST_TIMEOUT_SECONDS):
            content = llm.complete_text(
                repair_messages,
                max_tokens=_MAX_SCRIPT_TOKENS,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
    except Exception as exc:
        return None, f"自动重试失败：{exc}"

    _write_debug_text(debug_dir, "llm_raw_response_retry.txt", content)
    script = _sanitize_generated_script(strip_code_fence(content))
    ok, repair_reason = validate_generated_script(script)
    if not ok:
        return None, f"自动重试仍未通过：{repair_reason}"
    return script, f"LLM 直接建模脚本生成完成（{llm.model}，自动修复一次）"


def _write_debug_text(debug_dir: Optional[str], filename: str, text: str) -> None:
    if not debug_dir:
        return
    try:
        with open(os.path.join(debug_dir, filename), "w", encoding="utf-8") as fh:
            fh.write(text)
            if text and not text.endswith("\n"):
                fh.write("\n")
    except Exception:
        pass


def strip_code_fence(text: str) -> str:
    text = text.strip()
    fenced = re.search(r"```(?:python)?\s*(.*?)\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        text = re.sub(r"^```(?:python)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        first_import = re.search(r"(?m)^(?:import FreeCAD as App|import Part|from FreeCAD import)", text)
        if first_import:
            text = text[first_import.start():]
    return text.strip() + "\n"


def _sanitize_generated_script(script: str) -> str:
    script = re.sub(r"(?m)^\s*Part\.setMeasurePrecision\([^\n]*\)\s*\n?", "", script)
    script = re.sub(r"(?m)^\s*App\.setActiveDocument\([^\n]*\)\s*\n?", "", script)
    script = re.sub(r"(?m)^\s*doc\.close\(\)\s*\n?", "", script)
    script = re.sub(r"(?m)^\s*doc\.Name\s*=\s*[^\n]*\n?", "", script)
    script = re.sub(r"(?m)^\s*[A-Za-z_][A-Za-z0-9_]*\.Label\s*=\s*[^\n]*\n?", "", script)
    script = re.sub(
        r"Part\.makePolygon\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        r"Part.makePolygon(\1 + [\1[0]])",
        script,
    )
    wire_vars = re.findall(
        r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*Part\.makePolygon\(",
        script,
    )
    for var_name in wire_vars:
        script = re.sub(
            rf"\b{re.escape(var_name)}\.extrude\(",
            f"Part.Face({var_name}).extrude(",
            script,
        )
    shape_vars = re.findall(
        r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*Part\.make(?:Sphere|Box|Cylinder|Cone|Torus)\(",
        script,
    )
    for var_name in shape_vars:
        script = re.sub(
            rf"(?ms)^([ \t]*)if\s+{re.escape(var_name)}\.Shape\s+and\s+not\s+{re.escape(var_name)}\.Shape\.isEmpty:\s*\n"
            rf"\1[ \t]+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*{re.escape(var_name)}\.Shape\s*\n"
            rf"\1else:\s*\n\1[ \t]+(?:#.*\n\1[ \t]+)?\2\s*=\s*Part\.Solid\({re.escape(var_name)}\)\s*\n",
            rf"\1\2 = {var_name}\n",
            script,
        )
        script = re.sub(rf"\b{re.escape(var_name)}\.Shape\b", var_name, script)
    script = re.sub(r"\.(X|Y|Z)\b", lambda m: "." + m.group(1).lower(), script)
    script = re.sub(
        r"Part\.makePrism\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([^\n]+?)\)",
        r"Part.Face(\1).extrude(\2)",
        script,
    )
    script = re.sub(
        r"Part\.Fuse\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)",
        r"\1.fuse(\2)",
        script,
    )
    if "Part.Arc(" in script or "Part.makeArc(" in script or "Part.Line(" in script or ".Edge" in script:
        script = script.replace("Part.Arc(", "_safe_arc(")
        script = script.replace("Part.makeArc(", "_safe_arc(")
        script = script.replace("Part.Line(", "_safe_line(")
        script = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)\.Edge\b", r"_edge(\1)", script)
        script = _ensure_geometry_helper(script)
    script = re.sub(r"(_safe_arc\([^\n]*?\))\.toShape\(\)", r"\1", script)
    script = re.sub(r"(_safe_line\([^\n]*?\))\.toShape\(\)", r"\1", script)
    safe_edge_vars = re.findall(
        r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*_safe_(?:arc|line)\(",
        script,
    )
    for var_name in safe_edge_vars:
        script = re.sub(rf"\b{re.escape(var_name)}\.toShape\(\)", var_name, script)
    script = re.sub(r"Part\.Wire\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", r"Part.Wire(_valid_edges(\1))", script)
    script = re.sub(r"Part\.Wire\(\s*\[([^\]\n]+)\]\s*\)", r"Part.Wire(_valid_edges([\1]))", script)
    if "_valid_edges(" in script and "def _valid_edges(" not in script:
        script = _ensure_geometry_helper(script)
    circle_vars = re.findall(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*Part\.makeCircle\(", script)
    for var_name in circle_vars:
        script = re.sub(
            rf"Part\.Face\(\s*{re.escape(var_name)}\s*\)",
            f"Part.Face(Part.Wire([{var_name}]))",
            script,
        )
    if "Part.fuse(" in script:
        script = script.replace("Part.fuse(", "_fuse_all(")
        script = _ensure_fuse_helper(script)
    script = _strip_full_line_comments(script)
    return script.strip() + "\n"


def _strip_full_line_comments(script: str) -> str:
    lines = []
    blank_count = 0
    for line in script.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not stripped:
            blank_count += 1
            if blank_count > 1:
                continue
        else:
            blank_count = 0
        lines.append(line.rstrip())
    return "\n".join(lines)


def _ensure_geometry_helper(script: str) -> str:
    if ("def _safe_arc(" in script and "def _safe_line(" in script and
            "def _edge(" in script and "def _valid_edges(" in script):
        return script
    helper = (
        "\n\ndef _edge(curve):\n"
        "    if hasattr(curve, 'toShape'):\n"
        "        return curve.toShape()\n"
        "    return curve\n"
        "\n"
        "def _safe_arc(p1, p2, p3):\n"
        "    if p1.distanceToPoint(p3) < 1e-9:\n"
        "        return None\n"
        "    try:\n"
        "        return Part.Arc(p1, p2, p3).toShape()\n"
        "    except Exception:\n"
        "        if p1.distanceToPoint(p3) < 1e-9:\n"
        "            return None\n"
        "        return Part.LineSegment(p1, p3).toShape()\n"
        "\n"
        "def _safe_line(p1, p2):\n"
        "    if p1.distanceToPoint(p2) < 1e-9:\n"
        "        return None\n"
        "    return Part.LineSegment(p1, p2).toShape()\n"
        "\n"
        "def _valid_edges(edges):\n"
        "    return [_edge(edge) for edge in edges if edge is not None]\n"
    )
    insert_after = re.search(r"(?m)^(?:import .+\n)+", script)
    if insert_after:
        return script[:insert_after.end()] + helper + script[insert_after.end():]
    return helper.lstrip() + "\n" + script


def _ensure_fuse_helper(script: str) -> str:
    if "def _fuse_all(" in script:
        return script
    helper = (
        "\n\ndef _fuse_all(shapes):\n"
        "    shapes = [shape for shape in shapes if shape is not None]\n"
        "    if not shapes:\n"
        "        raise ValueError('no shapes to fuse')\n"
        "    result = shapes[0]\n"
        "    for shape in shapes[1:]:\n"
        "        result = result.fuse(shape)\n"
        "    return result\n"
    )
    insert_after = re.search(r"(?m)^(?:import .+\n)+", script)
    if insert_after:
        return script[:insert_after.end()] + helper + script[insert_after.end():]
    return helper.lstrip() + "\n" + script


def _view_extent(view: Dict[str, Any], key: str, default: float) -> float:
    try:
        value = float(view.get(key, default))
        return value if value > 0 else default
    except Exception:
        return default


class _alarm_timeout:
    def __init__(self, seconds: int):
        self.seconds = int(seconds)
        self.previous_handler = None

    def __enter__(self) -> None:
        if self.seconds <= 0:
            return
        self.previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, self._handle_timeout)
        signal.alarm(self.seconds)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.seconds <= 0:
            return
        signal.alarm(0)
        if self.previous_handler is not None:
            signal.signal(signal.SIGALRM, self.previous_handler)

    def _handle_timeout(self, _signum: int, _frame: Any) -> None:
        raise TimeoutError(f"LLM request exceeded {self.seconds} seconds")


def validate_generated_script(script: str) -> Tuple[bool, str]:
    if not script.strip():
        return False, "empty script"
    for token in _BANNED_TEXT:
        if token in script:
            return False, f"contains banned token {token!r}"
    required = ("FreeCAD", "Part", "Result", "saveAs", "DIMENSIONS_USED")
    missing = [token for token in required if token not in script]
    if missing:
        return False, f"missing required tokens: {missing}"
    if _has_flattened_arc_profile(script):
        return False, "R-Z arc profile mixes arc edges with polyline segments through arc midpoints; build one edge per rz_profile_template segment"
    if _has_chamfer_envelope_union(script):
        return False, "arc-revolve chamfer envelope must clip the hex body with common(), not fuse() with the body"
    if _has_untranslated_rz_profile(script):
        return False, "R-Z profile points must be translated to App.Vector(center_x + r, center_y, z) before revolve"
    if _regular_hex_prism_script_has_extra_features(script):
        return False, "regular hex prism scripts must not add holes, fillets, chamfers, or revolved envelopes without explicit circle/arc hints"
    if _has_naive_arc_midpoint(script):
        return False, "ARC midpoint angle must handle start/end crossing 360 degrees; if end_angle < start_angle, add 2*pi before averaging"
    ok_arc, arc_reason = _validate_literal_arc_midpoints(script)
    if not ok_arc:
        return False, arc_reason
    if _has_fixed_extra_hole_height(script):
        return False, "hole cutter height must come from hole_hints.height or use height_z + 2*margin with matching base -margin; do not use height + 0.2 with base -0.1"
    if "_edge(Part)" in script:
        return False, "do not call _edge(Part) or Part.Vertex to build wires; create line/arc edges or use Part.makePolygon(points)"
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return False, f"syntax error: {exc}"
    numeric_names = _numeric_assignments(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root not in _ALLOWED_IMPORT_ROOTS:
                    return False, f"import {alias.name!r} is not allowed"
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".", 1)[0]
            if root not in _ALLOWED_IMPORT_ROOTS:
                return False, f"from import {node.module!r} is not allowed"
        elif isinstance(node, ast.Call):
            func_name = _call_name(node.func)
            if func_name in {"eval", "exec", "compile", "open", "__import__"}:
                return False, f"call {func_name!r} is not allowed"
            if func_name in _INVALID_FREECAD_CALLS:
                return False, _INVALID_FREECAD_CALLS[func_name]
            if func_name.endswith(".rotate"):
                return False, "FreeCAD App.Vector.rotate is not available here; compute rotated coordinates with math.cos/math.sin instead"
            if _call_name(node.func).endswith(".extrude"):
                owner = node.func.value if isinstance(node.func, ast.Attribute) else None
                if isinstance(owner, ast.Name) and owner.id.lower().endswith(("wire", "polyline", "polygon")):
                    return False, "wire extrusion creates a shell; create Part.Face(wire).extrude(...) to produce a solid"
            if func_name == "Part.makeCylinder" and len(node.args) >= 4 and _is_number_literal(node.args[3]):
                return False, "Part.makeCylinder fourth argument must be a direction App.Vector, not a number; use Part.makeCylinder(radius, height, base, App.Vector(...))"
            if func_name == "Part.makeCylinder" and len(node.args) >= 3 and _is_numeric_expr(node.args[2], numeric_names):
                return False, "Part.makeCylinder third argument must be a base App.Vector, not a number; use App.Vector(cx, cy, z) or App.Vector(x, y, z)"
    return True, "ok"


def _numeric_assignments(tree: ast.AST) -> set:
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not _is_number_literal(node.value):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return names


def _is_numeric_expr(node: ast.AST, numeric_names: set) -> bool:
    if _is_number_literal(node):
        return True
    return isinstance(node, ast.Name) and node.id in numeric_names


def _has_flattened_arc_profile(script: str) -> bool:
    return (
        "rz_points" in script and
        "revolve_points" in script and
        "_safe_arc(" in script and
        "range(len(revolve_points) - 1)" in script
    )


def _has_chamfer_envelope_union(script: str) -> bool:
    envelope_names = ("chamfer_solid", "solid_env", "envelope")
    return any(f".fuse({name}" in script for name in envelope_names)


def _has_untranslated_rz_profile(script: str) -> bool:
    if "revolve(App.Vector(" not in script or "_safe_arc(App.Vector(" not in script:
        return False
    if any(token in script for token in ("center_x +", "center[0] +", "+ r,")):
        return False
    return bool(re.search(r"_safe_arc\(\s*App\.Vector\(\s*(?:8\.66|9\.33|10\.0)\s*,\s*0\.0", script))


def _regular_hex_prism_script_has_extra_features(script: str) -> bool:
    understanding_match = re.search(r'(?m)^\s*MODEL_UNDERSTANDING\s*=\s*(["\'])(.*?)\1', script)
    if not understanding_match:
        return False
    understanding = understanding_match.group(2)
    if "六棱柱" not in understanding and "六角柱" not in understanding:
        return False
    if "圆柱" in understanding or "组合" in understanding:
        return False
    banned = ("makeFillet", "makeChamfer", ".revolve(", "Part.makeCylinder", ".cut(")
    return any(token in script for token in banned)


def _has_naive_arc_midpoint(script: str) -> bool:
    return (
        "Part.Arc" in script and
        "start_a" in script and
        "end_a" in script and
        "mid_a = (start_a + end_a) / 2" in script and
        "end_a < start_a" not in script and
        "end_a += 2" not in script
    )


def _has_fixed_extra_hole_height(script: str) -> bool:
    return bool(re.search(
        r"Part\.makeCylinder\([^\n]*height\s*\+\s*0\.2[^\n]*App\.Vector\([^\n]*-0\.1",
        script,
    ))


def _validate_literal_arc_midpoints(script: str) -> Tuple[bool, str]:
    """Validate common literal edge dictionaries used by LLM scripts.

    For the project outline format, ARC p0/p1 are ordered around the loop.  A
    valid Part.Arc(p0, pmid, p1) midpoint must lie on the minor CCW interval
    from p0 to p1, or on the corresponding interval after swapping p0/p1.
    Qwen often computes a CCW angle from start/end but keeps p0/p1 in the
    opposite order; FreeCAD then creates the complementary large arc, expanding
    the bbox while static API checks still pass.
    """
    if "'kind': 'ARC'" not in script and '"kind": "ARC"' not in script:
        return True, "ok"
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return True, "ok"
    arc_items: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        try:
            value = ast.literal_eval(node)
        except Exception:
            continue
        if not isinstance(value, dict) or value.get("kind") != "ARC":
            continue
        if all(key in value for key in ("p0", "p1", "center", "radius")):
            arc_items.append(value)
    for idx, item in enumerate(arc_items):
        p0 = item.get("p0") or []
        p1 = item.get("p1") or []
        center = item.get("center") or []
        if len(p0) != 2 or len(p1) != 2 or len(center) != 2:
            continue
        radius = float(item.get("radius") or 0.0)
        if radius <= 0.0:
            continue
        start = item.get("start_angle", item.get("start"))
        end = item.get("end_angle", item.get("end"))
        if start is None or end is None:
            continue
        start_a = math.radians(float(start))
        end_a = math.radians(float(end))
        if end_a < start_a:
            end_a += 2.0 * math.pi
        mid_a = (start_a + end_a) * 0.5
        pmid = [
            float(center[0]) + radius * math.cos(mid_a),
            float(center[1]) + radius * math.sin(mid_a),
        ]
        clockwise = bool(item.get("clockwise"))
        if clockwise:
            valid = _point_on_minor_arc(p1, pmid, p0, center)
        else:
            valid = _point_on_minor_arc(p0, pmid, p1, center)
        if valid:
            continue
        return False, (
            f"ARC literal #{idx} midpoint does not match the edge direction; "
            "respect clockwise=true by constructing the clockwise small arc, otherwise construct the counter-clockwise small arc"
        )
    return True, "ok"


def _point_on_minor_arc(p0: List[float], pmid: List[float], p1: List[float], center: List[float]) -> bool:
    a0 = _angle_from_center(p0, center)
    am = _angle_from_center(pmid, center)
    a1 = _angle_from_center(p1, center)
    span = (a1 - a0) % (2.0 * math.pi)
    if span <= 1e-9 or span > math.pi + 1e-6:
        return False
    mid_span = (am - a0) % (2.0 * math.pi)
    return -1e-6 <= mid_span <= span + 1e-6


def _angle_from_center(point: List[float], center: List[float]) -> float:
    return math.atan2(float(point[1]) - float(center[1]), float(point[0]) - float(center[0])) % (2.0 * math.pi)


def _is_number_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float))


def _bundle_summary(bundle: ViewBundle) -> Dict[str, Any]:
    modeling_entities = _modeling_entities(bundle.entities)
    modeling_bundle = ViewBundle(
        name=bundle.name,
        bbox=bundle.bbox,
        entities=modeling_entities,
        annotations=bundle.annotations,
    )
    outlines, circles = extract_closed_outlines_and_circles(
        modeling_bundle, hidden_pred=_is_hidden_entity)
    return {
        "name": bundle.name,
        "bbox": _round_list(bundle.bbox),
        "width": _round_num(bundle.width),
        "height": _round_num(bundle.height),
        "entity_count": len(bundle.entities),
        "modeling_entity_count": len(modeling_entities),
        "excluded_auxiliary_entity_count": len(bundle.entities) - len(modeling_entities),
        "annotations": [_annotation_summary(ann) for ann in bundle.annotations],
        "visible_closed_outline_bboxes": [_outline_bbox_summary(o) for o in outlines[:_MAX_OUTLINES_PER_VIEW]],
        "approximated_curves": _dedupe_curve_summaries(
            _approximated_curve_summaries(outlines) + _approximated_line_circle_summaries(modeling_entities)
        ),
        "visible_circles": [_circle_summary(c) for c in circles],
    }


def _projected_view_summary(name: str, pv: Any) -> Dict[str, Any]:
    modeling_entities = _modeling_entities(pv.entities)
    temp_bundle = ViewBundle(
        name=name,
        bbox=(0.0, 0.0, float(pv.width), float(pv.height)),
        entities=modeling_entities,
    )
    outlines, circles = extract_closed_outlines_and_circles(temp_bundle, hidden_pred=_is_hidden_entity)
    visible = [e for e in modeling_entities if not _is_hidden_entity(e)]
    hidden = [e for e in modeling_entities if _is_hidden_entity(e)]
    ordered_profile_edges = _ordered_profile_edges_from_outlines(outlines)
    if not ordered_profile_edges:
        ordered_profile_edges = _ordered_profile_edges_from_endpoint_graph(visible, float(pv.width), float(pv.height))
    return {
        "name": name,
        "plane": pv.plane,
        "point_to_world": _point_to_world_hint(pv.plane, float(pv.width)),
        "origin_2d": _round_list(pv.origin_2d),
        "width": _round_num(pv.width),
        "height": _round_num(pv.height),
        "entity_count": len(pv.entities),
        "modeling_entity_count": len(modeling_entities),
        "excluded_auxiliary_entity_count": len(pv.entities) - len(modeling_entities),
        "visible_closed_outlines": [_outline_summary(o) for o in outlines[:_MAX_OUTLINES_PER_VIEW]],
        "ordered_profile_edges": ordered_profile_edges,
        "regular_polygon_hints": _regular_polygon_hints(outlines),
        "extrusion_profile_hints": _extrusion_profile_hints(outlines, pv.plane, float(pv.width)),
        "approximated_curves": _dedupe_curve_summaries(
            _approximated_curve_summaries(outlines) + _approximated_line_circle_summaries(modeling_entities)
        ),
        "visible_circles": [_circle_summary(c) for c in circles],
        "key_profile_entities": _key_profile_entity_summaries(visible, 96),
        "key_visible_entities": _key_entity_summaries(visible, _MAX_VISIBLE_ENTITIES_PER_VIEW),
        "key_hidden_entities": _key_entity_summaries(hidden, _MAX_HIDDEN_ENTITIES_PER_VIEW),
        "hidden_line_groups": _hidden_line_groups(hidden),
    }


def _ordered_profile_edges_from_outlines(outlines: List[Any]) -> List[Dict[str, Any]]:
    if not outlines:
        return []
    raw_edges = outlines[0].to_dict().get("edges", [])
    return _round_json([_profile_edge_summary(edge) for edge in raw_edges])


def _ordered_profile_edges_from_endpoint_graph(entities: List[DxfEntity], width: float, height: float) -> List[Dict[str, Any]]:
    """Trace the largest closed LINE/ARC profile from endpoint connectivity.

    This is view-agnostic and works for TOP/FRONT/LEFT projected 2D geometry.
    It keeps ARC entities as true ordered edges instead of falling back to a
    radial angle sort that only works for gear-like TOP views.
    """
    edges: List[Dict[str, Any]] = []
    for entity in entities:
        if entity.kind == "ARC" and entity.center is not None and entity.radius is not None:
            edge = _arc_profile_edge(entity)
            if edge is not None:
                edges.append(edge)
        elif entity.kind == "LINE" and len(entity.points) >= 2:
            edge = _line_profile_edge(entity.points[0], entity.points[1])
            if edge is not None:
                edges.append(edge)
        elif entity.kind in {"LWPOLYLINE", "POLYLINE"} and len(entity.points) >= 2:
            limit = len(entity.points) if entity.extra.get("closed") else len(entity.points) - 1
            for idx in range(limit):
                edge = _line_profile_edge(entity.points[idx], entity.points[(idx + 1) % len(entity.points)])
                if edge is not None:
                    edges.append(edge)
    if not edges or not any(edge.get("kind") == "ARC" for edge in edges):
        return []
    scale = max(width, height, 1.0)
    tol = max(scale * 1e-5, 1e-4)
    loops = _trace_closed_edge_loops(edges, tol)
    if not loops:
        return []
    loop = max(loops, key=_profile_loop_area)
    if len(loop) < 3 or not any(edge.get("kind") == "ARC" for edge in loop):
        return []
    return _round_json([_profile_edge_summary(edge) for edge in loop])


def _trace_closed_edge_loops(edges: List[Dict[str, Any]], tol: float) -> List[List[Dict[str, Any]]]:
    remaining = _prune_profile_dangling_edges(edges, tol)
    loops: List[List[Dict[str, Any]]] = []
    used: set[int] = set()
    for idx, edge in enumerate(remaining):
        if idx in used:
            continue
        loop, loop_indices = _trace_one_closed_edge_loop(remaining, idx, tol)
        if loop and len(loop) >= 3:
            loops.append(loop)
            used.update(loop_indices)
    return loops


def _trace_one_closed_edge_loop(edges: List[Dict[str, Any]], start_idx: int, tol: float) -> Tuple[List[Dict[str, Any]], set[int]]:
    first = edges[start_idx]
    loop = [first]
    used = {start_idx}
    start_pt = first.get("p0") or []
    end_pt = first.get("p1") or []
    while len(start_pt) == 2 and len(end_pt) == 2 and not _profile_points_close(end_pt, start_pt, tol):
        candidates = []
        for idx, edge in enumerate(edges):
            if idx in used:
                continue
            p0 = edge.get("p0") or []
            p1 = edge.get("p1") or []
            if _profile_points_close(p0, end_pt, tol):
                candidates.append((0.0, idx, edge))
            elif _profile_points_close(p1, end_pt, tol):
                candidates.append((0.0, idx, _reverse_profile_edge(edge)))
        if not candidates:
            return [], set()
        _score, next_idx, next_edge = min(candidates, key=lambda item: item[0])
        loop.append(next_edge)
        used.add(next_idx)
        end_pt = next_edge.get("p1") or []
        if len(used) > len(edges):
            return [], set()
    if len(start_pt) == 2 and len(end_pt) == 2 and _profile_points_close(end_pt, start_pt, tol):
        return loop, used
    return [], set()


def _prune_profile_dangling_edges(edges: List[Dict[str, Any]], tol: float) -> List[Dict[str, Any]]:
    remaining = list(edges)
    changed = True
    while changed:
        degree: Dict[Tuple[int, int], int] = {}
        for edge in remaining:
            for key in ("p0", "p1"):
                point = edge.get(key) or []
                if len(point) != 2:
                    continue
                node = _profile_point_key(point, tol)
                degree[node] = degree.get(node, 0) + 1
        kept = []
        for edge in remaining:
            p0 = edge.get("p0") or []
            p1 = edge.get("p1") or []
            if len(p0) != 2 or len(p1) != 2:
                continue
            if degree.get(_profile_point_key(p0, tol), 0) >= 2 and degree.get(_profile_point_key(p1, tol), 0) >= 2:
                kept.append(edge)
        changed = len(kept) != len(remaining)
        remaining = kept
    return remaining


def _reverse_profile_edge(edge: Dict[str, Any]) -> Dict[str, Any]:
    if edge.get("kind") == "ARC":
        item = {
            "kind": "ARC",
            "center": edge.get("center"),
            "radius": edge.get("radius"),
            "start_angle": edge.get("end_angle"),
            "end_angle": edge.get("start_angle"),
            "clockwise": not bool(edge.get("clockwise")),
            "p0": edge.get("p1"),
            "p1": edge.get("p0"),
        }
        if not item["clockwise"]:
            item.pop("clockwise", None)
        return item
    return {"kind": "LINE", "p0": edge.get("p1"), "p1": edge.get("p0")}


def _profile_points_close(a: Any, b: Any, tol: float) -> bool:
    return len(a or []) == 2 and len(b or []) == 2 and abs(float(a[0]) - float(b[0])) <= tol and abs(float(a[1]) - float(b[1])) <= tol


def _profile_point_key(point: Any, tol: float) -> Tuple[int, int]:
    return (round(float(point[0]) / tol), round(float(point[1]) / tol))


def _profile_loop_area(loop: List[Dict[str, Any]]) -> float:
    points = [edge.get("p0") for edge in loop if len(edge.get("p0") or []) == 2]
    if len(points) < 3:
        return 0.0
    area = 0.0
    for idx, point in enumerate(points):
        nxt = points[(idx + 1) % len(points)]
        area += float(point[0]) * float(nxt[1]) - float(nxt[0]) * float(point[1])
    return abs(area) * 0.5


def _arc_profile_edge(entity: DxfEntity) -> Optional[Dict[str, Any]]:
    if entity.center is None or entity.radius is None:
        return None
    cx, cy = float(entity.center[0]), float(entity.center[1])
    radius = float(entity.radius)
    if radius <= 0.0:
        return None
    start = float(entity.start_angle or 0.0)
    end = float(entity.end_angle or 0.0)
    start_rad = math.radians(start)
    end_rad = math.radians(end)
    return {
        "kind": "ARC",
        "center": [cx, cy],
        "radius": radius,
        "start_angle": start,
        "end_angle": end,
        "p0": [cx + radius * math.cos(start_rad), cy + radius * math.sin(start_rad)],
        "p1": [cx + radius * math.cos(end_rad), cy + radius * math.sin(end_rad)],
    }


def _line_profile_edge(p0: Any, p1: Any) -> Optional[Dict[str, Any]]:
    if len(p0 or []) != 2 or len(p1 or []) != 2:
        return None
    x0, y0 = float(p0[0]), float(p0[1])
    x1, y1 = float(p1[0]), float(p1[1])
    if math.hypot(x1 - x0, y1 - y0) <= 1e-9:
        return None
    return {"kind": "LINE", "p0": [x0, y0], "p1": [x1, y1]}


def _profile_edge_summary(edge: Dict[str, Any]) -> Dict[str, Any]:
    item = {
        "kind": edge.get("kind"),
        "p0": edge.get("p0"),
        "p1": edge.get("p1"),
    }
    if edge.get("kind") == "ARC":
        item["center"] = edge.get("center")
        item["radius"] = edge.get("radius")
        item["start_angle"] = edge.get("start_angle")
        item["end_angle"] = edge.get("end_angle")
        if edge.get("clockwise"):
            item["clockwise"] = True
    return item


def _key_profile_entity_summaries(entities: List[DxfEntity], limit: int) -> List[Dict[str, Any]]:
    profile_kinds = {"LINE", "ARC", "LWPOLYLINE", "POLYLINE"}
    selected = [entity for entity in entities if entity.kind in profile_kinds]
    ranked = sorted(selected, key=_entity_angle_rank)
    return [_entity_summary(entity, idx) for idx, entity in enumerate(ranked[:limit])]


def _entity_angle_rank(entity: DxfEntity) -> float:
    b = entity.bbox()
    if not b:
        return 0.0
    cx = (b[0] + b[2]) * 0.5
    cy = (b[1] + b[3]) * 0.5
    return math.atan2(cy, cx)


def _hidden_line_groups(entities: List[DxfEntity]) -> Dict[str, List[Dict[str, Any]]]:
    horizontal: Dict[float, List[Tuple[float, float]]] = {}
    vertical: Dict[float, List[Tuple[float, float]]] = {}
    for entity in entities:
        if entity.kind != "LINE" or len(entity.points) < 2:
            continue
        p0, p1 = entity.points[0], entity.points[1]
        x0, y0 = float(p0[0]), float(p0[1])
        x1, y1 = float(p1[0]), float(p1[1])
        if abs(y0 - y1) <= 1e-6:
            y = _round_num((y0 + y1) * 0.5)
            horizontal.setdefault(y, []).append((min(x0, x1), max(x0, x1)))
        elif abs(x0 - x1) <= 1e-6:
            x = _round_num((x0 + x1) * 0.5)
            vertical.setdefault(x, []).append((min(y0, y1), max(y0, y1)))
    return {
        "horizontal": _line_group_summary(horizontal),
        "vertical": _line_group_summary(vertical),
    }


def _line_group_summary(groups: Dict[float, List[Tuple[float, float]]]) -> List[Dict[str, Any]]:
    result = []
    for coord in sorted(groups):
        spans = groups[coord]
        if not spans:
            continue
        merged = _merge_spans(spans)
        result.append({
            "coord": _round_num(coord),
            "spans": [[_round_num(a), _round_num(b)] for a, b in merged],
            "max_span": _round_num(max((b - a) for a, b in merged)),
            "count": len(spans),
        })
    return result


def _merge_spans(spans: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    if not spans:
        return []
    spans = sorted(spans)
    merged = [spans[0]]
    for start, end in spans[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1e-6:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _point_to_world_hint(plane: str, width: float) -> str:
    if plane == "XY":
        return "2D point [u,v] maps to App.Vector(u, v, z); extrude along Z for thickness/height"
    if plane == "XZ":
        return "2D point [u,v] maps to App.Vector(u, y, v); extrude along Y for depth"
    if plane == "YZ":
        return f"2D point [u,v] maps to App.Vector(x, {width:.6f} - u, v); extrude along X for width"
    return "2D point [u,v] must be mapped according to the view plane before extrusion"


def _key_entity_summaries(entities: List[DxfEntity], limit: int) -> List[Dict[str, Any]]:
    if limit <= 0:
        return []
    ranked = sorted(enumerate(entities), key=lambda item: _entity_score(item[1]), reverse=True)
    return [_entity_summary(entity, idx) for idx, entity in ranked[:limit]]


def _entity_score(entity: DxfEntity) -> float:
    b = entity.bbox()
    if not b:
        return 0.0
    return ((b[2] - b[0]) ** 2 + (b[3] - b[1]) ** 2) ** 0.5


def _entity_summary(entity: DxfEntity, idx: int) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "id": idx,
        "kind": entity.kind,
        "layer": entity.layer,
        "linetype": entity.linetype,
        "linetype_desc": entity.extra.get("linetype_desc"),
        "hidden": _is_hidden_entity(entity),
        "bbox": _round_list(entity.bbox()) if entity.bbox() else None,
    }
    if entity.kind == "LINE" and len(entity.points) >= 2:
        item["points"] = [_round_point(p) for p in entity.points[:2]]
    elif entity.kind == "CIRCLE" and entity.center is not None:
        item["center"] = _round_point(entity.center)
        item["radius"] = _round_num(entity.radius or 0.0)
    elif entity.kind == "ARC" and entity.center is not None:
        item["center"] = _round_point(entity.center)
        item["radius"] = _round_num(entity.radius or 0.0)
        item["start_angle"] = _round_num(entity.start_angle or 0.0)
        item["end_angle"] = _round_num(entity.end_angle or 0.0)
    elif entity.kind in {"LWPOLYLINE", "POLYLINE"}:
        item["points"] = [_round_point(p) for p in entity.points[:40]]
        item["closed"] = bool(entity.extra.get("closed"))
    return item


def _annotation_summary(entity: DxfEntity) -> Dict[str, Any]:
    return {
        "kind": entity.kind,
        "bbox": _round_list(entity.bbox()) if entity.bbox() else None,
        "dim_text": entity.dim_text,
        "dim_measurement": entity.dim_measurement,
        "dim_type": entity.dim_type,
        "rotation": entity.extra.get("rotation"),
    }


def _outline_summary(outline: Any) -> Dict[str, Any]:
    raw_edges = outline.to_dict().get("edges", [])
    if len(raw_edges) <= _MAX_FULL_EDGES_PER_OUTLINE:
        edges = raw_edges
    else:
        edges = raw_edges[:_MAX_EDGES_PER_OUTLINE]
    sample_points = _sample_outline_points(outline, 24) if len(outline.edges) > _MAX_EDGES_PER_OUTLINE else []
    profile_points = _sample_outline_points(outline, _MAX_PROFILE_SAMPLE_POINTS) if len(outline.edges) >= 80 else []
    return {
        "bbox": _round_list(outline.bbox),
        "width": _round_num(outline.width),
        "height": _round_num(outline.height),
        "edge_count": len(outline.edges),
        "edges_complete": len(raw_edges) <= _MAX_FULL_EDGES_PER_OUTLINE,
        "edges": _round_json(edges),
        "sample_points_2d": _round_json(sample_points),
        "profile_points_2d": _round_json(profile_points),
    }


def _sample_outline_points(outline: Any, limit: int) -> List[List[float]]:
    raw_edges = outline.to_dict().get("edges", [])
    points = [edge.get("p0") for edge in raw_edges if len(edge.get("p0") or []) == 2]
    if len(points) < 2:
        return []
    if len(points) <= limit:
        return [[float(point[0]), float(point[1])] for point in points]
    step = len(points) / float(limit)
    sampled = []
    used = set()
    for idx in range(limit):
        point_index = min(int(round(idx * step)), len(points) - 1)
        if point_index in used:
            continue
        used.add(point_index)
        point = points[point_index]
        sampled.append([float(point[0]), float(point[1])])
    bbox = outline.bbox
    if len(bbox) == 4:
        sampled = _ensure_profile_points_cover_bbox(sampled, list(bbox), points)
    return sampled


def _outline_bbox_summary(outline: Any) -> Dict[str, Any]:
    return {
        "bbox": _round_list(outline.bbox),
        "width": _round_num(outline.width),
        "height": _round_num(outline.height),
        "edge_count": len(outline.edges),
    }


def _regular_polygon_hints(outlines: List[Any]) -> List[Dict[str, Any]]:
    hints: List[Dict[str, Any]] = []
    for outline in outlines[:_MAX_OUTLINES_PER_VIEW]:
        if len(outline.edges) != 6:
            continue
        min_x, min_y, max_x, max_y = [float(value) for value in outline.bbox]
        width = max_x - min_x
        height = max_y - min_y
        if width <= 0.0 or height <= 0.0:
            continue
        hints.append({
            "kind": "regular_hexagon_bbox",
            "bbox": _round_list(outline.bbox),
            "center": [_round_num((min_x + max_x) * 0.5), _round_num((min_y + max_y) * 0.5)],
            "flat_to_flat": _round_num(height),
            "vertex_to_vertex": _round_num(width),
            "circumradius": _round_num(width * 0.5),
            "inradius": _round_num(height * 0.5),
            "recommended_vertices_2d": _round_json([
                [max_x, (min_y + max_y) * 0.5],
                [max_x - width * 0.25, max_y],
                [min_x + width * 0.25, max_y],
                [min_x, (min_y + max_y) * 0.5],
                [min_x + width * 0.25, min_y],
                [max_x - width * 0.25, min_y],
            ]),
        })
    return hints


def _extrusion_profile_hints(outlines: List[Any], plane: str, width: float) -> List[Dict[str, Any]]:
    hints: List[Dict[str, Any]] = []
    for outline in outlines[:_MAX_OUTLINES_PER_VIEW]:
        if len(outline.edges) < 6:
            continue
        edges = outline.to_dict().get("edges", [])[:_MAX_EDGES_PER_OUTLINE]
        if not edges or not _all_axis_aligned_edges(edges):
            continue
        points_2d = [edge.get("p0") for edge in edges if len(edge.get("p0") or []) == 2]
        if len(points_2d) < 6:
            continue
        points_2d.append(points_2d[0])
        hints.append({
            "kind": "orthogonal_closed_profile",
            "source_plane": plane,
            "profile_points_2d": _round_json(points_2d),
            "profile_points_world": _round_json([
                _profile_point_to_world(plane, float(point[0]), float(point[1]), width)
                for point in points_2d
            ]),
            "extrude_axis": _profile_extrude_axis(plane),
            "extrude_vector_template": _profile_extrude_vector_template(plane),
        })
    return hints


def _all_axis_aligned_edges(edges: List[Dict[str, Any]]) -> bool:
    for edge in edges:
        p0 = edge.get("p0") or []
        p1 = edge.get("p1") or []
        if len(p0) != 2 or len(p1) != 2:
            return False
        if abs(float(p0[0]) - float(p1[0])) > 1e-6 and abs(float(p0[1]) - float(p1[1])) > 1e-6:
            return False
    return True


def _profile_point_to_world(plane: str, u: float, v: float, width: float) -> List[float]:
    if plane == "XY":
        return [u, v, 0.0]
    if plane == "XZ":
        return [u, 0.0, v]
    if plane == "YZ":
        return [0.0, width - u, v]
    return [u, v, 0.0]


def _profile_extrude_axis(plane: str) -> str:
    if plane == "XY":
        return "Z"
    if plane == "XZ":
        return "Y"
    if plane == "YZ":
        return "X"
    return "UNKNOWN"


def _profile_extrude_vector_template(plane: str) -> str:
    if plane == "XY":
        return "App.Vector(0, 0, length_z)"
    if plane == "XZ":
        return "App.Vector(0, length_y, 0)"
    if plane == "YZ":
        return "App.Vector(length_x, 0, 0)"
    return "App.Vector(dx, dy, dz)"


def _approximated_curve_summaries(outlines: List[Any]) -> List[Dict[str, Any]]:
    curves: List[Dict[str, Any]] = []
    for idx, outline in enumerate(outlines[:_MAX_OUTLINES_PER_VIEW * 2]):
        if len(outline.edges) < 8:
            continue
        circle = _fit_outline_circle(outline)
        if circle is not None:
            cx, cy, radius, max_error, rms_error = circle
            curves.append({
                "id": idx,
                "kind": "approximated_circle",
                "center": [_round_num(cx), _round_num(cy)],
                "radius": _round_num(radius),
                "bbox": _round_list(outline.bbox),
                "edge_count": len(outline.edges),
                "max_error": _round_num(max_error),
                "rms_error": _round_num(rms_error),
            })
            continue
        slot = _fit_outline_slot(outline)
        if slot is not None:
            curves.append({"id": idx, **slot})
    return curves


def _approximated_line_circle_summaries(entities: List[DxfEntity]) -> List[Dict[str, Any]]:
    line_segments = []
    for entity in entities:
        if _is_hidden_entity(entity) or entity.kind != "LINE" or len(entity.points) < 2:
            continue
        p0 = (float(entity.points[0][0]), float(entity.points[0][1]))
        p1 = (float(entity.points[1][0]), float(entity.points[1][1]))
        if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) <= 1e-9:
            continue
        line_segments.append((p0, p1))
    if not line_segments:
        return []
    scale = max(
        max(max(abs(v) for v in (*p0, *p1)) for p0, p1 in line_segments),
        1.0,
    )
    tol = max(scale * 1e-5, 1e-6)
    point_to_segments: Dict[Tuple[int, int], List[int]] = {}
    for idx, (p0, p1) in enumerate(line_segments):
        point_to_segments.setdefault(_point_key(p0, tol), []).append(idx)
        point_to_segments.setdefault(_point_key(p1, tol), []).append(idx)

    seen = set()
    curves: List[Dict[str, Any]] = []
    for start in range(len(line_segments)):
        if start in seen:
            continue
        stack = [start]
        component = []
        seen.add(start)
        while stack:
            idx = stack.pop()
            component.append(line_segments[idx])
            for point in line_segments[idx]:
                for neighbor in point_to_segments.get(_point_key(point, tol), []):
                    if neighbor in seen:
                        continue
                    seen.add(neighbor)
                    stack.append(neighbor)
        if len(component) < 8:
            continue
        points = [point for segment in component for point in segment]
        bbox = (
            min(point[0] for point in points),
            min(point[1] for point in points),
            max(point[0] for point in points),
            max(point[1] for point in points),
        )
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        radius = (width + height) * 0.25
        if radius <= tol or abs(width - height) > max(radius * 0.12, tol * 4.0):
            continue
        cx = (bbox[0] + bbox[2]) * 0.5
        cy = (bbox[1] + bbox[3]) * 0.5
        errors = [abs(math.hypot(point[0] - cx, point[1] - cy) - radius) for point in points]
        max_error = max(errors)
        rms_error = math.sqrt(sum(error * error for error in errors) / len(errors))
        if max_error > max(radius * 0.08, tol * 5.0):
            continue
        curves.append({
            "id": len(curves),
            "kind": "approximated_circle",
            "center": [_round_num(cx), _round_num(cy)],
            "radius": _round_num(radius),
            "bbox": _round_list(bbox),
            "edge_count": len(component),
            "max_error": _round_num(max_error),
            "rms_error": _round_num(rms_error),
        })
    return curves


def _point_key(point: Tuple[float, float], tol: float) -> Tuple[int, int]:
    return (round(float(point[0]) / tol), round(float(point[1]) / tol))


def _dedupe_curve_summaries(curves: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for curve in curves:
        if curve.get("kind") != "approximated_circle":
            out.append(curve)
            continue
        if any(existing.get("kind") == "approximated_circle" and _same_circle_center(curve, existing)
               for existing in out):
            continue
        out.append(curve)
    for idx, curve in enumerate(out):
        curve["id"] = idx
    return out


def _fit_outline_circle(outline: Any) -> Optional[Tuple[float, float, float, float, float]]:
    min_x, min_y, max_x, max_y = outline.bbox
    width = float(max_x - min_x)
    height = float(max_y - min_y)
    radius = (width + height) * 0.25
    if radius <= 1e-9:
        return None
    if abs(width - height) > max(radius * 0.10, 1e-6):
        return None
    cx = (float(min_x) + float(max_x)) * 0.5
    cy = (float(min_y) + float(max_y)) * 0.5
    samples = _outline_points(outline)
    if len(samples) < 12:
        return None
    errors = [abs(math.hypot(x - cx, y - cy) - radius) for x, y in samples]
    max_error = max(errors)
    rms_error = math.sqrt(sum(err * err for err in errors) / len(errors))
    if max_error > max(radius * 0.075, 1e-5):
        return None
    return cx, cy, radius, max_error, rms_error


def _fit_outline_slot(outline: Any) -> Optional[Dict[str, Any]]:
    min_x, min_y, max_x, max_y = [float(v) for v in outline.bbox]
    width = max_x - min_x
    height = max_y - min_y
    if min(width, height) <= 1e-9:
        return None
    aspect = max(width, height) / min(width, height)
    if aspect < 1.6 or len(outline.edges) < 12:
        return None
    if width >= height:
        radius = height * 0.5
        center_a = [min_x + radius, (min_y + max_y) * 0.5]
        center_b = [max_x - radius, (min_y + max_y) * 0.5]
        axis = "X"
        length = width
    else:
        radius = width * 0.5
        center_a = [(min_x + max_x) * 0.5, min_y + radius]
        center_b = [(min_x + max_x) * 0.5, max_y - radius]
        axis = "Y"
        length = height
    return {
        "kind": "approximated_rounded_slot",
        "axis_2d": axis,
        "centerline": [_round_point(center_a), _round_point(center_b)],
        "radius": _round_num(radius),
        "overall_length": _round_num(length),
        "bbox": _round_list(outline.bbox),
        "edge_count": len(outline.edges),
    }


def _outline_points(outline: Any) -> List[Tuple[float, float]]:
    points: List[Tuple[float, float]] = []
    for edge in outline.edges:
        p0 = edge.get("p0")
        p1 = edge.get("p1")
        if p0 is not None:
            points.append((float(p0[0]), float(p0[1])))
        if p1 is not None:
            points.append((float(p1[0]), float(p1[1])))
    return points


def _circle_summary(circle: DxfEntity) -> Dict[str, Any]:
    return {
        "center": _round_point(circle.center or (0.0, 0.0)),
        "radius": _round_num(circle.radius or 0.0),
        "bbox": _round_list(circle.bbox()) if circle.bbox() else None,
        "layer": circle.layer,
        "hidden": _is_hidden_entity(circle),
    }


def _is_hidden_entity(entity: DxfEntity) -> bool:
    layer = (entity.layer or "").upper()
    linetype = (entity.linetype or "").upper()
    desc = str(entity.extra.get("linetype_desc") or "").upper()
    return "HID" in layer or "HIDDEN" in linetype or "HIDDEN" in desc


def _modeling_entities(entities: List[DxfEntity]) -> List[DxfEntity]:
    return [entity for entity in entities if not _is_auxiliary_entity(entity)]


def _is_auxiliary_entity(entity: DxfEntity) -> bool:
    text = " ".join([
        str(entity.layer or ""),
        str(entity.linetype or ""),
        str(entity.extra.get("linetype_desc") or ""),
    ]).upper()
    return any(token in text for token in _AUXILIARY_TOKENS)


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _round_num(value: Any) -> float:
    return round(float(value), 6)


def _round_point(point: Any) -> List[float]:
    return [_round_num(point[0]), _round_num(point[1])]


def _round_list(values: Any) -> List[float]:
    return [_round_num(v) for v in values]


def _round_json(value: Any) -> Any:
    if isinstance(value, float):
        return _round_num(value)
    if isinstance(value, list):
        return [_round_json(v) for v in value]
    if isinstance(value, tuple):
        return [_round_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _round_json(v) for k, v in value.items()}
    return value