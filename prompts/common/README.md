# Common Prompt Knowledge

This folder stores prompt knowledge shared by both routes:

- Direct route: deterministic `features.json` generation plus `freecad_builder.py`.
- LLM route: JSON/images plus direct FreeCAD Python generation.

Files here should describe stable project conventions, not route-specific output formats.

Route-specific prompts stay in:

- `direct/prompts/`
- `llm/prompts/`

The root `prompts/` files are still kept for backward compatibility with the current runtime loader.