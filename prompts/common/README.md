# 公共提示词知识

本目录存放两条路线共享的提示词知识：

- Direct 路线：确定性生成 `features.json`，再交给 `freecad_builder.py` 建模。
- LLM 路线：使用 JSON/图片摘要，直接生成 FreeCAD Python 脚本。

这里的文件应描述稳定的项目约定，不描述某条路线专用的输出格式。

路线专用提示词放在：

- `direct/prompts/`
- `llm/prompts/`

根目录 `prompts/` 只保留公共知识，不再放路线专用提示词。