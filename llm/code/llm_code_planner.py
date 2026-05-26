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
    outlines = top.get("visible_closed_outlines") or []
    if not outlines:
        return None
    top_width = _view_extent(top, "width", 0.0)
    top_height = _view_extent(top, "height", 0.0)
    height_z = _view_extent(front, "height", _view_extent(left, "height", 0.0))
    if top_width <= 0.0 or top_height <= 0.0 or height_z <= 0.0:
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
            scale["width_x"], 0.0, 0.0, -float(bb.XMin) * scale["width_x"],
            0.0, scale["depth_y"], 0.0, -float(bb.YMin) * scale["depth_y"],
            0.0, 0.0, scale["height_z"], -float(bb.ZMin) * scale["height_z"],
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
        "regular_polygon_hints": _regular_polygon_hints(outlines),
        "extrusion_profile_hints": _extrusion_profile_hints(outlines, pv.plane, float(pv.width)),
        "approximated_curves": _dedupe_curve_summaries(
            _approximated_curve_summaries(outlines) + _approximated_line_circle_summaries(modeling_entities)
        ),
        "visible_circles": [_circle_summary(c) for c in circles],
        "key_visible_entities": _key_entity_summaries(visible, _MAX_VISIBLE_ENTITIES_PER_VIEW),
        "key_hidden_entities": _key_entity_summaries(hidden, _MAX_HIDDEN_ENTITIES_PER_VIEW),
    }


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
    edges = outline.to_dict().get("edges", [])[:_MAX_EDGES_PER_OUTLINE]
    sample_points = _sample_outline_points(outline, 24) if len(outline.edges) > _MAX_EDGES_PER_OUTLINE else []
    profile_points = _sample_outline_points(outline, _MAX_PROFILE_SAMPLE_POINTS) if len(outline.edges) >= 80 else []
    return {
        "bbox": _round_list(outline.bbox),
        "width": _round_num(outline.width),
        "height": _round_num(outline.height),
        "edge_count": len(outline.edges),
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