"""Lightweight LLM helper for the DXF_3D pipeline.

Loads OpenAI-compatible config from `config.json`, parses Markdown
prompt files in `DXF_3D/prompts/`, and exposes:

    LLMPlanner.refine_features(view_bboxes, draft_features) -> List[dict]

Any failure (no API key, network error, parse error) is non-fatal: the
caller simply uses the original draft and a reason string is logged.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(HERE, "prompts")


# ---------------------------------------------------------------------------
# Prompt loading (Markdown spec, see prompts/PROMPT_SPEC.md)
# ---------------------------------------------------------------------------

@dataclass
class Prompt:
    system: str
    user_template: str
    examples: List[Tuple[str, str]]


_SECTION_RE = re.compile(r"^##\s+([A-Z_]+)\s*$", re.MULTILINE)


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


def _render(template: str, vars: Dict[str, Any]) -> str:
    def repl(m: "re.Match[str]") -> str:
        key = m.group(1).strip()
        if key not in vars:
            return m.group(0)
        v = vars[key]
        if isinstance(v, str):
            return v
        return json.dumps(v, ensure_ascii=False, indent=2)
    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", repl, template)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class LLMPlanner:
    def __init__(self, config_path: str = "config.json"):
        self.config: Dict[str, Any] = {}
        self.client = None
        self.model: str = "(none)"
        self.disabled_reason: Optional[str] = None

        if not os.path.exists(config_path):
            self.disabled_reason = f"config not found: {config_path}"
            return
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
        except Exception as exc:
            self.disabled_reason = f"config load failed: {exc}"
            return

        api_key = self.config.get("openai_api_key")
        if not api_key:
            self.disabled_reason = "openai_api_key missing in config"
            return

        try:
            from openai import OpenAI  # type: ignore
        except Exception as exc:
            self.disabled_reason = f"openai package unavailable: {exc}"
            return

        kwargs: Dict[str, Any] = {"api_key": api_key}
        base_url = self.config.get("openai_base_url") or self.config.get("base_url")
        if base_url:
            kwargs["base_url"] = base_url
        try:
            self.client = OpenAI(**kwargs)
        except Exception as exc:
            self.disabled_reason = f"OpenAI client init failed: {exc}"
            return

        self.model = self.config.get("openai_model", "(unknown)")

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def refine_features(self, view_bboxes: Dict[str, Any],
                        draft_features: List[Dict[str, Any]],
                        view_geometry: Optional[List[Dict[str, Any]]] = None,
                        ) -> Tuple[Optional[List[Dict[str, Any]]], str]:
        """Return (refined_features_or_None, log_message)."""
        if _is_deterministic_sphere_draft(draft_features):
            return draft_features, "算法已确定为球体，跳过 LLM 特征改写"
        if not self.enabled:
            return None, f"LLM 已禁用：{self.disabled_reason}"

        try:
            prompt = load_prompt("feature_refiner")
        except Exception as exc:
            return None, f"提示词加载失败：{exc}"

        user_msg = _render(prompt.user_template, {
            "view_bboxes": view_bboxes,
            "view_geometry": view_geometry or [],
            "draft_features": draft_features,
        })
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": prompt.system},
        ]
        for inp, out in prompt.examples:
            messages.append({"role": "user", "content": inp})
            messages.append({"role": "assistant", "content": out})
        messages.append({"role": "user", "content": user_msg})

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.0,
            )
            content = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            return None, f"LLM 请求失败：{exc}"

        # Strip an optional ```json fence.
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        try:
            data = json.loads(content)
        except Exception as exc:
            return None, f"LLM 返回非 JSON：{exc}：{content[:200]!r}"

        feats = data.get("features")
        if not isinstance(feats, list):
            return None, f"LLM JSON 缺少 features 列表：keys={list(data)}"

        # Light validation: drop any item without kind/params.
        cleaned: List[Dict[str, Any]] = []
        for f in feats:
            if isinstance(f, dict) and "kind" in f and "params" in f \
               and isinstance(f["params"], dict):
                cleaned.append({"kind": f["kind"], "params": f["params"]})

        ok, reason = _validate_refined_features(
            draft_features, cleaned, view_geometry or []
        )
        if not ok:
            return None, f"LLM 结果未通过程序校验：{reason}；沿用算法草案"
        cleaned = _remove_duplicate_hole_features(cleaned)
        cleaned = _order_features_for_builder(cleaned)
        return cleaned, f"LLM 复核完成（{self.model}）：返回 {len(cleaned)} 个特征"

    def review_views(self, view_summary: List[Dict[str, Any]]) \
            -> Tuple[Optional[Dict[str, Any]], str]:
        """Return semantic view cleanup instructions, or None on failure."""
        if not self.enabled:
            return None, f"LLM 已禁用：{self.disabled_reason}"

        try:
            prompt = load_prompt("drawing_view_reviewer")
        except Exception as exc:
            return None, f"提示词加载失败：{exc}"

        user_msg = _render(prompt.user_template, {
            "view_summary": view_summary,
        })
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": prompt.system},
        ]
        for inp, out in prompt.examples:
            messages.append({"role": "user", "content": inp})
            messages.append({"role": "assistant", "content": out})
        messages.append({"role": "user", "content": user_msg})

        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.0,
            )
            content = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            return None, f"LLM 请求失败：{exc}"

        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        try:
            data = json.loads(content)
        except Exception as exc:
            return None, f"LLM 返回非 JSON：{exc}：{content[:200]!r}"

        ok, reason = _validate_view_review(view_summary, data)
        if not ok:
            return None, f"LLM 视图复核未通过程序校验：{reason}"
        return data, f"LLM 视图语义复核完成（{self.model}）"


# ---------------------------------------------------------------------------
# Deterministic safety checks for LLM output
# ---------------------------------------------------------------------------

_ALLOWED_KINDS = {"extrude_profile", "base_block", "sphere", "hole", "edge_chamfer"}
_CANONICAL_VIEWS = {"front", "top", "right"}


def _validate_view_review(
    view_summary: List[Dict[str, Any]],
    data: Dict[str, Any],
) -> Tuple[bool, str]:
    if not isinstance(data, dict):
        return False, "top-level JSON is not an object"
    views = data.get("views")
    if not isinstance(views, list):
        return False, "missing views list"

    by_input = {
        str(v.get("input_name")): v for v in view_summary
        if isinstance(v, dict) and v.get("input_name") is not None
    }
    if len(views) != len(by_input):
        return False, "view count changed"
    used_names = set()
    for item in views:
        if not isinstance(item, dict):
            return False, "view item is not an object"
        input_name = str(item.get("input_name"))
        if input_name not in by_input:
            return False, f"unknown input_name {input_name!r}"
        canonical_name = item.get("canonical_name")
        if canonical_name not in _CANONICAL_VIEWS:
            return False, f"invalid canonical_name {canonical_name!r}"
        if canonical_name in used_names:
            return False, f"duplicate canonical_name {canonical_name!r}"
        used_names.add(canonical_name)

        keep_ids = item.get("keep_entity_ids")
        remove_ids = item.get("remove_entity_ids", [])
        if keep_ids is not None and not isinstance(keep_ids, list):
            return False, f"keep_entity_ids for {input_name} is not a list"
        if not isinstance(remove_ids, list):
            return False, f"remove_entity_ids for {input_name} is not a list"

        valid_ids = {int(e.get("id")) for e in by_input[input_name].get("entities", [])
                     if isinstance(e, dict) and isinstance(e.get("id"), int)}
        for ids, label in ((keep_ids or [], "keep"), (remove_ids, "remove")):
            for value in ids:
                if not isinstance(value, int):
                    return False, f"{label}_entity_ids contains non-int"
                if value not in valid_ids:
                    return False, f"{label}_entity_ids contains unknown id {value}"
        if keep_ids is not None:
            kept = set(keep_ids)
            min_keep = 1 if len(valid_ids) < 3 else max(3, int(len(valid_ids) * 0.35))
            if len(kept) < min_keep:
                return False, f"too many entities removed from {input_name}"
    return True, "ok"


def _validate_refined_features(
    draft: List[Dict[str, Any]],
    refined: List[Dict[str, Any]],
    view_geometry: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[bool, str]:
    """Validate that the LLM only made safe, minimal edits.

    The prompt tells the model not to delete holes or rewrite the chosen
    extrusion profile. It may add one edge_chamfer only when the view geometry
    contains strong cross-view evidence, and this function enforces that in
    code so a bad completion cannot silently degrade the final model.
    """
    for item in refined:
        kind = item.get("kind")
        if kind not in _ALLOWED_KINDS:
            return False, f"unknown feature kind {kind!r}"

    draft_base = [
        f for f in draft
        if f.get("kind") in {"extrude_profile", "base_block", "sphere"}
    ]
    refined_base = [
        f for f in refined
        if f.get("kind") in {"extrude_profile", "base_block", "sphere"}
    ]
    if len(draft_base) != len(refined_base):
        return False, "base/profile feature count changed"
    for i, (before, after) in enumerate(zip(draft_base, refined_base)):
        if before.get("kind") != after.get("kind"):
            return False, f"base/profile kind changed at index {i}"
        ok, reason = _validate_base_feature(before, after)
        if not ok:
            return False, reason

    draft_holes = _dedupe_holes([f for f in draft if f.get("kind") == "hole"])
    refined_holes = _dedupe_holes([f for f in refined if f.get("kind") == "hole"])
    if len(refined_holes) < len(draft_holes):
        return False, (
            f"hole count decreased from {len(draft_holes)} "
            f"to {len(refined_holes)}"
        )
    for hole in draft_holes:
        if not any(_same_hole(hole, candidate) for candidate in refined_holes):
            return False, "draft hole missing from refined features"
    if len(refined_holes) > len(draft_holes):
        return False, "new hole feature was added"

    draft_chamfers = [f for f in draft if f.get("kind") == "edge_chamfer"]
    refined_chamfers = [f for f in refined if f.get("kind") == "edge_chamfer"]
    if len(refined_chamfers) < len(draft_chamfers):
        return False, "edge_chamfer feature count changed"
    if len(refined_chamfers) > len(draft_chamfers) + 1:
        return False, "too many edge_chamfer features were added"
    for before, after in zip(draft_chamfers, refined_chamfers[:len(draft_chamfers)]):
        if before.get("params") != after.get("params"):
            return False, "edge_chamfer params changed"
    if len(refined_chamfers) > len(draft_chamfers):
        added = refined_chamfers[-1]
        if not _edge_chamfer_has_view_evidence(draft, added, view_geometry or []):
            return False, "added edge_chamfer lacks view evidence"

    return True, "ok"


def _is_deterministic_sphere_draft(features: List[Dict[str, Any]]) -> bool:
    return (
        len(features) == 1
        and isinstance(features[0], dict)
        and features[0].get("kind") == "sphere"
        and isinstance(features[0].get("params"), dict)
    )


def _edge_chamfer_has_view_evidence(
    draft: List[Dict[str, Any]],
    feature: Dict[str, Any],
    view_geometry: List[Dict[str, Any]],
) -> bool:
    params = feature.get("params", {})
    if params.get("scope") != "outer_z_edges":
        return False
    profile = params.get("profile")
    if profile not in {"arc_revolve", "arc", "line"}:
        return False
    try:
        distance = float(params.get("distance"))
    except Exception:
        return False
    if distance <= 0:
        return False
    if profile == "arc_revolve":
        try:
            if float(params.get("top_radius")) <= 0:
                return False
        except Exception:
            return False

    base = next(
        (f for f in draft if f.get("kind") == "extrude_profile"),
        None,
    )
    base_params = base.get("params", {}) if isinstance(base, dict) else {}
    if base_params.get("source_view") != "top" or base_params.get("plane") != "XY":
        return False
    if len(base_params.get("edges", [])) < 5:
        return False

    by_name = {
        str(v.get("input_name")): v for v in view_geometry
        if isinstance(v, dict) and v.get("input_name") is not None
    }
    top_entities = by_name.get("top", {}).get("entities", [])
    if not top_entities:
        return False

    side_entities = []
    for name in ("front", "right"):
        side_entities.extend(by_name.get(name, {}).get("entities", []))
    if not any(e.get("kind") == "ARC" for e in side_entities):
        return False
    if profile == "arc_revolve":
        top_circles = [e for e in top_entities if e.get("kind") == "CIRCLE"]
        radii = []
        for circle in top_circles:
            try:
                radii.append(float(circle.get("radius")))
            except Exception:
                pass
        if len(radii) < 2:
            return False
        try:
            top_radius = float(params.get("top_radius"))
        except Exception:
            return False
        if abs(top_radius - max(radii)) > max(0.2, max(radii) * 0.03):
            return False
    return True


def _validate_base_feature(
    before: Dict[str, Any],
    after: Dict[str, Any],
) -> Tuple[bool, str]:
    kind = before.get("kind")
    bp = before.get("params", {})
    ap = after.get("params", {})
    if kind == "extrude_profile":
        for key in ("plane", "source_view", "edges"):
            if bp.get(key) != ap.get(key):
                return False, f"extrude_profile {key} changed"
    elif kind == "base_block":
        for key in ("width", "depth", "height", "origin"):
            if key in bp and bp.get(key) != ap.get(key):
                return False, f"base_block {key} changed"
    elif kind == "sphere":
        try:
            before_radius = float(bp.get("radius"))
            after_radius = float(ap.get("radius"))
        except Exception:
            return False, "sphere radius invalid"
        tol = max(abs(before_radius) * 1e-6, 1e-6)
        if abs(before_radius - after_radius) > tol:
            return False, "sphere radius changed"
        before_center = bp.get("center", [])
        after_center = ap.get("center", [])
        if len(before_center) != 3 or len(after_center) != 3:
            return False, "sphere center invalid"
        try:
            if any(abs(float(before_center[i]) - float(after_center[i])) > tol
                   for i in range(3)):
                return False, "sphere center changed"
        except Exception:
            return False, "sphere center invalid"
    return True, "ok"


def _dedupe_holes(holes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique: List[Dict[str, Any]] = []
    for hole in holes:
        if not any(_same_hole(hole, seen) for seen in unique):
            unique.append(hole)
    return unique


def _remove_duplicate_hole_features(
    features: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen_holes: List[Dict[str, Any]] = []
    for feature in features:
        if feature.get("kind") != "hole":
            out.append(feature)
            continue
        if any(_same_hole(feature, seen) for seen in seen_holes):
            continue
        seen_holes.append(feature)
        out.append(feature)
    return out


def _order_features_for_builder(
    features: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    order = {"extrude_profile": 0, "base_block": 0, "sphere": 0, "hole": 1, "edge_chamfer": 2}
    return sorted(
        features,
        key=lambda feature: order.get(str(feature.get("kind")), 99),
    )


def _same_hole(a: Dict[str, Any], b: Dict[str, Any], tol: float = 0.1) -> bool:
    ap = a.get("params", {})
    bp = b.get("params", {})
    if ap.get("axis") != bp.get("axis"):
        return False
    if ap.get("source_view") != bp.get("source_view"):
        return False
    try:
        if abs(float(ap.get("radius")) - float(bp.get("radius"))) > tol:
            return False
        apos = ap.get("position", [])
        bpos = bp.get("position", [])
        if len(apos) != 3 or len(bpos) != 3:
            return False
        return all(abs(float(apos[i]) - float(bpos[i])) <= tol for i in range(3))
    except Exception:
        return False
