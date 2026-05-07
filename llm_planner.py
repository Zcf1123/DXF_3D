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
                        draft_features: List[Dict[str, Any]]
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
        return cleaned, f"LLM 复核完成（{self.model}）：返回 {len(cleaned)} 个特征"
