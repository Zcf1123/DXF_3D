# Direct Route

This folder contains the current deterministic DXF-to-FreeCAD route.
The repository root keeps only the public entry script; runtime code lives here.

Run from the repository root:

```bash
./run.sh -d dxf_files/00005340.dxf
```

Contents:

- `code/`: parser, classifier, projection, feature inference, FreeCAD builder, exporters, and route runner.
- `prompts/`: prompts used by the current controlled route.

Design boundary:

- This route builds `features.json` first, then `freecad_builder.py` interprets those features with FreeCAD APIs.
- It is stable fallback behavior, not the long-term place for every new part family rule.