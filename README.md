# DXF_3D — DXF 三视图到 3D 重建

本项目把符合固定布局的 `.dxf` 工程图三视图解析为 3D 模型。当前路线：

```text
DXF 三视图 → DXF 实体解析 → FRONT/TOP/LEFT 视图分类 → 2D/3D 投影摘要
         → LLM 生成 FreeCAD Python 脚本 → 执行脚本 → 导出 FCStd/STEP/OBJ/PNG
```

项目可独立部署：目录内包含 `Dockerfile`、`run.sh`、`requirements.txt`、`config.json` 。拷贝 `DXF_3D/` 目录构建 Docker 镜像并配置 LLM 即可运行。

---

## 1. 输入与输出

### 输入

主要输入是 DXF 工程图文件：

- 默认输入目录：`dxf_files/`
- 支持命令行指定单个或多个 `.dxf`
- 单位默认按毫米处理，不做单位识别或缩放
- 图纸必须是固定三视图布局：`FRONT` 左上、`LEFT` 右上、`TOP` 左下

可选输入：

| 输入 | 位置/方式 | 说明 |
| --- | --- | --- |
| LLM 配置 | `config.json` | OpenAI 兼容接口配置，含 `api_key`、`base_url`、`model`、`api_mode` |
| 模型 profile | `--qwen` / `--gpt` / `--openai` | 临时切换 `config.json` 中的 profile |
| 建模意图提示 | `--intent "..."` | 给 LLM 的弱提示，用于歧义消解，不能覆盖三视图证据 |
| 投影验证开关 | `--val` | 额外生成模型投影与输入三视图的对比报告 |
| 输出子目录 | `DXF_3D_OUTPUT_SUBDIR=name` | 把输出写入 `outputs/name/` 下，便于测试分组 |

### 输出

每处理一个 DXF，会在 `outputs/` 下生成一个独立目录：

```text
outputs/<model>_<YYYYMMDD>_<HHMMSS>_<source_base>/
```

常见输出文件：

| 文件 | 说明 |
| --- | --- |
| `<base>.FCStd` | FreeCAD 原生工程文件，最终模型的主要检查对象 |
| `<base>.step` | STEP 通用 CAD 交换格式 |
| `<base>.obj` | OBJ 网格导出，便于轻量预览 |
| `<base>.png` | 输入 DXF 三视图预览图 |
| `<base>_views_normalized.png` | 坐标归一化后的输入三视图，便于排查视图映射 |
| `<base>_model_views.png` | 最终 3D 模型重新投影得到的 FRONT/LEFT/TOP 三视图 |
| `<base>_overview.png` | 3D 等轴侧快速预览图 |
| `entities.json` | DXF 解析后的实体和元数据 |
| `views_algorithm.json` | 纯算法视图分类结果 |
| `views.json` | 最终使用的视图分类结果 |
| `auto_context.json` | 发送给 LLM 的紧凑三视图/投影/几何上下文 |
| `generated_model.py` | LLM 生成并实际执行的 FreeCAD Python 脚本 |
| `dimension_validation.json` | 生成模型的包围盒尺寸契约校验结果 |
| `arc_validation.json` | 圆弧边约束校验结果；输入无 ARC 约束时可能为跳过 |
| `projection_validation.json` | 仅使用 `--val` 时生成；模型投影与输入视图的覆盖率/多余线报告 |
| `model.json` | FreeCAD 文档对象摘要 |
| `run.log` | 详细中文日志，包含各阶段、警告、异常栈和导出结果 |

---

## 2. 快速开始

### 构建镜像

```bash
docker build -t dxf-3d .
```

镜像名默认是 `dxf-3d`，也可通过环境变量覆盖：

```bash
DXF_3D_IMAGE=my-dxf-3d ./run.sh -d dxf_files/nut.dxf
```

### 运行

```bash
# 处理 dxf_files/ 下所有 DXF
./run.sh -d

# 处理指定文件
./run.sh -d dxf_files/nut.dxf

# 处理多个文件
./run.sh -d dxf_files/a.dxf dxf_files/b.dxf

# 使用指定模型 profile
./run.sh -d --qwen dxf_files/nut.dxf
./run.sh -d --gpt dxf_files/nut.dxf
./run.sh -d --openai dxf_files/nut.dxf

# 加建模意图弱提示
./run.sh -d --intent "先拉伸主体，再按三视图证据切除贯穿孔" dxf_files/nut.dxf

# 启用投影验证
./run.sh -d --val dxf_files/nut.dxf

# 禁用 LLM；默认路线会尽量使用结构化兜底，但复杂零件成功率会下降
./run.sh -d --no-llm dxf_files/nut.dxf
```

`-d` 表示开发挂载模式：把本地源码目录挂载进容器，改代码后无需重建镜像。日常开发和调试建议始终带 `-d`。

### 批量测试运行：`run_contrast_batch.sh`

`run_contrast_batch.sh` 用于批量处理一组 DXF，并汇总每个零件的 LLM 模型、投影验证指标、耗时、LLM 模型理解和 FCStd 路径。它内部会逐个调用 `run.sh -d`，并默认自动追加 `--val`，因此会生成投影验证结果。批处理终端只输出当前进度行，例如 `[1/27] dxf_files/test/00019717.dxf`，详细结果写入汇总文件。

默认输入/输出：

| 项 | 默认值 | 说明 |
| --- | --- | --- |
| 输入目录 | `dxf_files/test/` | 批量读取该目录下的 `*.dxf` / `*.DXF` |
| 输出分组 | `outputs/test/` | 若已存在，则自动改为 `outputs/test1/`、`outputs/test2/` 等 |
| JSON 汇总 | `outputs/<分组>/summary.json` | 结构化记录每个 DXF 的状态、指标、LLM 理解和产物路径 |
| CSV 汇总 | `outputs/<分组>/summary.csv` | 便于表格导出，只包含 `name`、`llm_model`、`elapsed_s`、`input_coverage`、`hit_ratio` |
| FCStd 汇总拷贝 | `outputs/<分组>/<part>.FCStd` | 若单次运行成功，会把对应 FCStd 复制到批量输出目录 |

常用示例：

```bash
# 使用默认 dxf_files/test/ 作为输入，输出到 outputs/test*/
./run_contrast_batch.sh

# 指定输入目录和输出分组名
DXF_3D_BATCH_INPUT_DIR=dxf_files/test \
DXF_3D_BATCH_OUTPUT_SUBDIR=contrast \
./run_contrast_batch.sh

# 批量时切换模型 profile
./run_contrast_batch.sh --qwen
./run_contrast_batch.sh --gpt

# 批量时附加建模意图弱提示
./run_contrast_batch.sh --intent "按三视图证据优先保证孔槽贯穿关系"

# 使用指定配置文件
./run_contrast_batch.sh --config config.json
```

支持转发给 `run.sh` 的选项：`--qwen`、`--gpt`、`--openai`、`--val`、`--no-llm`、`--intent` / `--model-intent`、`--config`。

运行前需要满足：`run.sh` 可执行、`config.json` 存在、Docker 可用、镜像 `dxf-3d` 已构建。镜像名可继续用 `DXF_3D_IMAGE` 覆盖；容器命令可用 `DXF_3D_CONTAINER_CLI` 覆盖。

`summary.json` 的顶层结构为：

```json
{
  "output_dir": "outputs/test",
  "count": 1,
  "results": [
    {
      "name": "00001926",
      "status": "OK",
      "llm_model": "qwen3.5-35b-a3b",
      "elapsed_s": 12.345,
      "input_coverage": {"front": "100.0%", "left": "100.0%", "top": "100.0%"},
      "hit_ratio": {"front": "100.0%", "left": "100.0%", "top": "100.0%"},
      "missing": {"front": "0.0%", "left": "0.0%", "top": "0.0%"},
      "extra": {"front": "0.0%", "left": "0.0%", "top": "0.0%"},
      "llm": {"understanding": "LLM 对模型结构的理解"},
      "fcstd": "outputs/test/00001926.FCStd",
      "run_dir": "/app/DXF_3D/outputs/test/..."
    }
  ]
}
```

`summary.csv` 仅用于导出和对比，列固定为：

```text
name,llm_model,elapsed_s,input_coverage,hit_ratio
```

---

## 3. 输入 DXF 约定

### 三视图布局

图纸布局固定为：

```text
+------------------+------------------+
| FRONT 主视图     | LEFT 左视图      |
| 左上             | 右上             |
+------------------+------------------+
| TOP 俯视图       | 空               |
| 左下             |                  |
+------------------+------------------+
```

坐标映射：

| 视图 | 位置 | 对应 3D 平面 | 草图坐标到世界坐标 |
| --- | --- | --- | --- |
| `front` | 左上 | XZ，`Y=0` | `x → X`，`y → Z` |
| `top` | 左下 | XY，`Z=0` | `x → X`，`y → Y` |
| `left` | 右上 | YZ，`X=0` | `x → Y`，`y → Z` |

尺寸命名：

- `W`：宽度，沿 X
- `D`：深度，沿 Y
- `H`：高度，沿 Z

### 支持的实体

用于建模的主要实体：

- `LINE`
- `ARC`
- `CIRCLE`
- `LWPOLYLINE`
- `POLYLINE`

会被解析但一般不作为外轮廓直接建模的实体：

- `TEXT` / `MTEXT`
- `DIMENSION`
- `ELLIPSE`
- `SPLINE`
- `HATCH`
- `SOLID`
- `INSERT`

### 图层建议

| 图层 | 含义 | 处理方式 |
| --- | --- | --- |
| `OUTLINE` / `0` | 可见轮廓 | 用于建模 |
| `HIDDEN` / `*_HID` | 隐藏线 | 作为孔、盲孔、内部切除证据，不直接作为实体外轮廓 |
| `CENTER` | 中心线 | 忽略或弱化 |
| `DIM` | 尺寸标注 | 解析为辅助信息，不作为轮廓 |

推荐隐藏线命名：`FRONT_HID`、`LEFT_HID`、`TOP_HID`。

### 主要限制

- 不支持多于三视图、剖视图、局部放大图、辅助视图、断面视图。
- 不保证复杂自由曲面、螺纹、沉孔、复杂圆角和阵列特征。
- 外轮廓应尽量闭合；明显不闭合会影响上下文生成和建模质量。
- 右下象限不参与建模。
- `SPLINE`、`ELLIPSE`、`HATCH` 目前不作为主体外轮廓直接建模。

---

## 4. 终端输出与日志

终端只显示核心摘要，例如：

```text
LLM         : qwen3.5-35b-a3b
Projection   : WARN
  FRONT WARN input_coverage= 82.1% missing= 17.9% hit_ratio=100.0% extra=  0.0%
  LEFT  WARN input_coverage= 75.6% missing= 24.4% hit_ratio=100.0% extra=  0.0%
  TOP   OK   input_coverage=100.0% missing=  0.0% hit_ratio= 99.6% extra=  0.4%
Output dir  : /home/zcf/DXF_3D/outputs/qwen3.5-35b-a3b_20260609_102742_nut
Status      : OK
```

`Projection` 只在使用 `--val` 时打印。含义：

| 指标 | 含义 |
| --- | --- |
| `input_coverage` | 输入视图中有多少比例被模型投影覆盖 |
| `missing` | 输入视图中未被模型覆盖的比例，约等于 `1 - input_coverage` |
| `hit_ratio` | 模型投影中有多少比例能被输入视图解释 |
| `extra` | 模型投影中多出来、输入视图没有证据的比例 |

详细过程全部写入输出目录的 `run.log`。

---

## 5. 项目文件与目录职责

### 根目录文件

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目使用说明、输入输出约定、目录说明 |
| `PIPELINE.md` | 更细的阶段级流水线说明 |
| `CLAUDE.md` | 编码行为约束和协作注意事项 |
| `Dockerfile` | 构建 FreeCAD + Python 依赖 + 项目代码的运行镜像 |
| `requirements.txt` | Python 依赖，目前主要是 `matplotlib` 和 `openai` |
| `config.json` | LLM profile 配置；包含敏感 key，提交和分享前应脱敏 |
| `run.sh` | Docker 启动脚本，负责挂载输入/输出/config 并调用容器内入口 |
| `run.py` | Python 根入口，转发到 `direct/code/run.py` 的 `main()` |
| `run_contrast_batch.sh` | 批量对比运行脚本，用于调试/实验输出分组 |
| `dxf_loader.py` | 纯 Python DXF 解析器，解析实体、图层、bbox、单位等 |
| `view_classifier.py` | 三视图分类器，把实体按布局分为 `front`、`top`、`left` |
| `projection_mapper.py` | 把 2D 视图实体归一化并映射到 3D 平面 |
| `geometry_estimator.py` | 轮廓闭环、尺寸估计、基础几何摘要工具 |
| `llm_client.py` | OpenAI 兼容 LLM 客户端和 prompt 模板渲染工具 |
| `sketch_recognizer_code.py` | 草图/轮廓识别相关实验或辅助代码 |
| `val_cache_policy.md` | `--val` 成功脚本缓存策略说明 |

### 运行时目录

| 目录 | 作用 |
| --- | --- |
| `dxf_files/` | 默认输入目录，放待处理 `.dxf` 文件 |
| `outputs/` | 默认输出目录，每次运行一个独立子目录 |

### `llm/`

默认路线相关代码和提示词。

| 路径 | 作用 |
| --- | --- |
| `llm/README.md` | LLM 路线说明 |
| `llm/code/llm_code_planner.py` | 构造 `auto_context.json`、请求 LLM 生成 FreeCAD 脚本、静态校验、执行失败修复、兜底脚本生成 |
| `llm/code/hlr_exporters.py` | 导出模型隐藏线/三视图 PNG 的辅助逻辑 |
| `llm/prompts/freecad_script_generator.md` | 生成 FreeCAD Python 脚本的主提示词 |
| `llm/prompts/auto_modeling_strategy.md` | LLM 建模策略提示词 |

### `direct/`

历史确定性特征路线和当前仍复用的 FreeCAD/导出逻辑。当前命令行默认不暴露独立 direct 路线。

| 路径 | 作用 |
| --- | --- |
| `direct/README.md` | direct 目录说明 |
| `direct/code/run.py` | 当前实际 CLI 编排器：解析参数、创建输出目录、串联各阶段 |
| `direct/code/exporters.py` | STEP、OBJ、PNG、overview、`model.json` 和投影验证导出器 |
| `direct/code/freecad_builder.py` | FreeCAD 辅助逻辑；默认路线复用投影视图嵌入函数 |
| `direct/code/feature_inference.py` | 历史确定性特征推断代码，当前默认路线不依赖其生成 `features.json` |
| `direct/code/llm_planner.py` | 历史 direct 路线的视图复核/特征精修 LLM 辅助代码 |
| `direct/prompts/` | 历史 direct 路线提示词 |

### `prompts/`

公共知识和约定。

| 文件 | 作用 |
| --- | --- |
| `prompts/part_knowledge.md` | 常见机械零件族、孔槽、连接件等语义知识 |
| `prompts/view_conventions.md` | 三视图布局、坐标映射和视图语义约定 |

### 其他辅助目录

| 目录 | 作用 |
| --- | --- |
| `3D2DXF/` | STEP 到 DXF 的辅助转换/样例目录，用于反向生成或准备测试图纸 |
| `dxf_class_coverage/` | DXF 类别覆盖率统计和批量分析工具 |

---

## 6. 流水线阶段

| 阶段 | 模块 | 输入 | 输出 |
| --- | --- | --- | --- |
| 0. 启动 | `run.sh`、`run.py`、`direct/code/run.py` | 命令行参数、环境变量、`config.json` | 目标 DXF 列表、输出目录、日志器、LLM 客户端 |
| 1. DXF 解析 | `dxf_loader.py` | `.dxf` 文本 | `entities.json`、实体列表、元数据 |
| 2. 视图分类 | `view_classifier.py` | 实体列表 | `views_algorithm.json`、`views.json` |
| 3. 投影映射 | `projection_mapper.py` | `front/top/left` 视图 | 归一化 2D/3D 投影实体 |
| 4. 上下文生成 | `llm/code/llm_code_planner.py` | 视图、投影、几何摘要、意图提示 | `auto_context.json` |
| 5. LLM 脚本生成 | `llm/code/llm_code_planner.py` | `auto_context.json`、prompt | `generated_model.py` |
| 6. FreeCAD 执行 | `generated_model.py` | LLM 生成脚本 | `<base>.FCStd`、尺寸/圆弧校验 JSON |
| 7. 导出 | `direct/code/exporters.py`、`llm/code/hlr_exporters.py` | `.FCStd`、投影信息 | STEP、OBJ、PNG、`model.json`、可选投影验证 |

---

## 7. LLM 配置

`config.json` 使用 profile 结构：

```json
{
  "active": "qwen",
  "profiles": {
    "qwen": {
      "api_key": "...",
      "base_url": "http://example/v1",
      "model": "qwen3.5-35b-a3b",
      "api_mode": "chat"
    },
    "gpt": {
      "api_key": "...",
      "base_url": "https://example/v1",
      "model": "gpt-5.5",
      "api_mode": "responses"
    }
  }
}
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `active` | 默认使用的 profile 名称 |
| `profiles.<name>.api_key` | API key，敏感信息 |
| `profiles.<name>.base_url` | OpenAI 兼容接口地址 |
| `profiles.<name>.model` | 模型名称 |
| `profiles.<name>.api_mode` | `chat` 或 `responses` |

运行时可用：

- `--qwen`：使用 `qwen` profile
- `--gpt`：使用 `gpt` profile
- `--openai`：使用 `openai` profile
- `DXF_3D_CONFIG_PROFILE=name`：通过环境变量指定 profile
- `--no-llm` 或 `DXF_3D_DISABLE_LLM=1`：禁用 LLM 调用

注意：`config.json` 包含密钥，分享项目或提交代码前必须脱敏。

---

## 8. 部署到另一台主机

### 方案 A：拷贝源码并在目标主机构建镜像

```bash
tar czf dxf_3d.tar.gz DXF_3D/
scp dxf_3d.tar.gz user@host:/path/
ssh user@host "cd /path && tar xzf dxf_3d.tar.gz"

ssh user@host
cd /path/DXF_3D
docker build -t dxf-3d .
./run.sh -d dxf_files/nut.dxf
```

### 方案 B：离线导出镜像

源主机：

```bash
cd DXF_3D
docker build -t dxf-3d .
docker save dxf-3d | gzip > dxf-3d.tar.gz
```

目标主机：

```bash
docker load < dxf-3d.tar.gz
cd /path/to/DXF_3D
./run.sh -d dxf_files/nut.dxf
```

---

## 9. 开发与维护注意事项

- 不引入外部 DXF 解析库；`dxf_loader.py` 是当前公共解析器。
- FreeCAD 相关导入应保持在函数内部或生成脚本内，避免普通 Python 环境 import 失败。
- 不修改固定三视图布局：`FRONT` 左上、`TOP` 左下、`LEFT` 右上。
- 不修改坐标映射：`FRONT → XZ`、`TOP → XY`、`LEFT → YZ`。
- 不删除 `*D\d+` 匿名块过滤；这类块通常来自尺寸标注箭头/引线，会污染视图聚类。
- 不提交明文 API key。
- 输出对象名应保持兼容导出器：`Result`、`DXF_FRONT`、`DXF_TOP`、`DXF_LEFT`。
- `dxf_files/` 和 `outputs/` 是运行时 I/O 目录，不要删除目录本身。

常见排查入口：

| 问题 | 优先检查 |
| --- | --- |
| 视图分类错误 | `<output>/views_algorithm.json`、`<output>/<base>.png` |
| 模型尺寸不对 | `<output>/auto_context.json`、`dimension_validation.json` |
| 模型缺线/多线 | 使用 `--val` 后查看 `projection_validation.json` 和 `<base>_model_views.png` |
| LLM 生成脚本失败 | `run.log`、`generated_model.py`、LLM 原始响应文件 |
| 普通 Python 无法导入 FreeCAD | 使用 Docker 或 `freecadcmd` 运行 |
