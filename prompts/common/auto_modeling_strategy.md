# Auto Modeling Strategy

Use this knowledge for the LLM-authored FreeCAD code route.

## Goal

Generate FreeCAD Python directly from three-view JSON summaries and images.

## Recommended Reasoning

- Identify the main part family from view layout, fitted curves, closed outlines, hidden lines, and optional user intent.
- Build simple solids first: cylinders, boxes, extruded profiles, and fused additive components.
- Apply cuts after positive solids are fused: round holes, rounded slots, rectangular/profile cuts, and blind cuts.
- Use TOP and LEFT to decide local depth/offset; do not default every FRONT component to full depth.
- Prefer exact FreeCAD primitives for fitted curves: `Part.makeCylinder`, `Part.Circle`, `Part.Arc`, `Part.Face`, `cut`, and `fuse`.

## Output Contract

- Output complete Python code only.
- Create a final `Part::Feature` object named `Result`.
- Assign the final solid to `Result.Shape`.
- Call `doc.recompute()` and `doc.saveAs(FCSTD_PATH)`.
- Do not use shell, network, file deletion, `eval`, or `exec`.