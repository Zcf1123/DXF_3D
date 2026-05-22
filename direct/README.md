# Direct 路线

本目录存放当前稳定的确定性 DXF 到 FreeCAD 建模路线。
DXF 解析、视图分类、投影和几何摘要等公共能力放在仓库根目录，
本目录只保留 direct 路线特有的特征推断、FreeCAD builder、导出和入口编排。

从仓库根目录运行：

```bash
./run.sh -d dxf_files/00005340.dxf
```

目录内容：

- `code/`：direct 路线入口、特征推断、FreeCAD builder、导出器，以及旧公共模块路径的兼容 wrapper。
- `prompts/`：当前受控路线使用的提示词。

设计边界：

- 本路线先生成 `features.json`，再由 `freecad_builder.py` 用 FreeCAD API 解释这些特征。
- 它是稳定兜底行为，不是长期堆放所有新零件族规则的地方。