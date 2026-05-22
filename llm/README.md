# LLM Route

This folder contains the direct LLM modeling route for the original project goal:

```text
DXF -> three-view JSON/images -> LLM writes FreeCAD Python -> execute/export
```

Run from the repository root:

```bash
./run.sh -d --auto dxf_files/00005340.dxf
```

Contents:

- `code/llm_code_planner.py`: direct LLM script generation helper used by `direct/code/run.py` when `--auto` is set.
- `prompts/freecad_script_generator.md`: direct FreeCAD script generation prompt.

Design boundary:

- This route should prefer improving JSON/image summaries, prompt quality, and automatic repair loops.
- Avoid adding new part-family modeling rules to `feature_inference.py` when the goal is LLM-authored modeling.