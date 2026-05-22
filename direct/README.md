# Direct 路线

本目录存放当前稳定的确定性 DXF 到 FreeCAD 建模路线。
仓库根目录只保留公开入口脚本；运行时代码放在这里。

从仓库根目录运行：

```bash
./run.sh -d dxf_files/00005340.dxf
```

目录内容：

- `code/`：解析器、视图分类、投影、特征推断、FreeCAD builder、导出器和路线入口。
- `prompts/`：当前受控路线使用的提示词。

设计边界：

- 本路线先生成 `features.json`，再由 `freecad_builder.py` 用 FreeCAD API 解释这些特征。
- 它是稳定兜底行为，不是长期堆放所有新零件族规则的地方。