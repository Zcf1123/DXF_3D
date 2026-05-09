# 3D2DXF — 3D 模型转 DXF 三视图工具

将 STEP / IGES / STL / OBJ 等三维模型文件自动投影为工程图三视图，输出标准 DXF 格式。

---

## 目录结构

```
3D2DXF/
├── convert.py    # 主转换脚本（在 FreeCAD 环境中运行）
├── run.sh        # Docker 启动脚本
├── 3d/           # 放置输入的 3D 文件
└── output/       # 输出的 DXF 文件
```

---

## 依赖

与上级项目（DXF_3D）共用同一个 Docker 镜像，镜像包含 FreeCAD + Python 环境。

首次使用前在 `DXF_3D/` 目录下构建镜像：

```bash
cd ..
docker build -t dxf-3d .
```

镜像名默认为 `dxf-3d`，可通过环境变量覆盖：

```bash
DXF_3D_IMAGE=my-image ./run.sh
```

---

## 使用方式

### 基本用法

```bash
cd 3D2DXF

# 转换 3d/ 目录下所有支持的文件
./run.sh

# 转换指定文件（可同时指定多个）
./run.sh 3d/model.step
./run.sh 3d/part1.step 3d/part2.stp

# 使用绝对路径（文件不必在 3d/ 目录下）
./run.sh /path/to/some_part.step
```

### 开发模式（修改 convert.py 后无需重建镜像）

```bash
./run.sh -d 3d/model.step
./run.sh -d          # 处理 3d/ 下所有文件
```

### 输出

每个输入文件在 `output/` 目录生成同名 `.dxf` 文件，例如：

```
3d/model.step  →  output/model.dxf
```

---

## 支持的输入格式

| 格式 | 扩展名 | 说明 |
|------|--------|------|
| STEP | `.step` `.stp` | 推荐，精确几何，圆弧/直线完整保留 |
| IGES | `.iges` `.igs` | 类似 STEP，精确几何 |
| STL  | `.stl` | 三角网格，圆弧会离散为折线 |
| OBJ  | `.obj` | 三角网格，同 STL |

> STL/OBJ 由于本身是网格近似，转出的轮廓线为折线段，精度低于 STEP/IGES。

---

## 输出 DXF 说明

### 三视图布局（第一角投影）

```
┌────────────────┬────────────────┐
│  FRONT（主视） │  RIGHT（右视） │
├────────────────┘                │
│  TOP（俯视）                    │
└─────────────────────────────────┘
```

视图间距自动按视图最大尺寸的 20% 计算，保证三视图比例协调。

### 图层定义

每个视图分为可见线和隐藏线两个图层：

| 图层 | 线型 | 内容 |
|------|------|------|
| `FRONT` | 实线 | 主视图可见轮廓 |
| `FRONT_HID` | 虚线 | 主视图隐藏线（被遮挡轮廓） |
| `TOP` | 实线 | 俯视图可见轮廓 |
| `TOP_HID` | 虚线 | 俯视图隐藏线 |
| `RIGHT` | 实线 | 右视图可见轮廓 |
| `RIGHT_HID` | 虚线 | 右视图隐藏线 |

DXF 格式为 R12（AC1009），兼容 AutoCAD、FreeCAD、LibreCAD 等主流 CAD 软件。

---

## 工作原理

### 整体流程

```
3D 文件
   │
   ▼  load_shape()
FreeCAD TopoShape       ← Part.read()（STEP/IGES）或 Mesh→Shape（STL/OBJ）
   │
   ▼  project_view() × 3
投影边集合（可见 + 隐藏）  ← TechDraw.projectEx() 沿三个轴方向投影
   │
   ▼  edge_to_ents()
DXF 实体列表              ← 离散化为 LINE 线段（64 点精度）
   │
   ▼  normalize() + shift_ents()
视图归零 + 布局排列
   │
   ▼  DXFWriter.write()
.dxf 文件
```

### 核心步骤说明

**1. 加载 3D 文件**

STEP/IGES 由 FreeCAD 的 `Part.read()` 直接读取为精确几何体（TopoShape）。
STL/OBJ 先以 `Mesh` 模块读取网格，再通过 `makeShapeFromMesh` + `removeSplitter` 转为 TopoShape。

**2. 三方向投影（`TechDraw.projectEx`）**

沿三个轴方向各做一次 HLR（Hidden Line Removal，消隐）投影：

| 视图 | 投影方向 | 投影平面 |
|------|----------|----------|
| 主视图（front） | +Y 轴 | XZ 平面 |
| 俯视图（top） | +Z 轴 | XY 平面 |
| 右视图（right） | +X 轴 | YZ 平面 |

`TechDraw.projectEx` 返回 10 个化合物（compound），前 5 个为可见边，后 5 个为隐藏边，分别包含 hard edges、smooth edges、sewn edges、outline edges、iso edges 五类。

投影结果的坐标均落在 Z=0 平面，坐标映射关系：
- front（+Y 投影）：result.x = worldZ，result.y = worldX
- top（+Z 投影）：result.x = worldX，result.y = worldY
- right（+X 投影）：result.x = worldZ，result.y = worldY

**3. 边转 DXF 实体（`edge_to_ents`）**

由于 `TechDraw.projectEx` 返回的边均为 BSplineCurve 类型（即使原始几何是直线），统一使用离散化处理：
- 每条边离散为 64 个点
- 若所有点共线（非闭合），合并为一条 LINE
- 否则连接相邻点输出为多段 LINE

**4. 布局与输出**

各视图先通过 `normalize()` 平移到左下角原点，再按第一角投影规则用 `shift_ents()` 排列到最终位置，最后由 `DXFWriter` 写出含 HEADER、TABLES（线型表 + 图层表）、ENTITIES 三段的 DXF R12 文件。

---

## 修改布局

如需调整三视图的排列方式，修改 `convert.py` 中 `convert()` 函数末尾的布局代码：

```python
# 第一角投影（当前默认）：FRONT 左上，RIGHT 右上，TOP 左下
all_ents = (
    shift_ents(front, 0,          th + GAP) +
    shift_ents(right, fw + GAP,   th + GAP) +
    shift_ents(top,   0,          0)
)

# 第三角投影（美国标准）：TOP 左上，FRONT 左下，RIGHT 右下
all_ents = (
    shift_ents(top,   0,          fh + GAP) +
    shift_ents(front, 0,          0) +
    shift_ents(right, fw + GAP,   0)
)
```

其中 `fw/fh`、`tw/th`、`rw/rh` 分别是主/俯/右视图的宽度和高度，`GAP` 为视图间距（自动计算为最大视图尺寸的 20%）。
