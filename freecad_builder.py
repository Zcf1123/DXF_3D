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
        elif f.kind == "sphere":
            p = f.params
            x, y, z = p.get("center", [0, 0, 0])
            solid = Part.makeSphere(float(p["radius"]),
                                    App.Vector(float(x), float(y), float(z)))
        elif f.kind == "cylinder_stack":
            solid = _make_cylinder_stack(f.params)
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
        elif f.kind == "profile_cut" and solid is not None:
            cutter = _extrude_profile(f.params)
            if cutter is not None:
                try:
                    solid = solid.cut(cutter)
                except Exception as exc:
                    warnings.append(
                        f"profile cut failed for {f.params}: {exc}"
                    )
            else:
                warnings.append(f"profile cutter could not be created: {f.params}")
        elif f.kind == "edge_chamfer" and solid is not None:
            try:
                chamfered = _apply_edge_chamfer(solid, f.params)
                if chamfered is not None:
                    solid = chamfered
                else:
                    warnings.append(f"no chamfer edges matched: {f.params}")
            except Exception as exc:
                warnings.append(f"edge chamfer failed for {f.params}: {exc}")

    if solid is None:
        raise RuntimeError("no solid was produced from inferred features")

    fc_path = os.path.join(out_dir, f"{base_name}.FCStd")
    obj = doc.addObject("Part::Feature", "Result")
    obj.Shape = solid

    # Embed the original 2D three-view drawings as named edge compounds.
    if projected is not None:
        _add_2d_views(doc, projected)
        if _is_single_view_extrude(features):
            _add_model_projection_views(doc, solid, set(projected.keys()))

    doc.recompute()
    doc.saveAs(fc_path)
    App.closeDocument(doc.Name)
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


def _is_single_view_extrude(features: List[Feature]) -> bool:
    return any(
        f.kind == "extrude_profile"
        and f.params.get("single_view_extrude") is True
        for f in features
    )


def _add_model_projection_views(doc, solid, existing: set) -> None:
    """Add generated FRONT/TOP/RIGHT view linework from the final solid.

    Single-view inputs only contain TOP linework, but downstream inspection is
    easier when the FCStd also contains generated orthographic projections.
    Existing input views are left untouched.
    """
    import FreeCAD as App
    import Part

    def project(point, view_name: str):
        if view_name == "front":
            return float(point.x), float(point.z)
        if view_name == "top":
            return float(point.x), float(point.y)
        return float(point.y), float(point.z)

    def lift_for(view_name: str, u: float, v: float):
        if view_name == "front":
            return App.Vector(u, 0.0, v)
        if view_name == "top":
            return App.Vector(u, v, 0.0)
        return App.Vector(0.0, u, v)

    for view_name in ("front", "top", "right"):
        if view_name in existing:
            continue
        edges = []
        for edge in solid.Edges:
            try:
                pts = edge.discretize(Deflection=0.5)
            except Exception:
                pts = [vertex.Point for vertex in edge.Vertexes]
            if len(pts) < 2:
                continue
            for a, b in zip(pts, pts[1:]):
                u0, v0 = project(a, view_name)
                u1, v1 = project(b, view_name)
                p0 = lift_for(view_name, u0, v0)
                p1 = lift_for(view_name, u1, v1)
                if p0.distanceToPoint(p1) < 1e-7:
                    continue
                try:
                    edges.append(Part.LineSegment(p0, p1).toShape())
                except Exception:
                    pass
        if edges:
            obj = doc.addObject("Part::Feature", f"DXF_{view_name.upper()}_GENERATED")
            obj.Shape = Part.Compound(edges)


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
    offset = float(params.get("offset", 0.0) or 0.0)

    if plane == "XY":
        def lift(u, v):
            return App.Vector(float(u), float(v), offset)
        extrude_vec = App.Vector(0.0, 0.0, depth)
    elif plane == "XZ":
        def lift(u, v):
            return App.Vector(float(u), offset, float(v))
        extrude_vec = App.Vector(0.0, depth, 0.0)
    elif plane == "YZ":
        def lift(u, v):
            return App.Vector(offset, float(u), float(v))
        extrude_vec = App.Vector(depth, 0.0, 0.0)
    else:
        raise ValueError(f"unknown plane {plane!r}")

    fc_edges = []
    for e in edges_def:
        if e["kind"] == "CIRCLE":
            cx, cy = e["center"]
            c3d = lift(float(cx), float(cy))
            axis = App.Vector(*_PLANE_AXIS.get(plane, (0, 0, 1)))
            wire = Part.Wire(Part.Circle(c3d, axis, float(e["radius"])).toShape())
            face = Part.Face(wire)
            return face.extrude(extrude_vec)
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
            if e.get("clockwise"):
                if ea > sa:
                    ea -= 2 * math.pi
            elif ea < sa:
                ea += 2 * math.pi
            mid = (sa + ea) * 0.5
            x0, y0 = e.get("p0", [cx + r * math.cos(sa), cy + r * math.sin(sa)])
            x1, y1 = e.get("p1", [cx + r * math.cos(ea), cy + r * math.sin(ea)])
            sp = lift(x0, y0)
            mp = lift(cx + r * math.cos(mid), cy + r * math.sin(mid))
            ep = lift(x1, y1)
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
        cyl = Part.makeCylinder(r, length,
                                App.Vector(x, y, z), App.Vector(0, 0, 1))
        cyl.rotate(App.Vector(x, y, z), App.Vector(0, 0, 1), 180.0)
        return cyl
    if axis == "Y":
        cyl = Part.makeCylinder(r, length,
                                App.Vector(x, y, z), App.Vector(0, 1, 0))
        cyl.rotate(App.Vector(x, y, z), App.Vector(0, 1, 0), 180.0)
        return cyl
    if axis == "X":
        cyl = Part.makeCylinder(r, length,
                                App.Vector(x, y, z), App.Vector(1, 0, 0))
        cyl.rotate(App.Vector(x, y, z), App.Vector(1, 0, 0), 180.0)
        return cyl
    return None


def _make_cylinder_stack(params: Dict):
    import FreeCAD as App
    import Part

    axis = params.get("axis", "Z")
    if axis != "Z":
        return None
    cx, cy = params.get("center", [0.0, 0.0])
    solids = []
    for segment in params.get("segments", []):
        z_min = float(segment.get("z_min", 0.0))
        z_max = float(segment.get("z_max", z_min))
        radius = float(segment.get("radius", 0.0))
        height = z_max - z_min
        if radius <= 0 or height <= 0:
            continue
        solids.append(Part.makeCylinder(
            radius,
            height,
            App.Vector(float(cx), float(cy), z_min),
            App.Vector(0, 0, 1),
        ))
    if not solids:
        return None
    solid = solids[0]
    for item in solids[1:]:
        solid = solid.fuse(item)
    try:
        return solid.removeSplitter()
    except Exception:
        return solid


def _apply_edge_chamfer(solid, params: Dict):
    distance = float(params.get("distance", 0.0) or 0.0)
    if distance <= 0:
        return None
    scope = params.get("scope", "outer_z_edges")
    if scope != "outer_z_edges":
        return None

    if params.get("profile") == "arc_revolve":
        return _apply_revolved_arc_envelope(solid, params)

    bb = solid.BoundBox
    z_min = float(bb.ZMin)
    z_max = float(bb.ZMax)
    scale = max(float(bb.XLength), float(bb.YLength), float(bb.ZLength), 1.0)
    tol = max(scale * 1e-7, 1e-6)
    profile = params.get("profile", "line")

    edges = []
    for edge in solid.Edges:
        edge_bb = edge.BoundBox
        same_end_face = (
            abs(float(edge_bb.ZMin) - z_min) <= tol
            and abs(float(edge_bb.ZMax) - z_min) <= tol
        ) or (
            abs(float(edge_bb.ZMin) - z_max) <= tol
            and abs(float(edge_bb.ZMax) - z_max) <= tol
        )
        if not same_end_face:
            continue
        if float(edge.Length) <= tol:
            continue
        if profile == "arc":
            edges.append(edge)
            continue
        if len(edge.Vertexes) != 2:
            continue
        p0 = edge.Vertexes[0].Point
        p1 = edge.Vertexes[1].Point
        chord = p0.distanceToPoint(p1)
        if chord > tol and abs(float(edge.Length) - chord) <= max(tol, chord * 1e-6):
            edges.append(edge)

    if not edges:
        return None
    if profile == "arc":
        return solid.makeFillet(distance, edges)
    return solid.makeChamfer(distance, edges)


def _apply_revolved_arc_envelope(solid, params: Dict):
    import FreeCAD as App
    import Part

    distance = float(params.get("distance", 0.0) or 0.0)
    top_radius = float(params.get("top_radius", 0.0) or 0.0)
    if distance <= 0 or top_radius <= 0:
        return None

    bb = solid.BoundBox
    height = float(bb.ZLength)
    if height <= 0 or distance >= height * 0.5:
        return None
    center_x = (float(bb.XMin) + float(bb.XMax)) * 0.5
    center_y = (float(bb.YMin) + float(bb.YMax)) * 0.5
    outer_radius = max(float(bb.XLength), float(bb.YLength)) * 0.5
    if top_radius >= outer_radius:
        return None

    z0 = 0.0
    z1 = distance
    z2 = height - distance
    z3 = height
    r0 = 0.0
    r1 = top_radius
    r2 = outer_radius

    bottom_mid = App.Vector((r1 + r2) * 0.5, 0.0, z1 * 0.35)
    top_mid = App.Vector((r1 + r2) * 0.5, 0.0, z3 - z1 * 0.35)

    edges = [
        Part.LineSegment(App.Vector(r0, 0.0, z0), App.Vector(r1, 0.0, z0)).toShape(),
        Part.Arc(App.Vector(r1, 0.0, z0), bottom_mid, App.Vector(r2, 0.0, z1)).toShape(),
        Part.LineSegment(App.Vector(r2, 0.0, z1), App.Vector(r2, 0.0, z2)).toShape(),
        Part.Arc(App.Vector(r2, 0.0, z2), top_mid, App.Vector(r1, 0.0, z3)).toShape(),
        Part.LineSegment(App.Vector(r1, 0.0, z3), App.Vector(r0, 0.0, z3)).toShape(),
        Part.LineSegment(App.Vector(r0, 0.0, z3), App.Vector(r0, 0.0, z0)).toShape(),
    ]
    face = Part.Face(Part.Wire(edges))
    envelope = face.revolve(
        App.Vector(0.0, 0.0, 0.0),
        App.Vector(0.0, 0.0, 1.0),
        360.0,
    )
    if not envelope.Solids and envelope.Shells:
        envelope = Part.Solid(envelope.Shells[0])
    envelope.rotate(
        App.Vector(0.0, 0.0, 0.0),
        App.Vector(0.0, 0.0, 1.0),
        30.0,
    )
    envelope.translate(App.Vector(center_x, center_y, float(bb.ZMin)))
    result = solid.common(envelope)
    return result.removeSplitter()
