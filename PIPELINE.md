## 总览

当前默认运行路线是 **Auto LLM 直接建模路线**：解析 DXF、分类三视图并生成紧凑的
`auto_context.json`，然后由 LLM 直接编写并执行 FreeCAD Python 脚本。

只有显式加 `--direct` 时，才进入确定性特征路线并生成/使用
`features_draft.json` 与 `features.json`。

```
.dxf 文件
  │
  ▼ 阶段 1   dxf_loader                      → entities.json
  ▼ 阶段 2   view_classifier                 → views_algorithm.json / views.json
  ▼ 阶段 3   projection_mapper               → 归一化 projected views
  │
  ├─ 默认 Auto 路线
  │    ▼ llm/code/llm_code_planner.build_auto_context → auto_context.json
  │    ▼ LLM 生成 FreeCAD Python                         → generated_model.py
  │    ▼ 执行脚本 + 尺寸契约校验                          → .FCStd / dimension_validation.json
  │    ▼ direct/code/exporters                            → .step / .obj / .png / model.json / run.log
  │
  └─ --direct 路线
       ▼ direct/code/llm_planner.review_views             → views_semantic.json  ← 启用 LLM 时执行
       ▼ direct/code/feature_inference                    → features_draft.json
       ▼ direct/code/llm_planner.refine_features          → features.json        ← 启用 LLM 时执行
       ▼ direct/code/freecad_builder                      → .FCStd
       ▼ direct/code/exporters                            → .step / .obj / .png / model.json / generated_model.py / run.log
```

默认 Auto 路线复用阶段 1-3 的解析、分类和投影，但**不生成 `Feature`，也不生成
`features.json`**；它会把紧凑的 `auto_context.json` 交给
`llm/code/llm_code_planner.py`，由 LLM 直接生成 `generated_model.py`，执行后再复用
`direct/code/exporters.py` 导出 STEP / OBJ / PNG / model.json。

加 `--direct` 时使用确定性特征路线：先生成 `features_draft.json/features.json`，再交给
`direct/code/freecad_builder.py` 建模。

注意：默认 Auto 路线强依赖 LLM。若默认路线下使用 `--no-llm` 或
`DXF_3D_DISABLE_LLM=1` 禁用 LLM，程序**不会自动切换为 `--direct`**；Auto 路线会因为
无法生成 FreeCAD 脚本而失败。若要完全不使用 LLM 跑确定性算法，应显式使用：

```bash
./run.sh -d --direct --no-llm dxf_files/xxx.dxf
```

每次运行在 `outputs/` 下创建一个独立子目录：

```
outputs/<YYYYMMDD>_<HHMMSS>_<文件名>/
```

---

## 阶段 0 — 启动准备

根入口 `run.py` 只转发到 `direct/code/run.py` 的 `main()`；实际初始化由
`direct/code/run.py` 完成：

1. 解析命令行参数，确定运行路线：
  - 默认：Auto LLM 直接建模路线，实例化 `LLMClient`。
  - `--direct`：确定性特征路线，实例化 `LLMPlanner`。
  - `--auto`：隐藏兼容参数，等价于默认 Auto 路线。
2. 读取 `config.json`。若 API key 为空、配置文件不存在、`--no-llm` 或
  `DXF_3D_DISABLE_LLM=1` 生效，LLM 标记为 `disabled`。
  - 默认 Auto 路线下，LLM disabled 不会自动降级为 direct；后续脚本生成阶段会失败。
  - `--direct` 路线下，LLM disabled 表示跳过视图复核和特征精修，直接使用算法结果。
3. 从命令行参数或 `dxf_files/` 目录收集待处理 DXF 列表。
4. 为每个 DXF 文件创建带时间戳的输出目录并初始化日志文件（`run.log`）。

---

## 阶段 1 — DXF 解析（`direct/code/dxf_loader.py`）

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

## 阶段 2 — 三视图分类（`direct/code/view_classifier.py`）

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
cluster.center.x > mx  且  cluster.center.y ≥ my   →  left（右上）
右下象限                                            →  不使用
```

同象限有多个 cluster 时，取实体数量最多、bbox 面积最大的那个。

### 标注归属

标注类实体按质心最近距离归到最近 bundle 的 `annotations` 列表，供后续尺寸估算使用。

**输出：** `views_algorithm.json`（算法原始分类，含每个视图的 bbox + 实体数）

---

## 默认 Auto 路线 — LLM 直接生成 FreeCAD 脚本

默认命令：

```bash
./run.sh -d dxf_files/xxx.dxf
```

该路线在阶段 1-3 后进入 `llm/code/llm_code_planner.py`：

1. `build_auto_context()` 从 `views`、`projected_views`、闭合轮廓、近似圆、隐藏线、孔线索和
  `prompts/part_knowledge.md` 构造 `auto_context.json`。
2. `generate_freecad_script()` 使用 `llm/prompts/freecad_script_generator.md` 请求 LLM 输出完整
  FreeCAD Python 脚本。
3. `validate_generated_script()` 对脚本做安全和结构校验，拒绝危险调用和常见错误 FreeCAD API。
4. 脚本写入 `generated_model.py` 后由 `runpy.run_path()` 执行，生成 `<base>.FCStd`。
5. `validate_fcstd_dimensions()` 使用 `dimension_constraints` 校验 `Result.Shape.BoundBox` 的
  X/Y/Z 尺寸。
6. 尺寸不合格时，先尝试 `normalize_fcstd_dimensions()` 自动归一化；仍失败时请求 LLM 重写脚本。
7. 成功后调用导出器生成 STEP / OBJ / PNG / `model.json`。

Auto 路线产物重点：

| 文件 | 说明 |
|------|------|
| `auto_context.json` | 送给 LLM 的紧凑三视图/投影/零件语义上下文 |
| `generated_model.py` | LLM 生成并实际执行的 FreeCAD 脚本 |
| `dimension_validation.json` | Auto 路线尺寸契约校验结果 |
| `llm_raw_response*.txt` | LLM 原始响应或重试响应（用于调试） |

Auto 路线不产生 `features_draft.json` / `features.json`。这些文件只属于 `--direct` 路线。

---

## `--direct` 阶段 2.5 — 视图语义复核（`direct/code/llm_planner.py`，可选）

以下阶段只在显式加 `--direct` 时执行：

```bash
./run.sh -d --direct dxf_files/xxx.dxf
```

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

## `--direct` 阶段 3 — 投影与特征推断

### 3a — 坐标投影（`direct/code/projection_mapper.py`）

把每个 bundle 的 2D 实体平移（减去 bbox 左下角偏移），归一化到以下坐标系：

| 视图 | 草图面 | draw.x → 世界 | draw.y → 世界 |
|------|--------|--------------|--------------|
| front | XZ | X | Z |
| top | XY | X | Y |
| left | YZ | Y | Z |

### 3b — 零件尺寸估算（`direct/code/geometry_estimator.py`）

1. 从各视图的 `DIMENSION` 标注里提取线性标注（`dim_type & 0x0F == 0`）。
2. 按旋转角判断标注方向（水平/垂直），映射到 W（宽，沿 X）/ D（深，沿 Y）/ H（高，沿 Z）轴，取各轴最大值。
3. 没有标注的轴用视图 bbox 均值兜底。

### 3c — 特征推断（`direct/code/feature_inference.py`）

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

取分值最高的视图作为拉伸基面（首选 top，次选 front，次次选 left）：

| source_view | plane | 拉伸轴 | depth 含义 |
|-------------|-------|--------|-----------|
| top | XY | +Z | 高度 H |
| front | XZ | +Y | 进深 D |
| left（LEFT 左视图） | YZ | +X | 宽度 W |

若无任何闭合轮廓，退化为 `base_block`（包围盒长方体）。

#### ③ 通孔识别

从各视图的 `CIRCLE` 实体生成孔候选（`hole`），然后做**跨视图隐藏线验证**：

- 在未生成该孔的另外两个视图里查找 `HIDDEN` 层几何
- 检查 HIDDEN 实体的 bbox 是否与孔投影范围重叠（容差 = max(r×15%, 0.5mm)）
- 有重叠 → 保留；无重叠但无 HIDDEN 层 → 默认保留（DXF 可能省略虚线）；有 HIDDEN 层但无重叠 → 过滤

孔轴方向由来源视图决定：top→Z，front→Y，left→X。

#### ④ 倒角识别

若 front / left 视图顶部或底部边有 ARC，推断存在 `edge_chamfer`。

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

## `--direct` 阶段 4 — LLM 特征精化（`direct/code/llm_planner.py`，可选）

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

## `--direct` 阶段 5 — FreeCAD 建模（`direct/code/freecad_builder.py`）

> 需要在 `freecadcmd` 环境下运行，普通 `python3` 无法导入 `FreeCAD / Part / Mesh`。

### 构建逻辑

按 `features.json` 顺序依次处理。再次强调：`features.json` 只在 `--direct` 路线产生并被消费，
默认 Auto 路线不使用该文件。

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
| `DXF_LEFT` | 左视图原始边线 |

**输出：** `<base>.FCStd`

---

## 阶段 6 — 导出附加产物（`direct/code/exporters.py`）

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
| 默认 Auto 路线 LLM 不可用（无 key / `--no-llm` / 网络失败） | 不会自动切换到 `--direct`；无法生成脚本时 `Status: FAILED` |
| Auto 路线 LLM 脚本未通过安全/结构校验 | 自动请求 LLM 修复一次；仍失败则 `Status: FAILED` |
| Auto 路线脚本执行失败 | 把异常原因发给 LLM 请求重写；仍失败则 `Status: FAILED` |
| Auto 路线尺寸契约校验失败 | 先尝试尺寸归一化，再尝试 LLM 尺寸修复；仍失败则 `Status: FAILED` |
| `--direct` 路线 LLM 不可用 | 跳过视图复核和特征精修，直接使用算法草案 |
| `--direct` 路线 LLM 返回非 JSON | 回退草案，写 log |
| `--direct` 路线 LLM 违反安全校验 | 回退草案，写 log |
| `--direct` 路线布尔 cut 失败（孔） | 记录 warning，继续后续特征 |
| `--direct` 路线无任何闭合轮廓 | 退化为 `base_block` |
| `--direct` 路线整个 solid 为 None | `Status: FAILED`，停止阶段 5/6 |
| 单项导出失败（STEP/OBJ/PNG 等） | 记录 warning，其余产物正常生成 |
