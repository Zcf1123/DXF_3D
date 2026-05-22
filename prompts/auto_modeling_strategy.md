# 自动建模策略

这些知识用于 LLM 直接编写 FreeCAD 代码的路线。

## 目标

根据三视图 JSON 摘要和图片摘要，直接生成 FreeCAD Python 建模脚本。

## 推荐推理顺序

- 先根据视图布局、拟合曲线、闭合轮廓、隐藏线和可选用户意图识别主要零件类型。
- 先构造正实体：圆柱、盒体、拉伸轮廓，以及需要融合的加性组件。
- 正实体融合后再做切除：圆孔、长圆孔、矩形/异形切除和盲切。
- 用 TOP 和 LEFT 判断局部深度与偏移；不要默认把每个 FRONT 组件都拉满总深度。
- 拟合曲线优先使用稳定的 FreeCAD 基元：`Part.makeCylinder`、`Part.Circle`、`Part.Arc`、`Part.Face`、`cut` 和 `fuse`。

## 输出约束

- 只输出完整 Python 代码。
- 创建最终 `Part::Feature` 对象，名称必须为 `Result`。
- 把最终 solid 赋给 `Result.Shape`。
- 调用 `doc.recompute()` 和 `doc.saveAs(FCSTD_PATH)`。
- 不要使用 shell、网络、文件删除、`eval` 或 `exec`。