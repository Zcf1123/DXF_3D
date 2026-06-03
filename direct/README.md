# direct 目录

本目录保留历史确定性特征路线代码，以及当前默认 LLM 路线仍复用的部分 FreeCAD
辅助逻辑和导出器。命令行已经不再提供 `--direct` 模式；从仓库根目录运行时只走
默认 LLM 直接建模路线。

从仓库根目录运行：

```bash
./run.sh -d dxf_files/00005340.dxf
```

目录内容：

- `code/run.py`：当前 CLI 编排器，默认调用 LLM 直接建模路线。
- `code/freecad_builder.py`：默认路线复用其中的投影视图嵌入逻辑。
- `code/exporters.py`：默认路线复用的 STEP / OBJ / PNG / model.json 导出器。
- `code/feature_inference.py`、`code/llm_planner.py`、`prompts/`：历史 direct 路线保留代码，当前命令行不启用。

设计边界：

- 新功能优先改进 `llm/` 路线、公共几何摘要和导出逻辑。
- 不要继续向历史 direct 特征推断里堆放新零件族规则，除非先恢复并明确需要该路线。