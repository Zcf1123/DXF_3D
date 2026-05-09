# DXF_3D — DXF 三视图到 3D 重建

把 `.dxf` 工程图（FRONT/TOP/RIGHT 三视图）解析成 3D 模型。
**自包含**：本目录内含 `Dockerfile` / `run.sh` / `requirements.txt` / `config.json`，
可以**整个目录拷贝到任意主机独立部署**，不依赖仓库其它任何文件。

---

## 一、快速开始（已构建镜像）

把要处理的 `.dxf` 放到 `DXF_3D/dxf_files/`，然后：

```bash
# 跑 dxf_files/ 下所有 DXF
./run.sh

# 或指定文件（路径可在 DXF_3D 内或宿主机任意位置）
./run.sh dxf_files/Drawing1.dxf
./run.sh /path/to/some.dxf

# 单一俯视图：按给定长度沿 Z 方向直接拉伸
./run.sh --extrude-depth 20 dxf_files/top_view_only.dxf

# 跳过 LLM，走纯算法路径（复杂图纸调试时更快）
./run.sh --no-llm dxf_files/Drawing1.dxf

# 给 LLM 受控建模意图，辅助理解隐藏线较多的复杂零件
./run.sh --model-intent "先拉伸圆柱；侧面矩形孔贯穿切除；上端圆孔盲切" dxf_files/part.dxf
```

镜像名可用环境变量 `DXF_3D_IMAGE` 覆盖（默认 `dxf-3d`）。
开发调试时可加 `-d`，让容器挂载当前源码目录，避免每次改代码后重建镜像。

### 单一俯视图拉伸

如果 DXF 中只包含一个俯视图轮廓，可以在命令行提供拉伸长度：

```bash
./run.sh --extrude-depth 20 dxf_files/top_view_only.dxf
```

该模式只在识别到单一几何视图时触发，会把该视图固定按 TOP/XY 平面处理并沿 Z
方向拉伸；标准 FRONT/TOP/RIGHT 三视图输入仍走原有建模逻辑。闭合线框、多段线、
弧线轮廓会作为外轮廓，内部圆会作为贯穿孔，内部闭合线/弧轮廓会作为异形贯穿孔；
只有一个圆时会拉伸为圆柱。输出仍会导出模型三视图 PNG，并在 `.FCStd` 中补充
由 3D 模型生成的 FRONT/RIGHT 投影视图线框。

---

## 二、部署到另一台主机

> 整个 `DXF_3D/` 目录已是自包含项目，按下列两种方式之一即可。

### 方案 A：源码 + 在目标主机构建镜像（推荐）

1. 把 `DXF_3D/` 目录拷到目标主机：
   ```bash
   tar czf dxf_3d.tar.gz DXF_3D/
   scp dxf_3d.tar.gz user@host:/path/
   ssh user@host "cd /path && tar xzf dxf_3d.tar.gz"
   ```
2. 在目标主机构建镜像：
   ```bash
   cd /path/DXF_3D
   docker build -t dxf-3d .
   ```
3. 按需修改 `config.json`（OpenAI 兼容协议的 API key / base_url / model），
   把 DXF 放进 `dxf_files/`，运行：
   ```bash
   ./run.sh
   ```

### 方案 B：导出镜像离线传输

源主机一次性构建 + 导出：

```bash
cd DXF_3D
docker build -t dxf-3d .
docker save dxf-3d | gzip > dxf-3d.tar.gz
```

把 `dxf-3d.tar.gz` 和整个 `DXF_3D/` 目录都拷到目标主机：

```bash
ssh user@host "docker load < dxf-3d.tar.gz"
# 在目标主机
cd /path/to/DXF_3D
./run.sh
```

> 提示：`run.sh` 通过卷挂载 `dxf_files/`、`outputs/`、`config.json`，
> 因此**改 DXF 或改 LLM 配置都不需要重建镜像**。

### 镜像内容（精简版）

| 来源 | 内容 |
| --- | --- |
| Ubuntu 22.04 | 基础系统 |
| `freecad`（PPA） | 提供 `freecadcmd` / `FreeCAD` / `Part` / `Mesh` / `MeshPart` |
| `requirements.txt` | `matplotlib`、`openai` |
| `COPY .` | DXF_3D 业务代码 |

启动入口：`freecadcmd` 加载 `DXF_3D.run.main`。

---

## 三、终端输出 / 日志

终端只打印核心摘要：

```
LLM         : qwen3.5-35b-a3b
Projection   : WARN
   FRONT WARN input= 82.1% model=100.0% extra=  0.0%
   RIGHT WARN input= 75.6% model=100.0% extra=  0.0%
   TOP   OK   input=100.0% model= 99.6% extra=  0.4%
Output dir  : DXF_3D/outputs/20260507_095610_Drawing1
Status      : OK
```

其中 `Projection` 是反投影验证摘要：把最终 3D 模型重新投影回 FRONT/RIGHT/TOP，
再与输入三视图比对。`input` 表示输入视图被模型覆盖的比例，`model` 表示模型
投影能被输入视图解释的比例，`extra` 表示模型投影中的多余线比例。
其余阶段日志（实体统计、视图归类、特征草案、LLM 返回、产物清单等）全部以中文
写入 `<output_dir>/run.log`。

如果只想走确定性算法、跳过 LLM 复核以缩短复杂图纸的运行时间，可以使用：

```bash
./run.sh --no-llm dxf_files/Drawing1.dxf
# 或
DXF_3D_DISABLE_LLM=1 ./run.sh dxf_files/Drawing1.dxf
```

如果复杂零件靠三视图隐藏线仍容易歧义，可以给 LLM 一段受控建模意图，帮助它把
图纸线段整理成 builder 支持的 `hole` / `profile_cut` 特征。该意图只用于受校验的
特征精修，不能让 LLM 输出当前 builder 不支持的自由特征：

```bash
./run.sh --model-intent "先拉伸一个圆柱；侧面矩形孔贯穿切除；上底面圆孔切除但不贯穿；中间再做一个不贯穿矩形切除" dxf_files/00996032.dxf
```

---

## 四、输入约定（必须遵守）

### 1. 视图布局（固定）

```
+------------------+------------------+
|  FRONT (主视图)  |  RIGHT (左视图)  |
|  左上            |  右上            |
+------------------+------------------+
|  TOP   (俯视图)  |   (空)           |
|  左下            |                  |
+------------------+------------------+
```

| 视图  | 位置 | 对应 3D 平面 | 草图坐标 → 世界坐标 |
| ----- | ---- | ------------ | -------------------- |
| FRONT | 左上 | XZ (Y=0)     | x → X，y → Z         |
| TOP   | 左下 | XY (Z=0)     | x → X，y → Y         |
| RIGHT | 右上 | YZ (X=0)     | x → Y，y → Z         |

零件三维尺寸命名约定：宽 W (沿 X) / 深 D (沿 Y) / 高 H (沿 Z)。

### 2. 几何规则

1. 闭合轮廓只能由 `LINE` / `ARC` / `CIRCLE` / `LWPOLYLINE` 组成；
   `SPLINE` / `ELLIPSE` / `HATCH` 一律忽略。
2. 每个视图的外轮廓必须**首尾闭合**（容差 `1e-3`）。
3. 当 TOP / FRONT / RIGHT 三个视图都只有一个同半径 `CIRCLE`，且圆心满足
   `(TOP.x == FRONT.x)`、`(TOP.y == RIGHT.x)`、`(FRONT.y == RIGHT.y)` 的三视图
   坐标联动关系时，识别为 `sphere`，不作为通孔。
4. 其它视图内部的 `CIRCLE` 自动作为通孔：
   - TOP 视图中的圆 → 孔轴 = Z
   - FRONT 视图中的圆 → 孔轴 = Y
   - RIGHT 视图中的圆 → 孔轴 = X
5. 对多边形棱柱类零件，FRONT/RIGHT 中真实 ARC + TOP 中相切大圆可识别为
   上下外轮廓圆弧端面倒角（`edge_chamfer.profile="arc_revolve"`）。
6. 坐标单位默认按毫米处理，**不做单位识别 / 缩放**。

### 3. 图层（推荐，可选）

| 图层名               | 含义     | 处理       |
| -------------------- | -------- | ---------- |
| `OUTLINE` / `0`      | 可见轮廓 | 用于建模   |
| `HIDDEN` / `*_HID`   | 虚线     | 作为孔、盲孔、内部切除等隐藏结构证据，不直接作为实体轮廓 |
| `CENTER`             | 中心线   | 忽略       |
| `DIM`                | 标注     | 忽略       |

如果不区分图层，则所有几何视为可见轮廓。

`*_HID` 是推荐的隐藏线命名方式，例如 `FRONT_HID` / `RIGHT_HID` / `TOP_HID`。
隐藏线不会被当成外轮廓直接拉伸，但会参与跨视图验证，帮助区分通孔、盲孔和内部切除。

### 4. 不支持的内容

- 多于三视图、剖视图、局部放大图、辅助视图、断面视图。
- 一般斜面/自由斜切仍不保证；当前仅支持从 FRONT/RIGHT 外轮廓中明确出现的角部
   直线削角推断三角楔切。
- 螺纹、沉孔、阵列孔、自由曲面和复杂圆角。
- 没有 TOP 多边形轮廓 + 侧视 ARC 证据的任意倒角/圆角。
- 缺少明显视图布局的对称图。

违反 §1（布局）或 §2（闭合轮廓）时，流水线会拒绝建模并把错误写入
`run.log`。

---

## 五、产物（每次运行一个 `outputs/<YYYYMMDD>_<HHMMSS>_<base>/` 目录）

| 文件                       | 说明 |
| -------------------------- | ---- |
| `<base>.FCStd`             | FreeCAD 项目（最终模型） |
| `<base>.step`              | STEP（`Part.export`） |
| `<base>.obj`               | OBJ 网格（`MeshPart` 三角化） |
| `<base>.png`               | DXF 三视图预览（matplotlib） |
| `<base>_views_normalized.png` | 归一化后的输入三视图，坐标从 0 开始，便于排查视图映射 |
| `<base>_model_views.png`   | 最终 3D 模型重新投影得到的 FRONT/RIGHT/TOP 三视图 |
| `<base>_overview.png`      | 3D 等轴侧快速总览 PNG；用于粗略预览，复杂切除件的准确性以 `.FCStd` / `.step` / `<base>_model_views.png` 为准 |
| `entities.json`            | DXF 解析后的实体 + 元数据 |
| `views_algorithm.json`     | 纯算法阶段的原始视图归类结果 |
| `views_semantic_input.json` | 提交给 LLM 视图语义复核的结构化输入摘要 |
| `views_semantic.json`      | LLM 对视图命名、保留/删除实体的复核结果 |
| `views.json`               | 最终使用的视图归类结果；启用 LLM 且校验通过时为语义复核后的结果，否则为算法结果 |
| `features_draft.json`      | LLM 介入**前**的特征草案 |
| `features.json`            | LLM 介入**后**的最终特征（未启用 LLM 时与草案相同） |
| `projection_validation.json` | 反投影验证报告：模型三视图与输入三视图的覆盖率、匹配率、bbox 差异和未覆盖线段 |
| `model.json`               | FreeCAD 文档对象信息 |
| `generated_model.py`       | 独立可重跑脚本：`freecadcmd generated_model.py` |
| `run.log`                  | 详细中文日志（每一阶段、警告、栈追踪） |

测试或调试时可设置 `DXF_3D_OUTPUT_SUBDIR=test`，输出会进入
`outputs/test/<YYYYMMDD>_<HHMMSS>_<base>/`。未设置时保持默认行为，仍输出到
`outputs/<YYYYMMDD>_<HHMMSS>_<base>/`。

---

## 六、流水线

```
dxf_loader → view_classifier → projection_mapper → feature_inference
                                                 ↘ llm_planner（可选）
                                                 → freecad_builder → exporters
```

| 模块 | 职责 |
| --- | --- |
| `dxf_loader.py`         | 纯 Python DXF 解析，输出 `DxfEntity` 列表 + 元数据 |
| `view_classifier.py`    | 按象限把实体分到 FRONT/TOP/RIGHT 三个 `ViewBundle` |
| `projection_mapper.py`  | 把每个视图的 2D 实体映射到 3D 平面坐标系 |
| `geometry_estimator.py` | 闭环检测、轮廓提取、零件尺寸估计 |
| `feature_inference.py`  | 推断拉伸轮廓、球体、同轴阶梯圆柱、圆孔/盲孔、异形贯穿孔、矩形/闭合轮廓切除和可确定的边倒角，输出 `Feature` 列表 |
| `llm_planner.py`        | 读 `config.json` 调用 OpenAI 兼容 API，复核视图语义和特征草案；证据充分或提供 `--model-intent` 时，可在校验范围内精修受支持的特征 |
| `freecad_builder.py`    | 按特征列表用 FreeCAD `Part` 建模并保存 `.FCStd` |
| `exporters.py`          | STEP / OBJ / PNG / 总览 PNG / model.json / 可复现 Python |
| `run.py`                | CLI 编排器 |

---

## 七、LLM 配置

读取本目录下的 `config.json`（OpenAI 兼容协议）：

```json
{
  "api_key": "...",
  "base_url": "...",
  "model": "..."
}
```

LLM 任何失败（缺 key、网络中断、JSON 解析错误、校验不通过）都不会中断流水线，
会自动回退到纯算法路径，原因写入 `run.log`。

LLM 当前分两步介入：

1. `drawing_view_reviewer.md`：复核 FRONT / TOP / RIGHT 视图命名，保守删除明显
   辅助线、标注线或跨视图线。
2. `feature_refiner.md`：复核 `features_draft.json`。默认不能重写主体轮廓、孔、
   edges 或尺寸；未提供建模意图时，主要允许新增有 TOP 多边形 + 侧视 ARC 等证据
   支撑的 `edge_chamfer`。提供 `--model-intent` 后，可在代码校验允许的范围内精修
   `hole` / `profile_cut` 等受支持特征，例如把明确描述的矩形孔改为贯穿切除或盲切。

prompt 文件遵循 [prompts/PROMPT_SPEC.md](prompts/PROMPT_SPEC.md) 的二级
标题分块约定，目前启用的 prompt 是
[prompts/drawing_view_reviewer.md](prompts/drawing_view_reviewer.md) 和
[prompts/feature_refiner.md](prompts/feature_refiner.md)。

---

## 八、目录速览

```
DXF_3D/
├── README.md                 本文档
├── Dockerfile                独立部署镜像定义
├── requirements.txt          Python 依赖
├── config.json               LLM 配置（API key / base_url / model）
├── run.sh                    Docker 启动脚本
├── dxf_files/                <—— 把要处理的 .dxf 放在这里
├── outputs/                  <—— 每次运行生成 <YYYYMMDD>_<HHMMSS>_<base>/
├── prompts/
│   ├── PROMPT_SPEC.md        prompt 文件分块规范
│   ├── drawing_view_reviewer.md  三视图语义复核 prompt
│   └── feature_refiner.md    特征复核 prompt（中文，详细）
├── dxf_loader.py
├── view_classifier.py
├── projection_mapper.py
├── geometry_estimator.py
├── feature_inference.py
├── llm_planner.py
├── freecad_builder.py
├── exporters.py
└── run.py
```
