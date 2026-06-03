# LLM 路线

本目录存放原始目标中的 LLM 直接建模路线：

```text
DXF -> 三视图 JSON/投影几何摘要 -> auto_context.json -> LLM 编写 FreeCAD Python -> 执行并导出
```

从仓库根目录运行：

```bash
./run.sh -d dxf_files/00005340.dxf
```

目录内容：

- `code/llm_code_planner.py`：默认路线下由 `direct/code/run.py` 调用的 LLM 脚本生成辅助模块。
- `prompts/freecad_script_generator.md`：直接生成 FreeCAD 脚本的提示词。
- `prompts/auto_modeling_strategy.md`：LLM 直接建模路线专用策略说明。

设计边界：

- `entities.json` 会落盘供调试查看；LLM 实际收到的是由解析结果、三视图分类和投影结果压缩生成的 `auto_context.json`。
- 本路线应优先改进 JSON/图片摘要、提示词质量和自动修复循环。
- 当目标是让 LLM 负责编写建模代码时，避免继续把新零件族规则堆进 `feature_inference.py`。