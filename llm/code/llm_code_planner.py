"""Direct LLM-to-FreeCAD script helper for the auto modeling route."""
from __future__ import annotations

import ast
import json
import math
import os
import re
import signal
from typing import Any, Dict, List, Optional, Tuple

from ...direct.code.dxf_loader import DxfEntity
from ...direct.code.geometry_estimator import extract_closed_outlines_and_circles
from ...direct.code.llm_planner import Prompt, _SECTION_RE, _load_part_knowledge_for_refiner, _render
from ...direct.code.view_classifier import ViewBundle


HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(os.path.dirname(HERE), "prompts")


def load_prompt(name: str) -> Prompt:
    path = os.path.join(PROMPTS_DIR, f"{name}.md")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    sections: Dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[m.group(1).strip()] = text[m.end():end].strip()

    if "SYSTEM" not in sections or "USER" not in sections:
        raise ValueError(f"Prompt {name}.md missing SYSTEM or USER section")

    examples: List[Tuple[str, str]] = []
    if "EXAMPLES" in sections:
        for block in re.split(r"\n---\s*\n", sections["EXAMPLES"]):
            if "--- output ---" in block:
                inp, out = block.split("--- output ---", 1)
                examples.append((inp.replace("--- input ---", "").strip(),
                                 out.strip()))

    return Prompt(system=sections["SYSTEM"],
                  user_template=sections["USER"],
                  examples=examples)


_MAX_VISIBLE_ENTITIES_PER_VIEW = 0
_MAX_HIDDEN_ENTITIES_PER_VIEW = 4
_MAX_OUTLINES_PER_VIEW = 4
_MAX_EDGES_PER_OUTLINE = 12

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
}
_REQUEST_TIMEOUT_SECONDS = 180
_MAX_SCRIPT_TOKENS = 9000


def build_auto_context(
    dxf_path: str,
    bundles: List[ViewBundle],
    projected: Dict[str, Any],
    model_intent: str = "",
) -> Dict[str, Any]:
    """Build a compact, geometry-first context for direct script generation."""
    return {
        "input_file": os.path.basename(dxf_path),
        "model_intent": model_intent or "（无）",
        "coordinate_convention": {
            "front": "XZ plane: drawing x -> world X, drawing y -> world Z",
            "top": "XY plane: drawing x -> world X, drawing y -> world Y",
            "left": "YZ plane: drawing x -> world Y, drawing y -> world Z",
        },
        "part_knowledge": _load_part_knowledge_for_refiner(),
        "views": [_bundle_summary(bundle) for bundle in bundles],
        "projected_views": {
            name: _projected_view_summary(name, pv)
            for name, pv in projected.items()
        },
    }


def generate_freecad_script(
    llm: Any,
    context: Dict[str, Any],
    base_name: str,
    fcstd_path: str,
    debug_dir: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """Return (script_or_none, message) from the direct FreeCAD generator."""
    if not getattr(llm, "enabled", False):
        return None, f"LLM 已禁用：{getattr(llm, 'disabled_reason', None)}"

    try:
        prompt = load_prompt("freecad_script_generator")
    except Exception as exc:
        return None, f"提示词加载失败：{exc}"

    user_msg = _render(prompt.user_template, {
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
            resp = llm.client.chat.completions.create(
                model=llm.model,
                messages=messages,
                temperature=0.0,
                max_tokens=_MAX_SCRIPT_TOKENS,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        content = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        return None, f"LLM 请求失败：{exc}"

    _write_debug_text(debug_dir, "llm_raw_response.txt", content)
    script = _sanitize_generated_script(strip_code_fence(content))
    _write_debug_text(debug_dir, "generated_model_candidate.py", script)
    ok, reason = validate_generated_script(script)
    if not ok:
        repaired, repair_msg = _repair_generated_script(
            llm, messages, content, reason, debug_dir)
        if repaired is None:
            fallback = _fallback_freecad_script(context, base_name, fcstd_path)
            _write_debug_text(debug_dir, "generated_model_fallback.py", fallback)
            return fallback, f"LLM 脚本未通过安全/结构校验：{reason}；{repair_msg}；已使用 auto_context 兜底脚本"
        return repaired, repair_msg
    return script, f"LLM 直接建模脚本生成完成（{llm.model}）"


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
            "5. 不要使用不存在的 `Part.Extrude`；拉伸必须使用 `Part.Face(...).extrude(App.Vector(...))`。\n"
            "6. `Part.makeCylinder` 正确签名是 `Part.makeCylinder(radius, height, base, direction)` 或带第 5 个 angle；第 4 个参数必须是方向向量，不是数字。\n"
            "7. 直接给短脚本，不要写工程图推理过程，不要写长段注释，避免输出被截断。\n"
            "8. 不要输出 Markdown 解释文字。"
        ),
    })
    try:
        with _alarm_timeout(_REQUEST_TIMEOUT_SECONDS):
            resp = llm.client.chat.completions.create(
                model=llm.model,
                messages=repair_messages,
                temperature=0.0,
                max_tokens=_MAX_SCRIPT_TOKENS,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        content = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        return None, f"自动重试失败：{exc}"

    _write_debug_text(debug_dir, "llm_raw_response_retry.txt", content)
    script = _sanitize_generated_script(strip_code_fence(content))
    _write_debug_text(debug_dir, "generated_model_candidate_retry.py", script)
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
    return text.strip() + "\n"


def _sanitize_generated_script(script: str) -> str:
    script = re.sub(r"(?m)^\s*Part\.setMeasurePrecision\([^\n]*\)\s*\n?", "", script)
    script = re.sub(r"\.(X|Y|Z)\b", lambda m: "." + m.group(1).lower(), script)
    if "Part.fuse(" in script:
        script = script.replace("Part.fuse(", "_fuse_all(")
        script = _ensure_fuse_helper(script)
    return script.strip() + "\n"


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


def _fallback_freecad_script(context: Dict[str, Any], base_name: str, fcstd_path: str) -> str:
    front = _context_view(context, "projected_views", "front") or _context_view(context, "views", "front")
    top = _context_view(context, "projected_views", "top") or _context_view(context, "views", "top")
    curves = list(front.get("approximated_curves") or [])
    outer_ids = _outer_curve_ids(curves)
    depth = _view_extent(top, "height", 1.0) or 1.0
    thin_depth = max(depth / 3.0, depth * 0.25)
    thin_y = max((depth - thin_depth) / 2.0, 0.0)
    lines = [
        "import FreeCAD as App",
        "import Part",
        "",
        f"BASE_NAME = {base_name!r}",
        f"FCSTD_PATH = {fcstd_path!r}",
        "",
        "doc = App.newDocument(BASE_NAME)",
        "Y_AXIS = App.Vector(0, 1, 0)",
        "shapes = []",
        "cuts = []",
        "",
    ]
    for curve in curves:
        curve_id = curve.get("id")
        is_outer = curve_id in outer_ids
        y0 = 0.0 if _is_center_curve(curve, front) else thin_y
        local_depth = depth if _is_center_curve(curve, front) else thin_depth
        target = "shapes" if is_outer else "cuts"
        expr = _curve_shape_expr(curve, y0, local_depth)
        if expr:
            lines.append(f"{target}.append({expr})")
    lines.extend(_fallback_connector_lines(curves, outer_ids, thin_y, thin_depth))
    lines.extend([
        "",
        "if shapes:",
        "    final_shape = shapes[0]",
        "    for shape in shapes[1:]:",
        "        final_shape = final_shape.fuse(shape)",
        "else:",
        f"    final_shape = Part.makeBox({_view_extent(front, 'width', 1.0):.6f}, {depth:.6f}, {_view_extent(front, 'height', 1.0):.6f}, App.Vector(0, 0, 0))",
        "for cut_shape in cuts:",
        "    final_shape = final_shape.cut(cut_shape)",
        "result = doc.addObject('Part::Feature', 'Result')",
        "result.Shape = final_shape",
        "doc.recompute()",
        "doc.saveAs(FCSTD_PATH)",
        "",
    ])
    return "\n".join(lines)


def _context_view(context: Dict[str, Any], section: str, name: str) -> Dict[str, Any]:
    views = context.get(section)
    if isinstance(views, dict):
        item = views.get(name)
        return item if isinstance(item, dict) else {}
    if isinstance(views, list):
        for item in views:
            if isinstance(item, dict) and item.get("name") == name:
                return item
    return {}


def _outer_curve_ids(curves: List[Dict[str, Any]]) -> set:
    outer = set()
    for curve in curves:
        curve_id = curve.get("id")
        bbox = curve.get("bbox") or []
        if len(bbox) != 4:
            continue
        contained = False
        for other in curves:
            if other is curve:
                continue
            obox = other.get("bbox") or []
            if len(obox) == 4 and _bbox_contains(obox, bbox) and _bbox_area(obox) > _bbox_area(bbox):
                contained = True
                break
        if not contained:
            outer.add(curve_id)
    return outer


def _bbox_contains(outer: List[float], inner: List[float], tol: float = 1e-5) -> bool:
    return (outer[0] <= inner[0] + tol and outer[1] <= inner[1] + tol and
            outer[2] >= inner[2] - tol and outer[3] >= inner[3] - tol)


def _bbox_area(bbox: List[float]) -> float:
    return max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))


def _view_extent(view: Dict[str, Any], key: str, default: float) -> float:
    try:
        value = float(view.get(key, default))
        return value if value > 0 else default
    except Exception:
        return default


def _is_center_curve(curve: Dict[str, Any], front: Dict[str, Any]) -> bool:
    center = curve.get("center") or None
    if not center:
        line = curve.get("centerline") or []
        if line:
            center = line[0]
    width = _view_extent(front, "width", 1.0)
    try:
        x = float(center[0])
        return 0.35 * width <= x <= 0.75 * width
    except Exception:
        return False


def _curve_shape_expr(curve: Dict[str, Any], y0: float, depth: float) -> str:
    kind = curve.get("kind")
    if kind == "approximated_circle":
        center = curve.get("center") or [0.0, 0.0]
        radius = float(curve.get("radius") or 0.0)
        if radius <= 0:
            return ""
        return (f"Part.makeCylinder({radius:.6f}, {depth:.6f}, "
                f"App.Vector({float(center[0]):.6f}, {y0:.6f}, {float(center[1]):.6f}), Y_AXIS)")
    if kind == "approximated_rounded_slot":
        return _slot_shape_expr(curve, y0, depth)
    return ""


def _slot_shape_expr(curve: Dict[str, Any], y0: float, depth: float) -> str:
    line = curve.get("centerline") or []
    radius = float(curve.get("radius") or 0.0)
    if len(line) != 2 or radius <= 0:
        return ""
    x1, z1 = float(line[0][0]), float(line[0][1])
    x2, z2 = float(line[1][0]), float(line[1][1])
    xmin, xmax = min(x1, x2), max(x1, x2)
    zmin, zmax = min(z1, z2), max(z1, z2)
    if abs(x1 - x2) <= abs(z1 - z2):
        straight = max(0.0, zmax - zmin)
        box = (f"Part.makeBox({2 * radius:.6f}, {depth:.6f}, {straight:.6f}, "
               f"App.Vector({x1 - radius:.6f}, {y0:.6f}, {zmin:.6f}))")
    else:
        straight = max(0.0, xmax - xmin)
        box = (f"Part.makeBox({straight:.6f}, {depth:.6f}, {2 * radius:.6f}, "
               f"App.Vector({xmin:.6f}, {y0:.6f}, {z1 - radius:.6f}))")
    cyl1 = f"Part.makeCylinder({radius:.6f}, {depth:.6f}, App.Vector({x1:.6f}, {y0:.6f}, {z1:.6f}), Y_AXIS)"
    cyl2 = f"Part.makeCylinder({radius:.6f}, {depth:.6f}, App.Vector({x2:.6f}, {y0:.6f}, {z2:.6f}), Y_AXIS)"
    return f"({box}).fuse({cyl1}).fuse({cyl2})"


def _fallback_connector_lines(curves: List[Dict[str, Any]], outer_ids: set, y0: float, depth: float) -> List[str]:
    bboxes = [curve.get("bbox") for curve in curves if curve.get("id") in outer_ids and len(curve.get("bbox") or []) == 4]
    if len(bboxes) < 2:
        return []
    bboxes = sorted(bboxes, key=lambda b: (float(b[0]) + float(b[2])) / 2.0)
    lines = ["# conservative connector blocks between neighboring outer profiles"]
    for left, right in zip(bboxes, bboxes[1:]):
        gap = float(right[0]) - float(left[2])
        if gap <= 0:
            continue
        zmin = min(float(left[1]), float(right[1]))
        zmax = max(float(left[3]), float(right[3]))
        lines.append(
            f"shapes.append(Part.makeBox({gap:.6f}, {depth:.6f}, {zmax - zmin:.6f}, "
            f"App.Vector({float(left[2]):.6f}, {y0:.6f}, {zmin:.6f})))")
    return lines


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
    required = ("FreeCAD", "Part", "Result", "saveAs")
    missing = [token for token in required if token not in script]
    if missing:
        return False, f"missing required tokens: {missing}"
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return False, f"syntax error: {exc}"
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
            if func_name == "Part.makeCylinder" and len(node.args) >= 4 and _is_number_literal(node.args[3]):
                return False, "Part.makeCylinder fourth argument must be a direction App.Vector, not a number; use Part.makeCylinder(radius, height, base, App.Vector(...))"
    return True, "ok"


def _is_number_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float))


def _bundle_summary(bundle: ViewBundle) -> Dict[str, Any]:
    outlines, circles = extract_closed_outlines_and_circles(bundle, hidden_pred=_is_hidden_entity)
    return {
        "name": bundle.name,
        "bbox": _round_list(bundle.bbox),
        "width": _round_num(bundle.width),
        "height": _round_num(bundle.height),
        "entity_count": len(bundle.entities),
        "annotations": [_annotation_summary(ann) for ann in bundle.annotations],
        "visible_closed_outline_bboxes": [_outline_bbox_summary(o) for o in outlines[:_MAX_OUTLINES_PER_VIEW]],
        "approximated_curves": _approximated_curve_summaries(outlines),
        "visible_circles": [_circle_summary(c) for c in circles],
    }


def _projected_view_summary(name: str, pv: Any) -> Dict[str, Any]:
    temp_bundle = ViewBundle(name=name, bbox=(0.0, 0.0, float(pv.width), float(pv.height)), entities=pv.entities)
    outlines, circles = extract_closed_outlines_and_circles(temp_bundle, hidden_pred=_is_hidden_entity)
    visible = [e for e in pv.entities if not _is_hidden_entity(e)]
    hidden = [e for e in pv.entities if _is_hidden_entity(e)]
    return {
        "name": name,
        "plane": pv.plane,
        "origin_2d": _round_list(pv.origin_2d),
        "width": _round_num(pv.width),
        "height": _round_num(pv.height),
        "entity_count": len(pv.entities),
        "visible_closed_outlines": [_outline_bbox_summary(o) for o in outlines[:_MAX_OUTLINES_PER_VIEW]],
        "approximated_curves": _approximated_curve_summaries(outlines),
        "visible_circles": [_circle_summary(c) for c in circles],
        "key_visible_entities": _key_entity_summaries(visible, _MAX_VISIBLE_ENTITIES_PER_VIEW),
        "key_hidden_entities": _key_entity_summaries(hidden, _MAX_HIDDEN_ENTITIES_PER_VIEW),
    }


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
    return {
        "bbox": _round_list(outline.bbox),
        "width": _round_num(outline.width),
        "height": _round_num(outline.height),
        "edge_count": len(outline.edges),
        "edges": _round_json(edges),
    }


def _outline_bbox_summary(outline: Any) -> Dict[str, Any]:
    return {
        "bbox": _round_list(outline.bbox),
        "width": _round_num(outline.width),
        "height": _round_num(outline.height),
        "edge_count": len(outline.edges),
    }


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