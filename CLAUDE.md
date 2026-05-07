# CLAUDE.md — DXF_3D 项目速查手册

> 本文件是给 Claude 看的上下文备忘录。把 `DXF_3D/` 拷贝到任意主机后，
> 先读这里，再看代码。

---

## 1. 项目一句话描述

把 `.dxf` 工程图（标准三视图：FRONT / TOP / RIGHT）解析并重建为 FreeCAD 3D 实体，
输出 `.FCStd` / `.step` / `.obj` / `.png` 预览及各阶段 JSON 产物。

---

## 2. 目录结构

```
DXF_3D/
├── run.py                  # 顶层入口，main() 供 freecadcmd 调用
├── run.sh                  # Docker 启动脚本（独立部署用）
├── Dockerfile              # 自包含镜像（FreeCAD + matplotlib + openai）
├── requirements.txt        # Python 依赖（仅 matplotlib + openai）
├── config.json             # LLM 连接配置，运行时挂载读取
│
├── dxf_loader.py           # 纯 Python DXF 解析，无 ezdxf 依赖
├── view_classifier.py      # 把实体聚类到 front/top/right 三视图
├── projection_mapper.py    # 2D 视图 → 3D 坐标系映射
├── geometry_estimator.py   # 从视图轮廓 + DIMENSION 实体估算零件尺寸
├── feature_inference.py    # 算法路径：推断 extrude_profile / hole 等特征
├── llm_planner.py          # LLM 路径：调用 OpenAI API 精化特征列表
├── freecad_builder.py      # 用 FreeCAD API 构建 3D 实体
├── exporters.py            # 导出 STEP / OBJ / PNG / model.json / generated_model.py
│
├── prompts/
│   ├── PROMPT_SPEC.md      # Prompt 文件格式规范（必读）
│   └── feature_refiner.md  # LLM 特征精化的 system/user 模板
│
├── dxf_files/              # 输入目录，放待处理的 .dxf（不纳入 Git）
└── outputs/                # 输出目录，按 <YYYYMMDD>_<HHMMSS>_<base>/ 分目录（不纳入 Git）
```

---

## 3. 数据流（Pipeline）

```
.dxf 文件
   │
   ▼  dxf_loader.py
List[DxfEntity]          ← LINE / CIRCLE / ARC / LWPOLYLINE / DIMENSION …
   │
   ▼  view_classifier.py
ViewBundle × 3           ← front (TL) / right (TR) / top (BL)
   │
   ▼  projection_mapper.py
ProjectedView × 3        ← 统一到 XZ / XY / YZ 坐标系
   │
   ▼  geometry_estimator.py
Outline + 尺寸 (W/D/H)   ← 优先读 DIMENSION 标注，回退用 bbox
   │
   ▼  feature_inference.py  ─── (算法路径，无 LLM)
List[Feature]
   │  ▲
   │  │  llm_planner.py（可选，任何失败回退算法路径）
   │  └─ LLMPlanner.refine_features()
   │
   ▼  freecad_builder.py    ← 需要 freecadcmd 环境
.FCStd 文件
   │
   ▼  exporters.py
.step / .obj / .png / model.json / generated_model.py / entities.json / views.json / features.json
```

---

## 4. 运行方式

### 4.1 Docker（推荐，自包含）

```bash
# 构建镜像（在 DXF_3D/ 目录下）
docker build -t dxf-3d .

# 处理 dxf_files/ 下所有 DXF
./run.sh

# 处理指定文件
./run.sh dxf_files/Drawing1.dxf
./run.sh /absolute/path/to/some.dxf   # 外部文件自动挂载
```

镜像名可用环境变量覆盖：`DXF_3D_IMAGE=my-image ./run.sh`

### 4.2 本地直接运行（需要系统安装 FreeCAD）

```bash
# 处理所有 DXF
freecadcmd -c "import sys; sys.path.insert(0,'/app'); from DXF_3D.run import main; sys.exit(main())"

# 处理指定文件（从项目仓库根目录）
freecadcmd -c "import sys; sys.path.insert(0,'/app'); from DXF_3D.run import main; sys.exit(main(['/app/DXF_3D/dxf_files/Drawing1.dxf']))"
```

注意：`sys.path` 必须包含 `/app`（即仓库根目录），因为模块以 `DXF_3D.xxx` 形式引用。

### 4.3 禁用 LLM（纯算法）

把 `config.json` 的 `openai_api_key` 置为空字符串或删除，流水线自动回退到算法路径，
终端打印 `LLM disabled: <reason>`，正常产出所有文件。

---

## 5. 配置文件（config.json）

```json
{
  "openai_api_key": "sk-...",          // OpenAI 兼容 API key
  "openai_base_url": "http://...",     // 内网或代理地址
  "openai_model": "qwen3.5-35b-a3b"   // 模型名，任意 OpenAI 兼容模型
}
```

- 运行时从 `DXF_3D/config.json` 读取，Docker 通过 volume 挂载注入，**不进镜像**。
- `temperature` 固定为 `0.0`，不在配置中暴露。

---

## 6. 三视图布局约定（固定，不可改）

```
+----------------+----------------+
|  FRONT (TL)    |  RIGHT (TR)    |
+----------------+----------------+
|  TOP   (BL)    |   (empty)      |
+----------------+----------------+
```

`view_classifier.py` 的聚类逻辑硬编码此布局。
坐标系约定（第三角投影，Z-up）：

| 视图  | 平面 | drawing x → | drawing y → |
|-------|------|------------|------------|
| front | XZ   | world X    | world Z    |
| top   | XY   | world X    | world Y    |
| right | YZ   | world Y    | world Z    |

---

## 7. Feature 类型（feature_inference.py）

| kind              | 含义                         |
|-------------------|------------------------------|
| `extrude_profile` | 主体轮廓拉伸，选最复杂视图   |
| `base_block`      | 无闭合轮廓时的包围盒方块兜底 |
| `hole`            | 圆形通孔，boolean cut        |

LLM 通过 `llm_planner.py` 精化此列表，返回相同结构的 JSON。

---

## 8. Prompt 文件格式

`prompts/` 下的 `.md` 文件，强制规范见 `prompts/PROMPT_SPEC.md`：

- 必须含 `## SYSTEM` 和 `## USER` 两个二级标题区块。
- `USER` 中用 `{{ key }}` 占位符，运行时替换。
- 可选 `## EXAMPLES`（few-shot），用 `--- input ---` / `--- output ---` 分隔。
- 加载函数：`llm_planner.load_prompt(name)` —— `name` 是文件名去掉 `.md`。

---

## 9. 输出产物说明

每次运行生成 `outputs/<YYYYMMDD>_<HHMMSS>_<base>/`，包含：

| 文件                   | 含义                                         |
|------------------------|----------------------------------------------|
| `<base>.FCStd`         | FreeCAD 项目（含三视图草图 + 3D 实体）       |
| `<base>.step`          | STEP 格式导出                                |
| `<base>.obj`           | OBJ 网格导出                                 |
| `<base>.png`           | 三视图 matplotlib 预览（无需 FreeCADGui）    |
| `entities.json`        | 解析出的 DXF 实体列表                        |
| `views.json`           | 视图分类结果                                 |
| `features_draft.json`  | 算法推断的原始特征（LLM 精化前）             |
| `features.json`        | 最终使用的特征（LLM 精化后，或与 draft 相同）|
| `model.json`           | FreeCAD 对象摘要（类型/体积/面积等）         |
| `generated_model.py`   | 可独立运行的复现脚本                         |
| `run.log`              | 详细日志（含 LLM 请求/响应、异常）           |

---

## 10. 编码规范

- **无外部 DXF 库**：`dxf_loader.py` 是纯 Python 手写解析器，禁止引入 `ezdxf`。
- **FreeCAD 延迟导入**：所有 `import FreeCAD` / `import Part` / `import Mesh` 只在函数内部出现，确保模块在普通 Python 环境可被 `import`（用于测试/调试）。
- **LLM 失败永不中断**：`llm_planner.py` 的所有异常必须捕获，回退算法路径，写 `run.log`。
- **Prompt 只改 `prompts/` 目录**：不在 Python 代码里拼接 system/user 字符串。
- **`*Dn` 匿名块不引入几何**：`dxf_loader.py` 中用 `re.compile(r"^\*D\d+$")` 正则跳过所有维度装饰块（引线、箭头、标注文字）。维度数值从 `DIMENSION` 实体的 `dim_measurement` 字段读取。**不得删除此过滤**，否则标注实体会污染视图聚类和轮廓提取。

---

## 11. 禁区（不可随意改动）

| 禁区 | 原因 |
|------|------|
| `config.json` 中的 `openai_api_key` | 明文密钥，禁止打印到终端，禁止提交公开仓库 |
| `view_classifier.py` 的三视图位置映射（TL/BL/TR） | 硬编码约定，改动会导致所有 DXF 分类错乱 |
| `projection_mapper.py` 的坐标轴映射（front→XZ 等） | 与 `freecad_builder.py` 的平面选择强绑定 |
| `freecad_builder.py` 中 FreeCAD 对象命名（`"Result"` / `"DXF_FRONT"` 等） | `exporters.py` 按名字查找对象 |
| `dxf_files/` 和 `outputs/` 目录 | 运行时 I/O，不纳入 Git，不得删除目录本身 |
| `Dockerfile` 中 `PYTHONPATH=/app` 和 `WORKDIR /app/DXF_3D` | 模块以 `DXF_3D.xxx` 引用，路径变动导致 ImportError |

---

## 12. 常见问题

**Q: 带标注的 DXF 输出形状异常（视图 bbox 偏大、轮廓错误）**  
A: 根本原因是 DXF 每个 `DIMENSION` 标注都关联一个 `*Dn` 匿名块，里面是引线/箭头/文字的 LINE/SOLID 实体。`dxf_loader.py` 通过 `*D\d+` 正则跳过这些块。如果问题复现，检查该过滤是否完整，以及有无其他命名格式的维度块。

**Q: `FreeCAD is not importable` 错误**  
A: 必须用 `freecadcmd` 运行，普通 `python3` 没有 FreeCAD。用 Docker 是最简方式。

**Q: LLM 一直 disabled**  
A: 检查 `config.json` 的 `openai_base_url` 是否可达，`openai_api_key` 是否非空。详细报错在 `run.log`。

**Q: 输出的 .FCStd 没有 3D 实体，只有草图**  
A: `features.json` 里没有 `extrude_profile` 类型的特征，说明所有视图均未找到闭合轮廓。检查 DXF 的线条是否真正首尾相连（`geometry_estimator.py` 的 `_build_graph` 有容差 `1e-6`，太大的 gap 无法识别）。

**Q: 新增一种特征类型**  
A: 在 `feature_inference.py` 的 `Feature.kind` 加新值，在 `freecad_builder.py` 的 `_direct_build` 加对应 `elif` 分支，在 `exporters.py` 的 `generated_model.py` 生成逻辑同步更新。
