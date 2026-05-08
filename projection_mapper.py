"""Map 2D view bundles into a unified 3D coordinate frame.

Convention (third-angle projection, Z-up):
    front view -> XZ plane    (drawing x -> world X, drawing y -> world Z)
    top view   -> XY plane    (drawing x -> world X, drawing y -> world Y)
    right view -> YZ plane    (drawing x -> world Y, drawing y -> world Z)

Each view is first translated so that the bottom-left of its bbox sits at
the origin in its 2D coordinate frame. The mapping then lifts entities
into 3D.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .dxf_loader import DxfEntity
from .view_classifier import ViewBundle


@dataclass
class ProjectedView:
    name: str
    plane: str                                 # "XZ" | "XY" | "YZ"
    origin_2d: Tuple[float, float]             # bbox min that was subtracted
    width: float
    height: float
    entities: List[DxfEntity] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "plane": self.plane,
            "origin_2d": list(self.origin_2d),
            "width": self.width,
            "height": self.height,
            "entity_count": len(self.entities),
        }


PLANE_FOR = {
    "front": "XZ",
    "top":   "XY",
    "right": "YZ",
}


def map_views_to_3d(bundles: List[ViewBundle]) -> Dict[str, ProjectedView]:
    """Return a dict keyed by canonical view name."""
    out: Dict[str, ProjectedView] = {}
    for b in bundles:
        if b.name not in PLANE_FOR:
            continue
        ox, oy, *_ = b.bbox
        normalized = [_translate_entity(e, -ox, -oy) for e in b.entities]
        out[b.name] = ProjectedView(
            name=b.name,
            plane=PLANE_FOR[b.name],
            origin_2d=(ox, oy),
            width=b.width,
            height=b.height,
            entities=normalized,
        )
    return out


def _translate_entity(e: DxfEntity, dx: float, dy: float) -> DxfEntity:
    new_points = [(p[0] + dx, p[1] + dy) for p in e.points]
    new_center = None
    if e.center is not None:
        new_center = (e.center[0] + dx, e.center[1] + dy)
    return DxfEntity(
        kind=e.kind, layer=e.layer, linetype=e.linetype,
        points=new_points,
        center=new_center,
        radius=e.radius,
        start_angle=e.start_angle,
        end_angle=e.end_angle,
        text=e.text,
        dim_type=e.dim_type,
        dim_text=e.dim_text,
        dim_measurement=e.dim_measurement,
        block_name=e.block_name,
        extra=dict(e.extra),
    )


def lift_2d_to_3d(plane: str, x: float, y: float,
                  depth: float = 0.0) -> Tuple[float, float, float]:
    """Map a single 2D point in the given view plane to a 3D point."""
    if plane == "XZ":   # front
        return (x, depth, y)
    if plane == "XY":   # top
        return (x, y, depth)
    if plane == "YZ":   # right
        return (depth, x, y)
    raise ValueError(f"Unknown plane: {plane}")
