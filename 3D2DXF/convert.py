#!/usr/bin/env python3
"""
3D → DXF 三视图转换器

支持格式: STEP (.step/.stp), IGES (.iges/.igs), STL (.stl), OBJ (.obj)

用法（通过 run.sh 调用，或直接）:
    freecadcmd -c "import sys; sys.path.insert(0,'/work'); import convert; sys.exit(convert.main(None))"
    freecadcmd -c "import sys; sys.path.insert(0,'/work'); import convert; sys.exit(convert.main(['/work/3d/model.step']))"
"""

import sys
import os
import math
import glob


# ── DXF R12 写入 ──────────────────────────────────────────────────────────────

def _f(v):
    return f"{float(v):.6f}"


class DXFWriter:
    def __init__(self):
        self._ents = []

    def line(self, x1, y1, x2, y2, layer="0", lt=""):
        self._ents.append(dict(type="LINE", layer=layer, lt=lt,
                               x1=x1, y1=y1, x2=x2, y2=y2))

    def circle(self, cx, cy, r, layer="0", lt=""):
        self._ents.append(dict(type="CIRCLE", layer=layer, lt=lt,
                               cx=cx, cy=cy, r=r))

    def arc(self, cx, cy, r, a0, a1, layer="0", lt=""):
        self._ents.append(dict(type="ARC", layer=layer, lt=lt,
                               cx=cx, cy=cy, r=r, a0=a0, a1=a1))

    def write(self, path):
        rows = []

        # HEADER
        rows += ["0", "SECTION", "2", "HEADER",
                 "9", "$ACADVER", "1", "AC1009",
                 "0", "ENDSEC"]

        # TABLES
        rows += ["0", "SECTION", "2", "TABLES"]

        # LTYPE 表
        rows += ["0", "TABLE", "2", "LTYPE", "70", "3"]
        rows += ["0", "LTYPE", "2", "CONTINUOUS", "70", "0",
                 "3", "Solid line", "72", "65", "73", "0", "40", "0.0"]
        rows += ["0", "LTYPE", "2", "HIDDEN", "70", "0",
                 "3", "__ __ __ __ __", "72", "65", "73", "2", "40", "9.0",
                 "49", "6.0", "49", "-3.0"]
        rows += ["0", "LTYPE", "2", "CENTER", "70", "0",
                 "3", "___ _ ___ _ ___", "72", "65", "73", "4", "40", "21.0",
                 "49", "12.0", "49", "-3.0", "49", "3.0", "49", "-3.0"]
        rows += ["0", "ENDTAB"]

        # LAYER 表
        layers = sorted({e["layer"] for e in self._ents} | {"0"})
        rows += ["0", "TABLE", "2", "LAYER", "70", str(len(layers))]
        for lname in layers:
            if "HID" in lname:
                lt_for_layer = "HIDDEN"
            else:
                lt_for_layer = "CONTINUOUS"
            rows += ["0", "LAYER", "2", lname, "70", "0", "62", "7", "6", lt_for_layer]
        rows += ["0", "ENDTAB"]

        rows += ["0", "ENDSEC"]

        # ENTITIES
        rows += ["0", "SECTION", "2", "ENTITIES"]
        for e in self._ents:
            t, la, lt = e["type"], e["layer"], e["lt"]
            rows += ["0", t, "8", la]
            if lt:
                rows += ["6", lt]
            if t == "LINE":
                rows += ["10", _f(e["x1"]), "20", _f(e["y1"]), "30", "0.0",
                         "11", _f(e["x2"]), "21", _f(e["y2"]), "31", "0.0"]
            elif t == "CIRCLE":
                rows += ["10", _f(e["cx"]), "20", _f(e["cy"]), "30", "0.0",
                         "40", _f(e["r"])]
            elif t == "ARC":
                rows += ["10", _f(e["cx"]), "20", _f(e["cy"]), "30", "0.0",
                         "40", _f(e["r"]),
                         "50", _f(e["a0"]), "51", _f(e["a1"])]

        rows += ["0", "ENDSEC", "0", "EOF"]

        with open(path, "w") as f:
            f.write("\n".join(rows) + "\n")


# ── 几何投影 ──────────────────────────────────────────────────────────────────

def edge_to_ents(edge, ax, ay, sx=1.0, sy=1.0, layer="0", lt=""):
    """
    把 FreeCAD 投影边（TechDraw.projectEx 返回，均在 Z=0 平面）转为 DXF 实体。
    ax, ay : 从投影点读取的轴索引（0=x,1=y）；front/right 用 ax=1,ay=0；top 用 ax=0,ay=1
    sx, sy : 方向缩放（-1 翻转）
    """
    import Part

    def pt2d(p):
        return float(p[ax]) * sx, float(p[ay]) * sy

    result = []
    curve = edge.Curve

    try:
        # TechDraw.projectEx 对直线也返回 BSplineCurve，统一离散化处理。
        # 对近似直线使用 2 点，对曲线使用 64 点以保证圆弧精度。
        pts_3d = []
        try:
            pts_3d = edge.discretize(64)
        except Exception:
            n = 64
            for i in range(n + 1):
                t = edge.FirstParameter + (edge.LastParameter - edge.FirstParameter) * i / n
                pts_3d.append(edge.valueAt(t))

        pts = [pt2d(p) for p in pts_3d]

        # 若所有点共线（且非闭合曲线），折叠为一条直线
        if len(pts) >= 2:
            x0, y0 = pts[0]
            xe, ye = pts[-1]
            is_closed = abs(xe - x0) < 1e-9 and abs(ye - y0) < 1e-9
            if not is_closed:
                collinear = all(
                    abs((ye - y0) * (px - x0) - (xe - x0) * (py - y0)) < 1e-6
                    for px, py in pts
                )
                if collinear:
                    result.append(dict(type="LINE", layer=layer, lt=lt,
                                       x1=x0, y1=y0, x2=xe, y2=ye))
                    return result

        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            if abs(x1 - x2) > 1e-9 or abs(y1 - y2) > 1e-9:
                result.append(dict(type="LINE", layer=layer, lt=lt,
                                   x1=x1, y1=y1, x2=x2, y2=y2))

    except Exception as ex:
        print(f"  [warn] edge_to_ents: {ex}")

    return result


def project_view(shape, direction, ax, ay, sx=1.0, sy=1.0,
                 lvis="0", lhid="HIDDEN"):
    """
    沿 direction 方向投影形体，返回实体字典列表（包含可见线和隐藏线）。
    result[0..4]:  可见边 (hard/smooth/sewn/outline/iso)
    result[5..9]:  隐藏边 (hard/smooth/sewn/outline/iso)

    TechDraw.projectEx 返回的投影边均在 Z=0 平面：
      - 沿 +Y 投影（front）: result.x=worldZ, result.y=worldX  → ax=1, ay=0
      - 沿 +Z 投影（top）  : result.x=worldX, result.y=worldY  → ax=0, ay=1
      - 沿 +X 投影（right）: result.x=worldZ, result.y=worldY  → ax=1, ay=0
    """
    import FreeCAD
    import TechDraw

    try:
        result = TechDraw.projectEx(shape, FreeCAD.Vector(*direction))
    except Exception as e:
        print(f"  [warn] projectEx({direction}) failed: {e}")
        return []

    ents = []
    for compound in result[:4]:
        try:
            for edge in compound.Edges:
                ents += edge_to_ents(edge, ax, ay, sx, sy, layer=lvis, lt="")
        except Exception:
            pass
    for compound in result[4:]:
        try:
            for edge in compound.Edges:
                ents += edge_to_ents(edge, ax, ay, sx, sy, layer=lhid, lt="HIDDEN")
        except Exception:
            pass

    return ents


def bbox_of_ents(ents):
    """返回实体列表的 (xmin, ymin, xmax, ymax)。"""
    xs, ys = [], []
    for e in ents:
        if e["type"] == "LINE":
            xs += [e["x1"], e["x2"]]
            ys += [e["y1"], e["y2"]]
        elif e["type"] in ("ARC", "CIRCLE"):
            r = e["r"]
            xs += [e["cx"] - r, e["cx"] + r]
            ys += [e["cy"] - r, e["cy"] + r]
    if not xs:
        return 0.0, 0.0, 0.0, 0.0
    return min(xs), min(ys), max(xs), max(ys)


def shift_ents(ents, dx, dy):
    """平移所有实体。"""
    result = []
    for e in ents:
        e = dict(e)
        if e["type"] == "LINE":
            e["x1"] += dx; e["y1"] += dy
            e["x2"] += dx; e["y2"] += dy
        else:
            e["cx"] += dx; e["cy"] += dy
        result.append(e)
    return result


def normalize(ents):
    """将视图平移使左下角对齐原点，返回 (normalized_ents, width, height)。"""
    if not ents:
        return ents, 0.0, 0.0
    x0, y0, x1, y1 = bbox_of_ents(ents)
    return shift_ents(ents, -x0, -y0), x1 - x0, y1 - y0


# ── 加载 3D 文件 ──────────────────────────────────────────────────────────────

def load_shape(path):
    import Part
    ext = os.path.splitext(path)[1].lower()

    if ext in ('.step', '.stp', '.iges', '.igs'):
        shape = Part.read(path)

    elif ext == '.stl':
        import Mesh
        mesh = Mesh.Mesh(path)
        shape = Part.Shape()
        shape.makeShapeFromMesh(mesh.Topology, 0.1)
        shape = shape.removeSplitter()

    elif ext == '.obj':
        import Mesh
        mesh = Mesh.Mesh()
        mesh.read(path)
        shape = Part.Shape()
        shape.makeShapeFromMesh(mesh.Topology, 0.1)
        shape = shape.removeSplitter()

    else:
        raise ValueError(
            f"不支持的格式: {ext}，支持 .step/.stp/.iges/.igs/.stl/.obj"
        )

    return shape


# ── 主转换函数 ────────────────────────────────────────────────────────────────

def convert(input_path, output_path):
    """将单个 3D 文件转换为 DXF 三视图。"""
    print(f"加载: {input_path}")
    shape = load_shape(input_path)

    bb = shape.BoundBox
    W = bb.XMax - bb.XMin   # X 方向宽度
    D = bb.YMax - bb.YMin   # Y 方向深度
    H = bb.ZMax - bb.ZMin   # Z 方向高度
    print(f"  包围盒  W={W:.2f}  D={D:.2f}  H={H:.2f}")

    # 三视图投影
    # TechDraw.projectEx 投影结果均在 Z=0 平面：
    #   沿 +Y（front）: result.x=worldZ, result.y=worldX → ax=1,ay=0
    #   沿 +Z（top）  : result.x=worldX, result.y=worldY → ax=0,ay=1
    #   沿 +X（right）: result.x=worldZ, result.y=worldY → ax=1,ay=0
    print("  投影主视图 (front) ...")
    front = project_view(shape, (0, 1, 0), ax=1, ay=0,
                         lvis="FRONT", lhid="FRONT_HID")

    print("  投影俯视图 (top) ...")
    top = project_view(shape, (0, 0, 1), ax=0, ay=1,
                       lvis="TOP", lhid="TOP_HID")

    print("  投影右视图 (right) ...")
    right = project_view(shape, (1, 0, 0), ax=1, ay=0,
                         lvis="RIGHT", lhid="RIGHT_HID")

    # 各视图归零到左下角原点
    front, fw, fh = normalize(front)
    top,   tw, th = normalize(top)
    right, rw, rh = normalize(right)

    # 视图间距：视图最大尺寸的 20%，至少 1 单位
    max_dim = max(fw, fh, tw, th, rw, rh, 1e-6)
    GAP = max(max_dim * 0.2, 1e-3)
    print(f"  视图尺寸 front={fw:.3f}x{fh:.3f}  top={tw:.3f}x{th:.3f}  right={rw:.3f}x{rh:.3f}  GAP={GAP:.3f}")

    # 布局（第一角投影，与本项目 FRONT/RIGHT/TOP 约定一致）：
    #   FRONT（左上）  RIGHT（右上）
    #   TOP  （左下）
    all_ents = (
        shift_ents(front, 0,          th + GAP) +
        shift_ents(right, fw + GAP,   th + GAP) +
        shift_ents(top,   0,          0)
    )

    dxf = DXFWriter()
    for e in all_ents:
        t, la, lt = e["type"], e["layer"], e["lt"]
        if t == "LINE":
            dxf.line(e["x1"], e["y1"], e["x2"], e["y2"], la, lt)
        elif t == "CIRCLE":
            dxf.circle(e["cx"], e["cy"], e["r"], la, lt)
        elif t == "ARC":
            dxf.arc(e["cx"], e["cy"], e["r"], e["a0"], e["a1"], la, lt)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    dxf.write(output_path)
    print(f"  输出: {output_path}")


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main(files=None):
    """
    files: list of input file paths, or None to process all files in ./3d/
    返回值: 错误数量（0 表示全部成功）
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_dir  = os.path.join(script_dir, "3d")
    output_dir = os.path.join(script_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    if files is None:
        exts = ("*.step", "*.stp", "*.iges", "*.igs", "*.stl", "*.obj")
        files = []
        for pat in exts:
            files += glob.glob(os.path.join(input_dir, pat))
        if not files:
            print(f"[3D2DXF] 3d/ 目录下没有找到支持的文件: {input_dir}")
            return 1

    errors = 0
    for fp in files:
        base = os.path.splitext(os.path.basename(fp))[0]
        out  = os.path.join(output_dir, base + ".dxf")
        try:
            convert(fp, out)
        except Exception as e:
            print(f"[3D2DXF] ERROR {fp}: {e}")
            import traceback
            traceback.print_exc()
            errors += 1

    return errors


if __name__ == "__main__":
    # 过滤掉 freecadcmd 注入的参数（以 -- 开头）
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(args if args else None))
