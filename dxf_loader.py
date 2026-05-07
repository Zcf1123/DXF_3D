"""DXF entity loader (pure Python, no external dependencies).

Extends the minimal text parser shipped in `cad_assistant.init_freecad`
without modifying it. Reads a DXF file and returns a flat list of
normalized entities along with some lightweight metadata.

Supported entities:
    LINE, CIRCLE, ARC, LWPOLYLINE, POLYLINE, ELLIPSE, SPLINE,
    TEXT, MTEXT, DIMENSION, INSERT (expanded), SOLID, HATCH (kind only)

Reads the BLOCKS section as well as ENTITIES, expands `INSERT`
references with translation / scale / rotation.  Anonymous `*Dn` blocks
(auto-generated dimension decoration: arrowheads, witness lines, text)
are intentionally **skipped** — they contain graphical decorations, not
part geometry.  Dimension measurements are read from the DIMENSION entity
``dim_measurement`` field instead.

This module never imports `ezdxf`.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass
class DxfEntity:
    kind: str                      # "LINE" | "CIRCLE" | "ARC" | "LWPOLYLINE" | "POLYLINE"
                                   # | "SPLINE" | "ELLIPSE" | "TEXT" | "MTEXT"
                                   # | "DIMENSION" | "INSERT" | "HATCH"
    layer: str = "0"
    points: List[Tuple[float, float]] = field(default_factory=list)
    center: Optional[Tuple[float, float]] = None
    radius: Optional[float] = None
    start_angle: Optional[float] = None     # degrees
    end_angle: Optional[float] = None       # degrees
    text: Optional[str] = None
    dim_type: Optional[int] = None
    dim_text: Optional[str] = None
    dim_measurement: Optional[float] = None
    block_name: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def bbox(self) -> Optional[Tuple[float, float, float, float]]:
        """Return (xmin, ymin, xmax, ymax) for this entity, or None."""
        xs: List[float] = []
        ys: List[float] = []
        if self.kind == "CIRCLE" and self.center and self.radius is not None:
            cx, cy = self.center
            r = self.radius
            return (cx - r, cy - r, cx + r, cy + r)
        if self.kind == "ARC" and self.center and self.radius is not None:
            cx, cy = self.center
            r = self.radius
            sa = math.radians(self.start_angle or 0.0)
            ea = math.radians(self.end_angle or 0.0)
            # Approximate via sampling
            ang = sa
            if ea < sa:
                ea += 2 * math.pi
            steps = 16
            for i in range(steps + 1):
                a = sa + (ea - sa) * i / steps
                xs.append(cx + r * math.cos(a))
                ys.append(cy + r * math.sin(a))
            return (min(xs), min(ys), max(xs), max(ys))
        if self.points:
            xs = [p[0] for p in self.points]
            ys = [p[1] for p in self.points]
            return (min(xs), min(ys), max(xs), max(ys))
        if self.center:
            cx, cy = self.center
            return (cx, cy, cx, cy)
        return None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["bbox"] = self.bbox()
        return d


def _try_import_ezdxf():  # kept for back-compat callers, always returns None
    return None


def load_dxf(path: str) -> Tuple[List[DxfEntity], Dict[str, Any]]:
    """Load a DXF file and return (entities, metadata).

    metadata keys:
      - path: source path
      - backend: "pure-python"
      - layers: list of layer names seen
      - units: $INSUNITS code as string if found
      - bbox: overall bounding box (xmin, ymin, xmax, ymax) or None
      - entity_count: number of returned entities
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    pairs = _read_pairs(path)
    sections = _split_sections(pairs)
    header_units = _header_value(sections.get("HEADER", []), "$INSUNITS")

    blocks = _parse_blocks(sections.get("BLOCKS", []))

    raw_entries: List[Tuple[str, Dict[str, Any]]] = list(
        _iter_entities(sections.get("ENTITIES", []))
    )
    raw_entries = _absorb_polyline_vertices(raw_entries)

    flat: List[Tuple[str, Dict[str, Any]]] = []
    for kind, params in raw_entries:
        if kind == "INSERT":
            flat.extend(_expand_insert(params, blocks))
        else:
            flat.append((kind, params))

    # `*Dn` blocks (e.g. *D0, *D1) hold the graphical decoration of
    # DIMENSION entities (arrowheads drawn as SOLID, witness lines as LINE,
    # value text as MTEXT/TEXT).  Including them as geometry corrupts view
    # clustering and outline extraction, so they are explicitly excluded.
    # Other anonymous blocks (e.g. symbols/titles) are still expanded.
    _DIM_BLOCK_RE = re.compile(r"^\*D\d+$")
    for name, ents in blocks.items():
        if name.startswith("*") and name not in {"*Model_Space", "*Paper_Space",
                                                  "*Paper_Space0"}:
            if _DIM_BLOCK_RE.match(name):
                continue  # dimension decoration – skip, values read from DIMENSION entity
            for kind, params in ents:
                if kind == "INSERT":
                    continue
                flat.append((kind, dict(params)))

    entities = [e for e in (_to_entity(k, p) for k, p in flat) if e is not None]

    layers = sorted({e.layer for e in entities})
    bbox = _overall_bbox(entities)

    meta: Dict[str, Any] = {
        "path": path,
        "backend": "pure-python",
        "layers": layers,
        "units": header_units,
        "bbox": bbox,
        "entity_count": len(entities),
    }
    return entities, meta


# ---------------------------------------------------------------------------
# Pure-Python DXF reader
# ---------------------------------------------------------------------------

def _read_pairs(path: str) -> List[Tuple[str, str]]:
    """Read a DXF file as a list of (group_code_str, value_str) pairs."""
    with open(path, "r", errors="replace") as f:
        raw = f.read()
    lines = [ln.strip() for ln in raw.replace("\r\n", "\n").split("\n")]
    if len(lines) % 2 == 1:
        lines.append("")
    return list(zip(lines[0::2], lines[1::2]))


def _split_sections(pairs: List[Tuple[str, str]]
                    ) -> Dict[str, List[Tuple[str, str]]]:
    """Split pairs into a {section_name: pairs_inside} dict."""
    sections: Dict[str, List[Tuple[str, str]]] = {}
    n = len(pairs)
    i = 0
    while i < n:
        code, value = pairs[i]
        if code == "0" and value == "SECTION":
            if i + 1 < n and pairs[i + 1][0] == "2":
                name = pairs[i + 1][1]
                j = i + 2
                buf: List[Tuple[str, str]] = []
                while j < n and not (pairs[j][0] == "0"
                                     and pairs[j][1] == "ENDSEC"):
                    buf.append(pairs[j])
                    j += 1
                sections[name] = buf
                i = j + 1
                continue
        i += 1
    return sections


def _header_value(header_pairs: List[Tuple[str, str]], var: str) -> Optional[str]:
    """Return the value following a `$VAR` header variable, if any."""
    for i, (code, value) in enumerate(header_pairs):
        if code == "9" and value == var and i + 1 < len(header_pairs):
            return header_pairs[i + 1][1]
    return None


def _iter_entities(pairs: List[Tuple[str, str]]
                   ) -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Yield (kind, params) for top-level entities in `pairs`.

    LWPOLYLINE vertices are accumulated into params["_points"].
    POLYLINE / VERTEX / SEQEND are emitted as separate entries and later
    absorbed by `_absorb_polyline_vertices`.
    MTEXT continuation strings (code 3) are concatenated into code 1.
    """
    current_kind: Optional[str] = None
    current: Dict[str, Any] = {}
    poly_points: List[Tuple[float, float]] = []
    in_lwpoly = False
    pending_x: Optional[float] = None
    mtext_buf: List[str] = []

    def commit():
        nonlocal current_kind, current, poly_points, in_lwpoly, pending_x
        nonlocal mtext_buf
        if current_kind is None:
            return None
        if in_lwpoly and poly_points:
            current["_points"] = list(poly_points)
        if current_kind == "MTEXT" and mtext_buf:
            joined = "".join(mtext_buf)
            current["1"] = (current.get("1", "") or "") + joined
        out = (current_kind, current)
        current_kind = None
        current = {}
        poly_points = []
        in_lwpoly = False
        pending_x = None
        mtext_buf = []
        return out

    for code, value in pairs:
        if code == "0":
            done = commit()
            if done is not None:
                yield done
            current_kind = value
            current = {}
            in_lwpoly = (value == "LWPOLYLINE")
            continue
        if current_kind is None:
            continue
        try:
            v: Any = float(value)
        except ValueError:
            v = value

        if in_lwpoly and code == "10":
            pending_x = float(v) if isinstance(v, (int, float)) else 0.0
            continue
        if in_lwpoly and code == "20" and pending_x is not None:
            y = float(v) if isinstance(v, (int, float)) else 0.0
            poly_points.append((pending_x, y))
            pending_x = None
            continue

        if current_kind == "MTEXT" and code == "3":
            mtext_buf.append(str(value))
            continue

        current[code] = v

    done = commit()
    if done is not None:
        yield done


def _absorb_polyline_vertices(entries: List[Tuple[str, Dict[str, Any]]]
                              ) -> List[Tuple[str, Dict[str, Any]]]:
    """Collapse [POLYLINE, VERTEX, ..., SEQEND] into a single POLYLINE
    entry with `_points`."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    i = 0
    while i < len(entries):
        kind, params = entries[i]
        if kind == "POLYLINE":
            pts: List[Tuple[float, float]] = []
            j = i + 1
            while j < len(entries) and entries[j][0] == "VERTEX":
                vp = entries[j][1]
                pts.append((float(vp.get("10", 0.0) or 0.0),
                            float(vp.get("20", 0.0) or 0.0)))
                j += 1
            params = dict(params)
            params["_points"] = pts
            out.append((kind, params))
            # Skip optional trailing SEQEND
            if j < len(entries) and entries[j][0] == "SEQEND":
                j += 1
            i = j
            continue
        if kind in ("VERTEX", "SEQEND"):
            i += 1
            continue
        out.append((kind, params))
        i += 1
    return out


# ---------------------------------------------------------------------------
# BLOCKS table + INSERT expansion
# ---------------------------------------------------------------------------

def _parse_blocks(pairs: List[Tuple[str, str]]
                  ) -> Dict[str, List[Tuple[str, Dict[str, Any]]]]:
    """Return {block_name: [entities]} from the BLOCKS section."""
    blocks: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}
    n = len(pairs)
    i = 0
    while i < n:
        code, value = pairs[i]
        if code == "0" and value == "BLOCK":
            block_name = ""
            j = i + 1
            # Read header until next code 0
            while j < n and pairs[j][0] != "0":
                if pairs[j][0] == "2" and not block_name:
                    block_name = pairs[j][1]
                j += 1
            # Collect body until ENDBLK
            body_start = j
            while j < n and not (pairs[j][0] == "0" and pairs[j][1] == "ENDBLK"):
                j += 1
            body = pairs[body_start:j]
            if block_name:
                ents = list(_iter_entities(body))
                ents = _absorb_polyline_vertices(ents)
                blocks[block_name] = ents
            i = j + 1
            continue
        i += 1
    return blocks


def _transform_xy(x: float, y: float, ix: float, iy: float,
                  sx: float, sy: float, rot_deg: float
                  ) -> Tuple[float, float]:
    a = math.radians(rot_deg or 0.0)
    cs, sn = math.cos(a), math.sin(a)
    px, py = x * sx, y * sy
    return (cs * px - sn * py + ix, sn * px + cs * py + iy)


def _expand_insert(params: Dict[str, Any],
                   blocks: Dict[str, List[Tuple[str, Dict[str, Any]]]]
                   ) -> List[Tuple[str, Dict[str, Any]]]:
    name = params.get("2")
    if not isinstance(name, str) or name not in blocks:
        return []
    ix = float(params.get("10", 0.0) or 0.0)
    iy = float(params.get("20", 0.0) or 0.0)
    sx = float(params.get("41", 1.0) or 1.0)
    sy = float(params.get("42", 1.0) or 1.0)
    rot = float(params.get("50", 0.0) or 0.0)

    out: List[Tuple[str, Dict[str, Any]]] = []
    for k, p in blocks[name]:
        if k == "INSERT":
            np = dict(p)
            ax, ay = _transform_xy(
                float(np.get("10", 0.0) or 0.0),
                float(np.get("20", 0.0) or 0.0),
                ix, iy, sx, sy, rot,
            )
            np["10"] = ax
            np["20"] = ay
            np["41"] = float(np.get("41", 1.0) or 1.0) * sx
            np["42"] = float(np.get("42", 1.0) or 1.0) * sy
            np["50"] = float(np.get("50", 0.0) or 0.0) + rot
            out.extend(_expand_insert(np, blocks))
            continue
        np = dict(p)
        if k == "LINE":
            x1, y1 = _transform_xy(float(np.get("10", 0.0)),
                                   float(np.get("20", 0.0)),
                                   ix, iy, sx, sy, rot)
            x2, y2 = _transform_xy(float(np.get("11", 0.0)),
                                   float(np.get("21", 0.0)),
                                   ix, iy, sx, sy, rot)
            np["10"], np["20"], np["11"], np["21"] = x1, y1, x2, y2
        elif k in ("CIRCLE", "ARC"):
            cx, cy = _transform_xy(float(np.get("10", 0.0)),
                                   float(np.get("20", 0.0)),
                                   ix, iy, sx, sy, rot)
            np["10"], np["20"] = cx, cy
            np["40"] = float(np.get("40", 0.0)) * (abs(sx) + abs(sy)) * 0.5
            if k == "ARC":
                np["50"] = float(np.get("50", 0.0)) + rot
                np["51"] = float(np.get("51", 0.0)) + rot
        elif k in ("LWPOLYLINE", "POLYLINE"):
            pts = np.get("_points") or []
            np["_points"] = [_transform_xy(p[0], p[1], ix, iy, sx, sy, rot)
                             for p in pts]
        elif k in ("TEXT", "MTEXT", "SOLID", "DIMENSION"):
            tx, ty = _transform_xy(float(np.get("10", 0.0) or 0.0),
                                   float(np.get("20", 0.0) or 0.0),
                                   ix, iy, sx, sy, rot)
            np["10"], np["20"] = tx, ty
        out.append((k, np))
    return out


# ---------------------------------------------------------------------------
# Convert raw (kind, params) -> DxfEntity
# ---------------------------------------------------------------------------

def _f(d: Dict[str, Any], code: str, default: float = 0.0) -> float:
    try:
        return float(d.get(code, default))
    except (TypeError, ValueError):
        return default


def _to_entity(kind: str, p: Dict[str, Any]) -> Optional[DxfEntity]:
    layer = str(p.get("8", "0") or "0")
    if kind == "LINE":
        return DxfEntity(
            kind="LINE", layer=layer,
            points=[(_f(p, "10"), _f(p, "20")),
                    (_f(p, "11"), _f(p, "21"))],
        )
    if kind == "CIRCLE":
        r = _f(p, "40")
        if r <= 0:
            return None
        return DxfEntity(
            kind="CIRCLE", layer=layer,
            center=(_f(p, "10"), _f(p, "20")), radius=r,
        )
    if kind == "ARC":
        r = _f(p, "40")
        if r <= 0:
            return None
        return DxfEntity(
            kind="ARC", layer=layer,
            center=(_f(p, "10"), _f(p, "20")), radius=r,
            start_angle=_f(p, "50"), end_angle=_f(p, "51"),
        )
    if kind in ("LWPOLYLINE", "POLYLINE"):
        pts = list(p.get("_points") or [])
        flag = int(_f(p, "70", 0.0))
        return DxfEntity(
            kind=kind, layer=layer, points=pts,
            extra={"closed": bool(flag & 1)},
        )
    if kind == "ELLIPSE":
        return DxfEntity(
            kind="ELLIPSE", layer=layer,
            center=(_f(p, "10"), _f(p, "20")),
            extra={
                "major_axis": (_f(p, "11"), _f(p, "21")),
                "ratio": _f(p, "40", 1.0),
                "start_param": _f(p, "41"),
                "end_param": _f(p, "42"),
            },
        )
    if kind == "SPLINE":
        return DxfEntity(kind="SPLINE", layer=layer)
    if kind in ("TEXT", "MTEXT"):
        text = p.get("1", "")
        if not isinstance(text, str):
            text = str(text)
        return DxfEntity(
            kind=kind, layer=layer,
            text=text,
            points=[(_f(p, "10"), _f(p, "20"))],
        )
    if kind == "DIMENSION":
        text = p.get("1", "")
        if not isinstance(text, str):
            text = str(text)
        meas = p.get("42")
        try:
            measurement = float(meas) if meas is not None else None
        except (TypeError, ValueError):
            measurement = None
        return DxfEntity(
            kind="DIMENSION", layer=layer,
            points=[(_f(p, "10"), _f(p, "20"))],
            dim_type=int(_f(p, "70", 0.0)),
            dim_text=text,
            dim_measurement=measurement,
            extra={"rotation": _f(p, "50")},
        )
    if kind == "INSERT":
        # Should already be expanded by _expand_insert, but keep raw if not.
        return DxfEntity(
            kind="INSERT", layer=layer,
            points=[(_f(p, "10"), _f(p, "20"))],
            block_name=str(p.get("2", "")),
            extra={
                "xscale": _f(p, "41", 1.0),
                "yscale": _f(p, "42", 1.0),
                "rotation": _f(p, "50"),
            },
        )
    if kind == "SOLID":
        return DxfEntity(
            kind="SOLID", layer=layer,
            points=[(_f(p, "10"), _f(p, "20")),
                    (_f(p, "11"), _f(p, "21")),
                    (_f(p, "12"), _f(p, "22")),
                    (_f(p, "13"), _f(p, "23"))],
        )
    if kind == "HATCH":
        return DxfEntity(kind="HATCH", layer=layer)
    return None


def _overall_bbox(entities: List[DxfEntity]):
    xs_min, ys_min, xs_max, ys_max = [], [], [], []
    for e in entities:
        b = e.bbox()
        if b is None:
            continue
        xs_min.append(b[0]); ys_min.append(b[1])
        xs_max.append(b[2]); ys_max.append(b[3])
    if not xs_min:
        return None
    return (min(xs_min), min(ys_min), max(xs_max), max(ys_max))
