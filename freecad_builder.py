"""Build a FreeCAD model from inferred features.

Outputs a `.FCStd` file containing:
  - "Result"          : the final 3D solid
  - "DXF_FRONT/TOP/RIGHT" : the original 2D three-view drawings embedded as
                            edge compounds in their respective 3D planes,
                            so they are visible in the model tree alongside
                            the solid.

Requires FreeCAD to be importable (e.g. run under `freecadcmd`).
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Optional

from .feature_inference import Feature


def build_model(features: List[Feature], out_dir: str,
                base_name: str = "model",
                projected: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a FreeCAD document and save it as <base_name>.FCStd.

    When *projected* (a dict of ``ProjectedView`` objects keyed by view name)
    is supplied, the three 2D views are embedded in the document as named
    edge compounds alongside the solid.

    Returns {"fcstd": path} on success, or {"error": "..."} on failure.
    """
    os.makedirs(out_dir, exist_ok=True)

    try:
        import FreeCAD  # noqa: F401
    except Exception as exc:
        return {"error": f"FreeCAD is not importable: {exc}"}

    try:
        return _direct_build(features, out_dir, base_name, projected)
    except Exception as exc:
        return {"error": f"build failed: {exc}"}


# ---------------------------------------------------------------------------

def _direct_build(features: List[Feature], out_dir: str,
                  base_name: str,
                  projected: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    import FreeCAD as App
    import Part

    doc = App.newDocument(base_name)
    solid = None
    warnings: List[str] = []

    for f in features:
        if f.kind == "extrude_profile":
            solid = _extrude_profile(f.params)
        elif f.kind == "base_block":
            p = f.params
            ox, oy, oz = p.get("origin", [0, 0, 0])
            solid = Part.makeBox(p["width"], p["depth"], p["height"],
                                 App.Vector(ox, oy, oz))
        elif f.kind == "hole" and solid is not None:
            cyl = _make_hole_cylinder(f.params)
            if cyl is not None:
                try:
                    solid = solid.cut(cyl)
                except Exception as exc:
                    warnings.append(
                        f"hole cut failed for {f.params}: {exc}"
                    )
            else:
                warnings.append(f"hole cylinder could not be created: {f.params}")

    if solid is None:
        raise RuntimeError("no solid was produced from inferred features")

    fc_path = os.path.join(out_dir, f"{base_name}.FCStd")
    obj = doc.addObject("Part::Feature", "Result")
    obj.Shape = solid

    # Embed the original 2D three-view drawings as named edge compounds.
    if projected is not None:
        _add_2d_views(doc, projected)

    doc.recompute()
    doc.saveAs(fc_path)
    App.closeDocument(base_name)
    return {"fcstd": fc_path, "warnings": warnings}


# ---------------------------------------------------------------------------
# 2-D view embedding
# ---------------------------------------------------------------------------

# Lift functions: (u, v) in normalised view coords -> 3-D App.Vector.
# Must match the convention in projection_mapper.py.
_LIFT_FN = {
    "XZ": lambda App, u, v: App.Vector(float(u), 0.0, float(v)),   # front
    "XY": lambda App, u, v: App.Vector(float(u), float(v), 0.0),   # top
    "YZ": lambda App, u, v: App.Vector(0.0, float(u), float(v)),   # right
}

# Normal axis of each sketch plane (used for circles).
_PLANE_AXIS = {
    "XZ": (0, 1, 0),
    "XY": (0, 0, 1),
    "YZ": (1, 0, 0),
}


def _add_2d_views(doc, projected: Dict[str, Any]) -> None:
    """Add DXF_FRONT / DXF_TOP / DXF_RIGHT as Part::Feature edge compounds.

    Each object lives in the FreeCAD model tree alongside the solid.  The
    entities are already normalised to origin-relative 2-D coords by
    ``projection_mapper.map_views_to_3d``; here they are simply lifted into
    their respective 3-D planes.
    """
    import FreeCAD as App
    import Part

    for view_name, pv in projected.items():
        plane = pv.plane
        lift_fn = _LIFT_FN.get(plane)
        ax_xyz = _PLANE_AXIS.get(plane)
        if lift_fn is None or ax_xyz is None:
            continue

        lift = lambda u, v, _App=App, _fn=lift_fn: _fn(_App, u, v)
        axis = App.Vector(*ax_xyz)

        fc_edges = _entities_to_fc_edges(pv.entities, lift, axis)
        if not fc_edges:
            continue

        compound = Part.Compound(fc_edges)
        label = f"DXF_{view_name.upper()}"
        obj = doc.addObject("Part::Feature", label)
        obj.Shape = compound


def _entities_to_fc_edges(entities, lift, axis) -> List:
    """Convert DxfEntity list to a flat list of FreeCAD edge shapes."""
    import Part

    fc_edges: List = []
    for e in entities:
        kind = e.kind
        if kind == "LINE" and len(e.points) >= 2:
            a = lift(*e.points[0])
            b = lift(*e.points[1])
            if a.distanceToPoint(b) < 1e-9:
                continue
            try:
                fc_edges.append(Part.LineSegment(a, b).toShape())
            except Exception:
                pass

        elif kind == "CIRCLE" and e.center is not None and e.radius is not None:
            c3d = lift(*e.center)
            try:
                fc_edges.append(
                    Part.Circle(c3d, axis, float(e.radius)).toShape()
                )
            except Exception:
                pass

        elif kind == "ARC" and e.center is not None and e.radius is not None:
            cx, cy = e.center
            r = float(e.radius)
            sa = math.radians(float(e.start_angle or 0.0))
            ea = math.radians(float(e.end_angle or 0.0))
            if ea < sa:
                ea += 2 * math.pi
            mid = (sa + ea) * 0.5
            sp = lift(cx + r * math.cos(sa),  cy + r * math.sin(sa))
            mp = lift(cx + r * math.cos(mid), cy + r * math.sin(mid))
            ep = lift(cx + r * math.cos(ea),  cy + r * math.sin(ea))
            try:
                fc_edges.append(Part.Arc(sp, mp, ep).toShape())
            except Exception:
                try:
                    fc_edges.append(Part.LineSegment(sp, ep).toShape())
                except Exception:
                    pass

        elif kind in ("LWPOLYLINE", "POLYLINE") and len(e.points) >= 2:
            pts = e.points
            closed = bool(e.extra.get("closed", False))
            n = len(pts)
            end = n if closed else n - 1
            for i in range(end):
                a = lift(*pts[i])
                b = lift(*pts[(i + 1) % n])
                if a.distanceToPoint(b) < 1e-9:
                    continue
                try:
                    fc_edges.append(Part.LineSegment(a, b).toShape())
                except Exception:
                    pass

    return fc_edges


def _extrude_profile(params: Dict):
    """Build a wire from edges in the given sketch plane and extrude it
    along the perpendicular axis by `depth`.

    plane: "XY" -> extrude along +Z (drawing x/y -> world X/Y)
           "XZ" -> extrude along +Y (drawing x/y -> world X/Z)
           "YZ" -> extrude along +X (drawing x/y -> world Y/Z)
    """
    import FreeCAD as App
    import Part

    edges_def = params["edges"]
    depth = float(params["depth"])
    plane = params.get("plane", "XZ")

    if plane == "XY":
        def lift(u, v):
            return App.Vector(float(u), float(v), 0.0)
        extrude_vec = App.Vector(0.0, 0.0, depth)
    elif plane == "XZ":
        def lift(u, v):
            return App.Vector(float(u), 0.0, float(v))
        extrude_vec = App.Vector(0.0, depth, 0.0)
    elif plane == "YZ":
        def lift(u, v):
            return App.Vector(0.0, float(u), float(v))
        extrude_vec = App.Vector(depth, 0.0, 0.0)
    else:
        raise ValueError(f"unknown plane {plane!r}")

    fc_edges = []
    for e in edges_def:
        if e["kind"] == "LINE":
            x0, y0 = e["p0"]
            x1, y1 = e["p1"]
            a = lift(x0, y0)
            b = lift(x1, y1)
            if a.distanceToPoint(b) < 1e-9:
                continue
            fc_edges.append(Part.LineSegment(a, b).toShape())
        elif e["kind"] == "ARC":
            cx, cy = e["center"]
            r = float(e["radius"])
            sa = math.radians(float(e["start_angle"] or 0.0))
            ea = math.radians(float(e["end_angle"] or 0.0))
            if ea < sa:
                ea += 2 * math.pi
            mid = (sa + ea) * 0.5
            sp = lift(cx + r * math.cos(sa), cy + r * math.sin(sa))
            mp = lift(cx + r * math.cos(mid), cy + r * math.sin(mid))
            ep = lift(cx + r * math.cos(ea), cy + r * math.sin(ea))
            try:
                fc_edges.append(Part.Arc(sp, mp, ep).toShape())
            except Exception:
                fc_edges.append(Part.LineSegment(sp, ep).toShape())

    if not fc_edges:
        return None

    try:
        wire = Part.Wire(fc_edges)
    except Exception:
        wire = Part.Wire(Part.__sortEdges__(fc_edges))

    if not wire.isClosed():
        try:
            v0 = wire.OrderedVertexes[0].Point
            v1 = wire.OrderedVertexes[-1].Point
            if v0.distanceToPoint(v1) > 1e-9:
                closing = Part.LineSegment(v1, v0).toShape()
                wire = Part.Wire(fc_edges + [closing])
        except Exception:
            pass

    face = Part.Face(wire)
    solid = face.extrude(extrude_vec)
    return solid


def _make_hole_cylinder(params: Dict):
    import FreeCAD as App
    import Part

    r = float(params["radius"])
    axis = params["axis"]
    x, y, z = params["position"]
    length = float(params["through_length"])

    if axis == "Z":
        return Part.makeCylinder(r, length,
                                 App.Vector(x, y, z), App.Vector(0, 0, 1))
    if axis == "Y":
        return Part.makeCylinder(r, length,
                                 App.Vector(x, y, z), App.Vector(0, 1, 0))
    if axis == "X":
        return Part.makeCylinder(r, length,
                                 App.Vector(x, y, z), App.Vector(1, 0, 0))
    return None
