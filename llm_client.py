"""Shared LLM client and prompt helpers for DXF_3D."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class Prompt:
    system: str
    user_template: str
    examples: List[Tuple[str, str]]


SECTION_RE = re.compile(r"^##\s+([A-Z_]+)\s*$", re.MULTILINE)


def load_prompt_from_dir(prompts_dir: str, name: str) -> Prompt:
    path = os.path.join(prompts_dir, f"{name}.md")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    sections: Dict[str, str] = {}
    matches = list(SECTION_RE.finditer(text))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[match.group(1).strip()] = text[match.end():end].strip()

    if "SYSTEM" not in sections or "USER" not in sections:
        raise ValueError(f"Prompt {name}.md missing SYSTEM or USER section")

    examples: List[Tuple[str, str]] = []
    if "EXAMPLES" in sections:
        for block in re.split(r"\n---\s*\n", sections["EXAMPLES"]):
            if "--- output ---" in block:
                inp, out = block.split("--- output ---", 1)
                examples.append((inp.replace("--- input ---", "").strip(), out.strip()))

    return Prompt(
        system=sections["SYSTEM"],
        user_template=sections["USER"],
        examples=examples,
    )


def render_template(template: str, vars: Dict[str, Any]) -> str:
    def repl(match: "re.Match[str]") -> str:
        key = match.group(1).strip()
        if key not in vars:
            return match.group(0)
        value = vars[key]
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, indent=2)

    return re.sub(r"\{\{\s*([\w.]+)\s*\}\}", repl, template)


class LLMClient:
    def __init__(self, config_path: str = "config.json", disabled: bool = False):
        self.config: Dict[str, Any] = {}
        self.client = None
        self.model: str = "(none)"
        self.api_mode: str = "chat"
        self.disabled_reason: Optional[str] = None

        env_disabled = os.environ.get("DXF_3D_DISABLE_LLM", "").strip().lower()
        if disabled or env_disabled in {"1", "true", "yes", "on"}:
            self.disabled_reason = "disabled by option"
            return

        if not os.path.exists(config_path):
            self.disabled_reason = f"config not found: {config_path}"
            return
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                self.config = json.load(f)
            self.config = _resolve_config_profile(self.config)
        except Exception as exc:
            self.disabled_reason = f"config load failed: {exc}"
            return

        api_key = self.config.get("api_key") or self.config.get("openai_api_key")
        if not api_key:
            self.disabled_reason = "api_key missing in config"
            return

        try:
            from openai import OpenAI  # type: ignore
        except Exception as exc:
            self.disabled_reason = f"openai package unavailable: {exc}"
            return

        kwargs: Dict[str, Any] = {"api_key": api_key}
        base_url = self.config.get("base_url") or self.config.get("openai_base_url")
        if base_url:
            kwargs["base_url"] = base_url
        try:
            self.client = OpenAI(**kwargs)
        except Exception as exc:
            self.disabled_reason = f"OpenAI client init failed: {exc}"
            return

        self.model = self.config.get("model") or self.config.get("openai_model", "(unknown)")
        self.api_mode = _resolve_api_mode(self.config, bool(base_url))

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def complete_text(
        self,
        messages: List[Dict[str, str]],
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> str:
        if self.api_mode == "responses":
            instructions = "\n\n".join(
                msg.get("content", "") for msg in messages
                if msg.get("role") == "system"
            ).strip()
            input_messages = [
                {"role": msg.get("role", "user"), "content": msg.get("content", "")}
                for msg in messages
                if msg.get("role") != "system"
            ]
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "input": input_messages,
            }
            if instructions:
                kwargs["instructions"] = instructions
            if max_tokens is not None:
                kwargs["max_output_tokens"] = max_tokens
            if timeout is not None:
                kwargs["timeout"] = timeout
            resp = self.client.responses.create(**kwargs)
            return (getattr(resp, "output_text", "") or "").strip()

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.0,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = self.client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()


def _resolve_config_profile(config: Dict[str, Any]) -> Dict[str, Any]:
    profiles = config.get("profiles")
    if not isinstance(profiles, dict):
        return config

    active = config.get("active") or config.get("active_profile")
    if not active:
        raise ValueError("config has profiles but no active profile")
    profile = profiles.get(active)
    if not isinstance(profile, dict):
        raise ValueError(f"active profile not found: {active}")

    merged = {
        key: value for key, value in config.items()
        if key not in {"profiles", "active", "active_profile"}
    }
    merged.update(profile)
    return merged


def _resolve_api_mode(config: Dict[str, Any], has_base_url: bool) -> str:
    mode = str(config.get("api_mode") or config.get("api_type") or "").strip().lower()
    if mode in {"responses", "response"}:
        return "responses"
    if mode in {"chat", "chat_completions", "chat.completions"}:
        return "chat"
    return "chat" if has_base_url else "responses"