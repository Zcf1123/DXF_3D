# Direct Route

This folder is a snapshot of the current deterministic DXF-to-FreeCAD route.
The active runtime files remain in the repository root so existing commands keep working.

Run from the repository root:

```bash
./direct/run_direct.sh dxf_files/00005340.dxf
```

Equivalent root command:

```bash
./run.sh -d dxf_files/00005340.dxf
```

Contents:

- `code/`: current parser, classifier, projection, feature inference, FreeCAD builder, exporters, and runner snapshots.
- `prompts/`: prompts used by the current controlled route.

Design boundary:

- This route builds `features.json` first, then `freecad_builder.py` interprets those features with FreeCAD APIs.
- It is stable fallback behavior, not the long-term place for every new part family rule.