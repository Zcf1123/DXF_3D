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

        ok, reason = _validate_refined_features(draft_features, cleaned)
        if not ok:
            return None, f"LLM 结果未通过程序校验：{reason}；沿用算法草案"
        cleaned = _remove_duplicate_hole_features(cleaned)
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

_ALLOWED_KINDS = {"extrude_profile", "base_block", "hole", "edge_chamfer"}
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
            if len(kept) < max(3, int(len(valid_ids) * 0.35)):
                return False, f"too many entities removed from {input_name}"
    return True, "ok"


def _validate_refined_features(
    draft: List[Dict[str, Any]],
    refined: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Validate that the LLM only made safe, minimal edits.

    The prompt tells the model not to delete holes, not to rewrite the chosen
    extrusion profile, and not to invent new features.  This function enforces
    those constraints in code so a bad completion cannot silently degrade the
    final model.
    """
    for item in refined:
        kind = item.get("kind")
        if kind not in _ALLOWED_KINDS:
            return False, f"unknown feature kind {kind!r}"

    draft_base = [
        f for f in draft
        if f.get("kind") in {"extrude_profile", "base_block"}
    ]
    refined_base = [
        f for f in refined
        if f.get("kind") in {"extrude_profile", "base_block"}
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
    if len(draft_chamfers) != len(refined_chamfers):
        return False, "edge_chamfer feature count changed"
    for before, after in zip(draft_chamfers, refined_chamfers):
        if before.get("params") != after.get("params"):
            return False, "edge_chamfer params changed"

    return True, "ok"


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
