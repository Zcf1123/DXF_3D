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

        # LTYPE 表：使用固定 R12 线型长度。
        # FreeCAD 的 DXF 导入器对极小的自定义线型 pattern 容错较差，
        # 因此不要按模型尺寸缩放这里的 40/49 数值。
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
    ax, ay : 从投影点读取的轴索引（0=x,1=y）；front/left 用 ax=1,ay=0；top 用 ax=0,ay=1
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


def _line_key(ent, tol=1e-6):
    """生成线段去重 key，忽略端点顺序。"""
    if ent.get("type") != "LINE":
        return None
    p1 = (round(ent["x1"] / tol), round(ent["y1"] / tol))
    p2 = (round(ent["x2"] / tol), round(ent["y2"] / tol))
    return tuple(sorted((p1, p2)))


def _line_length(ent):
    if ent.get("type") != "LINE":
        return 0.0
    return math.hypot(ent["x2"] - ent["x1"], ent["y2"] - ent["y1"])


def _line_fully_covered_by(ent, covers, tol=1e-6):
    """判断 ent 是否被 covers 中一条或多条共线线段完全覆盖。"""
    if ent.get("type") != "LINE":
        return False

    x1, y1, x2, y2 = ent["x1"], ent["y1"], ent["x2"], ent["y2"]
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length <= tol:
        return True

    intervals = []
    len2 = length * length
    for other in covers:
        if other.get("type") != "LINE":
            continue
        ox1, oy1 = other["x1"], other["y1"]
        ox2, oy2 = other["x2"], other["y2"]

        # other 的两个端点都必须落在 ent 所在直线上。
        d1 = abs(dx * (oy1 - y1) - dy * (ox1 - x1)) / length
        d2 = abs(dx * (oy2 - y1) - dy * (ox2 - x1)) / length
        if d1 > tol or d2 > tol:
            continue

        t1 = ((ox1 - x1) * dx + (oy1 - y1) * dy) / len2
        t2 = ((ox2 - x1) * dx + (oy2 - y1) * dy) / len2
        lo, hi = sorted((t1, t2))
        lo = max(0.0, lo)
        hi = min(1.0, hi)
        if hi - lo > tol / length:
            intervals.append((lo, hi))

    if not intervals:
        return False

    intervals.sort()
    cover_start, cover_end = intervals[0]
    if cover_start > tol / length:
        return False
    for lo, hi in intervals[1:]:
        if lo > cover_end + tol / length:
            break
        cover_end = max(cover_end, hi)
    return cover_end >= 1.0 - tol / length


def _remove_covered_lines(ents, tol=1e-6):
    """去掉被同组更长/已保留共线线段完全覆盖的 LINE。"""
    ordered = sorted(ents, key=_line_length, reverse=True)
    kept = []
    for ent in ordered:
        if not _line_fully_covered_by(ent, kept, tol):
            kept.append(ent)
    return kept


def project_view(shape, direction, ax, ay, sx=1.0, sy=1.0,
                 lvis="0", lhid="HIDDEN"):
    """
    沿 direction 方向投影形体，返回实体字典列表（包含可见线和隐藏线）。
    result[0..4]:  可见边 (hard/smooth/sewn/outline/iso)
    result[5..9]:  隐藏边 (hard/smooth/sewn/outline/iso)

    TechDraw.projectEx 返回的投影边均在 Z=0 平面：
            - 前视图（front，从 -Y 看向 +Y）: result.x=-worldZ, result.y=worldX → ax=1, ay=0, sy=-1
      - 沿 +Z 投影（top）  : result.x=worldX, result.y=worldY  → ax=0, ay=1
            - 左视图（left，从 -X 看向 +X） : result.x=-worldZ, result.y=-worldY → ax=1, ay=0, sy=-1
    """
    import FreeCAD
    import TechDraw

    try:
        result = TechDraw.projectEx(shape, FreeCAD.Vector(*direction))
    except Exception as e:
        print(f"  [warn] projectEx({direction}) failed: {e}")
        return []

    # TechDraw.projectEx 返回 10 个化合物：
    #   [0]V_hard  [1]V_smooth  [2]V_sewn  [3]V_outline  [4]V_iso
    #   [5]H_hard  [6]H_smooth  [7]H_sewn  [8]H_outline  [9]H_iso
    # 只取有意义的边：排除 sewn（网格伪缝合线）和 iso（参数曲线网格线）
    VISIBLE_IDX = [0, 1, 3]   # V_hard, V_smooth, V_outline
    HIDDEN_IDX  = [5, 6, 8]   # H_hard, H_smooth, H_outline

    visible_ents = []
    for idx in VISIBLE_IDX:
        try:
            for edge in result[idx].Edges:
                visible_ents += edge_to_ents(edge, ax, ay, sx, sy, layer=lvis, lt="")
        except Exception:
            pass
    visible_ents = _remove_covered_lines(visible_ents)

    visible_keys = {key for key in (_line_key(e) for e in visible_ents) if key is not None}
    hidden_ents = []
    hidden_keys = set()
    for idx in HIDDEN_IDX:
        try:
            for edge in result[idx].Edges:
                for ent in edge_to_ents(edge, ax, ay, sx, sy, layer=lhid, lt="HIDDEN"):
                    key = _line_key(ent)
                    if key is not None:
                        if key in visible_keys or key in hidden_keys:
                            continue
                        if _line_fully_covered_by(ent, visible_ents) or _line_fully_covered_by(ent, hidden_ents):
                            continue
                        hidden_keys.add(key)
                    hidden_ents.append(ent)
        except Exception:
            pass
    hidden_ents = _remove_covered_lines(hidden_ents)

    return visible_ents + hidden_ents


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

    # 三视图投影（第一角投影 / 国标 GB/T 4458.1）
    #
    # TechDraw.projectEx 投影结果均在 Z=0 平面，坐标映射：
    #   正视图 -Y→+Y: result.x=-worldZ(高), result.y=worldX(宽)
    #             ax=1→DXF_x=worldX, ay=0 + sy=-1→DXF_y=worldZ
    #   俯视图 +Z: result.x=worldX(宽), result.y=worldY(深)
    #             ax=0→DXF_x=worldX, ay=1→DXF_y=worldY，保持 FreeCAD 顶视图方向
    #   左视图 -X→+X: result.x=-worldZ(高), result.y=-worldY(深)
    #             ax=1→DXF_x=-worldY, ay=0 + sy=-1→DXF_y=worldZ

    print("  投影正视图 (front, 从 -Y 看向 +Y) ...")
    front = project_view(shape, (0, -1, 0), ax=1, ay=0, sx=1.0, sy=-1.0,
                         lvis="FRONT", lhid="FRONT_HID")

    print("  投影俯视图 (top, 从 +Z 看向原点) ...")
    top = project_view(shape, (0, 0, 1), ax=0, ay=1, sx=1.0, sy=1.0,
                       lvis="TOP", lhid="TOP_HID")

    print("  投影左视图 (left, 从 -X 看向 +X) ...")
    left = project_view(shape, (-1, 0, 0), ax=1, ay=0, sx=1.0, sy=-1.0,
                        lvis="LEFT", lhid="LEFT_HID")

    # 各视图归零到左下角原点
    front, fw, fh = normalize(front)
    top,   tw, th = normalize(top)
    left,  lw, lh = normalize(left)

    # 视图间距：视图最大尺寸的 20%，至少 1 单位
    max_dim = max(fw, fh, tw, th, lw, lh, 1e-6)
    GAP = max(max_dim * 0.2, 1e-3)
    print(f"  视图尺寸 front={fw:.3f}x{fh:.3f}  top={tw:.3f}x{th:.3f}  left={lw:.3f}x{lh:.3f}  GAP={GAP:.3f}")

    # 布局（第一角投影）：
    #   正视图（左上）  左视图（右上）
    #   俯视图（左下）
    all_ents = (
        shift_ents(front, 0,          th + GAP) +
        shift_ents(left,  fw + GAP,   th + GAP) +
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

def expand_input_files(inputs):
    """展开输入路径：文件直接转换，目录转换其中所有 STEP/STP 文件。"""
    result = []
    for item in inputs:
        path = os.path.abspath(item)
        if os.path.isdir(path):
            result += glob.glob(os.path.join(path, "*.step"))
            result += glob.glob(os.path.join(path, "*.stp"))
        elif not os.path.exists(path):
            print(f"[3D2DXF] 路径不存在，已跳过: {path}")
        else:
            result.append(path)
    return sorted(result)


def main(files=None):
    """
    files: list of input file/dir paths, or None to process STEP/STP files in ./3d/
    返回值: 错误数量（0 表示全部成功）
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_dir  = os.path.join(script_dir, "3d")
    output_dir = os.path.join(script_dir, "output")
    os.makedirs(output_dir, exist_ok=True)

    if files is None:
        files = expand_input_files([input_dir])
        if not files:
            print(f"[3D2DXF] 3d/ 目录下没有找到 STEP/STP 文件: {input_dir}")
            return 1
    else:
        files = expand_input_files(files)
        if not files:
            print("[3D2DXF] 指定路径下没有找到 STEP/STP 文件")
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
