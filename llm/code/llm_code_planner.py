"""Direct LLM-to-FreeCAD script helper for the auto modeling route."""
from __future__ import annotations

import ast
import json
import math
import os
import re
import signal
from typing import Any, Dict, List, Optional, Tuple

from .dxf_loader import DxfEntity
from .geometry_estimator import extract_closed_outlines_and_circles
from .llm_planner import _load_part_knowledge_for_refiner, _render, load_prompt
from .view_classifier import ViewBundle


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
_REQUEST_TIMEOUT_SECONDS = 180


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
                max_tokens=5000,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        content = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        return None, f"LLM 请求失败：{exc}"

    _write_debug_text(debug_dir, "llm_raw_response.txt", content)
    script = strip_code_fence(content)
    _write_debug_text(debug_dir, "generated_model_candidate.py", script)
    ok, reason = validate_generated_script(script)
    if not ok:
        repaired, repair_msg = _repair_generated_script(
            llm, messages, content, reason, debug_dir)
        if repaired is None:
            return None, f"LLM 脚本未通过安全/结构校验：{reason}；{repair_msg}"
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
            "5. 不要输出 Markdown 解释文字。"
        ),
    })
    try:
        with _alarm_timeout(_REQUEST_TIMEOUT_SECONDS):
            resp = llm.client.chat.completions.create(
                model=llm.model,
                messages=repair_messages,
                temperature=0.0,
                max_tokens=5000,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        content = (resp.choices[0].message.content or "").strip()
    except Exception as exc:
        return None, f"自动重试失败：{exc}"

    _write_debug_text(debug_dir, "llm_raw_response_retry.txt", content)
    script = strip_code_fence(content)
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
    if text.startswith("```"):
        text = re.sub(r"^```(?:python)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip() + "\n"


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
    return True, "ok"


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