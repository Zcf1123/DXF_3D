# View Conventions

Use these conventions in every DXF_3D prompt and modeling path.

## Fixed Layout

- FRONT: upper-left view, main/front view.
- LEFT: upper-right view, true left view, canonical name `left`.
- TOP: lower-left view.
- Lower-right quadrant is empty/reserved.

## World Mapping

- FRONT maps to world XZ: drawing x -> X, drawing y -> Z.
- TOP maps to world XY: drawing x -> X, drawing y -> Y.
- LEFT maps to world YZ: drawing x -> Y, drawing y -> Z.
- Z is height, X is width, Y is depth.

## Entity Semantics

- Visible outline lines define external profiles and visible steps.
- Hidden/dashed lines are not external outlines; use them as evidence for holes, slots, blind cuts, and obscured edges.
- Center lines are axis/position evidence, not solid material.
- Dimension annotations should inform sizes but should not become model geometry.
- If DXF curves have been exploded into many short LINE entities, prefer fitted circle/arc/rounded-slot summaries over raw line sequences.

## Modeling Priority

- FRONT usually controls XZ profile shape.
- TOP usually controls Y-depth, Y-offset, and local thickness.
- LEFT validates YZ height/depth relationships and hidden-line evidence.