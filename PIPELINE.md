## 总览

```
.dxf 文件
  │
  ▼ 阶段 1   dxf_loader          → entities.json
  ▼ 阶段 2   view_classifier     → views_algorithm.json
  ▼ 阶段 2.5 LLM.review_views   → views_semantic.json        ← 启用 LLM 时执行
  │           ↓ _apply_view_review（删除辅助线、重命名视图）
  ▼ 阶段 3   projection_mapper
             + feature_inference  → features_draft.json
  ▼ 阶段 4   LLM.refine_features → features.json             ← 启用 LLM 时执行
  ▼ 阶段 5   freecad_builder     → .FCStd
  ▼ 阶段 6   exporters           → .step / .obj / .png / _overview.png
                                    model.json / generated_model.py / run.log
```

每次运行在 `outputs/` 下创建一个独立子目录：

```
outputs/<YYYYMMDD>_<HHMMSS>_<文件名>/
```

---

## 阶段 0 — 启动准备

`main()` 完成以下初始化：

1. 读取 `config.json`，实例化 `LLMPlanner`。  
   若 `openai_api_key` 为空或文件不存在，LLM 标记为 `disabled`，流水线退化为纯算法模式。
2. 从命令行参数或 `dxf_files/` 目录收集待处理 DXF 列表。
3. 为每个 DXF 文件创建带时间戳的输出目录并初始化日志文件（`run.log`）。

---

## 阶段 1 — DXF 解析（`dxf_loader.py`）

**输入：** `.dxf` 文本文件

### 处理步骤

1. 逐行读取，两行一对拆成 `(group_code, value)` 对。
2. 按 `SECTION` 分割：
   - `HEADER`：读取 `$INSUNITS`（单位代码，记录但不做缩放）
   - `BLOCKS`：解析块定义，支持 `INSERT` 展开（含平移、缩放、旋转）
   - `ENTITIES`：主实体区
3. **跳过 `*Dn` 匿名块**（`*D0`、`*D1` 等）——这些是 `DIMENSION` 实体的箭头/引线/标注文字，会污染视图几何。
4. 支持解析的实体类型：

   | 类型 | 说明 |
   |------|------|
   | `LINE` | 直线段，取两端点 |
   | `CIRCLE` | 圆，取圆心 + 半径 |
   | `ARC` | 圆弧，取圆心 + 半径 + 起止角 |
   | `LWPOLYLINE` / `POLYLINE` | 多段线，逐顶点积累，支持 closed 标记 |
   | `ELLIPSE` | 椭圆（仅记录，不参与轮廓提取） |
   | `SPLINE` | 样条（仅记录，不参与轮廓提取） |
   | `TEXT` / `MTEXT` | 文字标注 |
   | `DIMENSION` | 尺寸标注，提取 `dim_measurement` 数值 |
   | `INSERT` | 块引用，展开为子实体 |
   | `SOLID` / `HATCH` | 填充图形（仅记录类型） |

**输出：** `entities.json`

```json
{
  "meta": { "path": "...", "backend": "pure-python", "layers": [...], "units": "4", "bbox": [...] },
  "entities": [
    { "kind": "LINE", "layer": "0", "points": [[x0,y0],[x1,y1]], "bbox": [...] },
    { "kind": "CIRCLE", "layer": "0", "center": [cx,cy], "radius": r, "bbox": [...] },
    ...
  ]
}
```

---

## 阶段 2 — 三视图分类（`view_classifier.py`）

**输入：** `List[DxfEntity]`

### 实体预分类

- **几何类**：`LINE / CIRCLE / ARC / LWPOLYLINE / POLYLINE / SPLINE / ELLIPSE`
- **标注类**：`TEXT / MTEXT / DIMENSION / HATCH / SOLID`

### 聚类逻辑（两条路径）

**主路径（bbox 近邻聚类）：**

1. 计算每个几何实体的 bbox。
2. 以对角线长的 10% 为容差，用 Union-Find 把空间相邻或互相包含的实体合并成 cluster。
3. 结果合并后检测：若小 cluster 的 bbox 被大 cluster 完全包含，也合并进去。

**兜底路径（视图分割线检测）：**  
若聚类后结果 < 3 个（视图共享边界线无间隙时），检测水平/垂直的贯穿全图分割线，按分割线把实体分到 3 个象限。贯穿线本身不归属任何视图。

### 象限到视图名映射（固定，不可改）

以全图 bbox 中心 `(mx, my)` 为基准：

```
cluster.center.x ≤ mx  且  cluster.center.y ≥ my  →  front（左上）
cluster.center.x ≤ mx  且  cluster.center.y < my   →  top  （左下）
cluster.center.x > mx  且  cluster.center.y ≥ my   →  right（右上）
右下象限                                            →  不使用
```

同象限有多个 cluster 时，取实体数量最多、bbox 面积最大的那个。

### 标注归属

标注类实体按质心最近距离归到最近 bundle 的 `annotations` 列表，供后续尺寸估算使用。

**输出：** `views_algorithm.json`（算法原始分类，含每个视图的 bbox + 实体数）

---

## 阶段 2.5 — 视图语义复核（`llm_planner.py`，可选）

**输入：** 每个视图实体的语义摘要（kind / linetype / bbox，不发送完整坐标）

### 处理步骤

1. 生成 `views_semantic_input.json` 并发送给 LLM（使用 `drawing_view_reviewer.md` prompt）。
2. LLM 返回 JSON，指示：
   - 每个视图的规范名称（`canonical_name`）
   - 需要删除的实体 ID 列表（`remove_entity_ids`）及原因（辅助线、中心线、多余短线等）
3. `_apply_view_review()` 原地修改 bundle：删除对应实体，重新计算 bbox，更新视图名称。
4. 任何失败静默忽略，沿用算法分类结果，原因写入 `run.log`。

**中间产物：**
- `views_semantic_input.json`：送给 LLM 的输入摘要
- `views_semantic.json`：LLM 原始回复 + 实际应用变化记录

**输出：** `views.json`（最终使用的视图，可能已经过 LLM 修剪）

---

## 阶段 3 — 投影与特征推断

### 3a — 坐标投影（`projection_mapper.py`）

把每个 bundle 的 2D 实体平移（减去 bbox 左下角偏移），归一化到以下坐标系：

| 视图 | 草图面 | draw.x → 世界 | draw.y → 世界 |
|------|--------|--------------|--------------|
| front | XZ | X | Z |
| top | XY | X | Y |
| right | YZ | Y | Z |

### 3b — 零件尺寸估算（`geometry_estimator.py`）

1. 从各视图的 `DIMENSION` 标注里提取线性标注（`dim_type & 0x0F == 0`）。
2. 按旋转角判断标注方向（水平/垂直），映射到 W（宽，沿 X）/ D（深，沿 Y）/ H（高，沿 Z）轴，取各轴最大值。
3. 没有标注的轴用视图 bbox 均值兜底。

### 3c — 特征推断（`feature_inference.py`）

按以下顺序处理：

#### ① 球体检测（优先）

若三个视图各只有一个圆、半径相同、无其他几何，且 W≈D≈H≈2r，输出单个 `sphere` 特征，跳过后续所有步骤。

#### ② 拉伸轮廓打分与选取

对三个视图分别提取最大闭合外轮廓，计算复杂度分值：

| 条件 | 得分 |
|------|------|
| 每条 LINE 边 | +1 |
| 每条 ARC 边 | +11（+1 边 +10 加成） |
| TOP 视图有圆孔且轮廓是多边形（六角形等） | 额外 +25 |

取分值最高的视图作为拉伸基面（首选 top，次选 front，次次选 right）：

| source_view | plane | 拉伸轴 | depth 含义 |
|-------------|-------|--------|-----------|
| top | XY | +Z | 高度 H |
| front | XZ | +Y | 进深 D |
| right | YZ | +X | 宽度 W |

若无任何闭合轮廓，退化为 `base_block`（包围盒长方体）。

#### ③ 通孔识别

从各视图的 `CIRCLE` 实体生成孔候选（`hole`），然后做**跨视图隐藏线验证**：

- 在未生成该孔的另外两个视图里查找 `HIDDEN` 层几何
- 检查 HIDDEN 实体的 bbox 是否与孔投影范围重叠（容差 = max(r×15%, 0.5mm)）
- 有重叠 → 保留；无重叠但无 HIDDEN 层 → 默认保留（DXF 可能省略虚线）；有 HIDDEN 层但无重叠 → 过滤

孔轴方向由来源视图决定：top→Z，front→Y，right→X

#### ④ 倒角识别

若 front / right 视图顶部或底部边有 ARC，推断存在 `edge_chamfer`。

**输出：** `features_draft.json`

```json
[
  {
    "kind": "extrude_profile",
    "params": {
      "plane": "XY", "depth": 7.0, "source_view": "top",
      "edges": [{"kind": "LINE", "p0": [x0,y0], "p1": [x1,y1]}, ...],
      "bbox_2d": [xmin, ymin, xmax, ymax]
    }
  },
  {
    "kind": "hole",
    "params": { "radius": 5.0, "axis": "Z", "position": [x,y,z], "through_length": 7.0, "source_view": "top" }
  }
]
```

---

## 阶段 4 — LLM 特征精化（`llm_planner.py`，可选）

**输入：** `view_bboxes` + `features_draft`

### 处理步骤

1. 使用 `feature_refiner.md` prompt 请求 LLM 输出修正后的特征 JSON。
2. **程序级安全校验**（`_validate_refined_features()`）：

   | 规则 | 违反后果 |
   |------|---------|
   | kind 必须在白名单内（`extrude_profile / base_block / hole / edge_chamfer`） | 拒绝整个结果 |
   | `extrude_profile` 的 `plane / source_view / edges` 不能改动 | 拒绝 |
   | `base_block` 的 `width / depth / height / origin` 不能改动 | 拒绝 |
   | 孔不能减少（不能删孔） | 拒绝 |
   | 不能新增孔 | 拒绝 |

3. 通过校验后，自动去重完全相同的孔。
4. 任何校验失败 → 回退 `features_draft`，原因写入 `run.log`。

**输出：** `features.json`（LLM 修正版，或与 draft 完全相同）

---

## 阶段 5 — FreeCAD 建模（`freecad_builder.py`）

> 需要在 `freecadcmd` 环境下运行，普通 `python3` 无法导入 `FreeCAD / Part / Mesh`。

### 构建逻辑

按 `features.json` 顺序依次处理：

| kind | FreeCAD 操作 |
|------|-------------|
| `extrude_profile` | 构建 `Part.Wire` → `Part.Face` → `.extrude(vec)` |
| `base_block` | `Part.makeBox(W, D, H, origin)` |
| `hole` | `Part.makeCylinder(r, length, pos, axis)` → `solid.cut(cyl)` |
| `edge_chamfer` | 遍历顶/底面边，`makeChamfer` 或 ARC 扫掠 |
| `sphere` | `Part.makeSphere(r, center)` |

- 布尔 cut 失败时记录 warning 进 `run.log`，不中断流程。
- 若所有特征处理完成后 `solid` 仍为 `None`，抛出异常（最终 `Status: FAILED`）。

### 文档结构

FreeCAD 文档中包含以下对象：

| 对象名 | 内容 |
|--------|------|
| `Result` | 最终 3D 实体（唯一的 solid） |
| `DXF_FRONT` | 前视图原始边线（edge compound，供对照） |
| `DXF_TOP` | 俯视图原始边线 |
| `DXF_RIGHT` | 侧视图原始边线 |

**输出：** `<base>.FCStd`

---

## 阶段 6 — 导出附加产物（`exporters.py`）

每项导出单独 try-except，失败只写 warning，不影响整体 Status：

| 产物 | 工具 | 说明 |
|------|------|------|
| `<base>.step` | `Part.export([Result.Shape])` | 只导出 `Result` 实体，不含三视图线框 |
| `<base>.obj` | `MeshPart.meshFromShape` + `Mesh.export` | 三角化网格，只导出 `Result` |
| `<base>.png` | matplotlib（无需 GUI） | 三视图 2×2 预览图 |
| `<base>_overview.png` | matplotlib 等轴测投影 | 3D 线框总览，白底黑线，无坐标轴 |
| `model.json` | FreeCAD `BoundBox` + `Volume` | 实体元信息摘要 |
| `generated_model.py` | 字符串生成 | 独立可重跑脚本，`freecadcmd generated_model.py` |
| `run.log` | Python `logging` | 所有阶段中文详细日志，含 LLM 请求/响应 |

---

## 终端摘要输出（stderr）

```
LLM         : qwen3.5-35b-a3b
Output dir  : outputs/20260508_095610_Drawing1
Status      : OK
```

`freecadcmd` 自身的进度输出被重定向到 `/dev/null`，只有流水线摘要通过 stderr 显示。

---

## 错误与降级策略

| 情形 | 行为 |
|------|------|
| LLM 不可用（无 key / 网络失败） | 跳过 LLM 阶段，全用算法结果 |
| LLM 返回非 JSON | 回退草案，写 log |
| LLM 违反安全校验 | 回退草案，写 log |
| 布尔 cut 失败（孔） | 记录 warning，继续后续特征 |
| 无任何闭合轮廓 | 退化为 `base_block` |
| 整个 solid 为 None | `Status: FAILED`，停止阶段 5/6 |
| 单项导出失败（STEP/OBJ/PNG 等） | 记录 warning，其余产物正常生成 |
