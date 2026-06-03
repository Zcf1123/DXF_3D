# 3D2DXF 曲线离散与重新识别说明

## 1. 之前为什么曲线会变成很多小线段

3D2DXF 的投影流程依赖 FreeCAD 完成：

1. 读取模型
   - STEP / IGES：`Part.read()`
   - STL / OBJ：`Mesh.Mesh()` / `mesh.read()`，再通过 `Part.Shape.makeShapeFromMesh()` 转为形体

2. 生成三视图投影
   - 使用 FreeCAD 原始函数：`TechDraw.projectEx()`
   - 分别按前视图、俯视图、左视图方向投影
   - `TechDraw.projectEx()` 会返回可见边和隐藏边，包括 hard edge、smooth edge、outline edge 等

3. 曲线离散
   - 投影边会通过 FreeCAD 的 `edge.discretize()` 采样成一组点
   - 原先每两个相邻采样点都会写成一条 AutoCAD DXF `LINE`
   - 因此圆、圆弧、样条曲线会被拆成大量短线段

结果：

```text
一条曲线 → 多个采样点 → 多条 LINE
```

这会导致 DXF 中线段数量很多，圆看起来像由许多小线段拼成。

---

## 2. 改进：让曲线重新识别为 ARC / CIRCLE / POLYLINE

现在仍然使用 FreeCAD 的原始投影能力：

- `Part.read()` 读取 STEP / IGES
- `TechDraw.projectEx()` 生成三视图投影边
- `edge.discretize()` 获取曲线采样点

改进点在 DXF 输出前：不再把所有采样点直接拆成 `LINE`，而是根据采样点的几何特征重新识别曲线类型。

### 识别规则

| 几何情况 | DXF 输出实体 | AutoCAD 中的意义 |
|---|---|---|
| 点集共线 | `LINE` | 直线段 |
| 点集可拟合为圆且闭合 | `CIRCLE` | 完整圆 |
| 点集可拟合为圆但不闭合 | `ARC` | 圆弧 |
| 复杂曲线，无法稳定拟合为圆 | `POLYLINE` | 连续折线/多段线 |

### AutoCAD 实体含义

- `LINE`：单条直线段，只有起点和终点。
- `ARC`：真正的圆弧实体，由圆心、半径、起止角度定义。
- `CIRCLE`：真正的圆实体，由圆心和半径定义。
- `POLYLINE`：一条连续多段线，多个点属于同一个对象，不再是大量独立线段。

---

## 3. 改进效果

以 `00991575.step` 为例，改进后 DXF 实体统计为：

```text
LINE       112
ARC         16
POLYLINE     6
CIRCLE       2
```

其中俯视图中间闭合圆已经从碎线形式识别为 `CIRCLE`；外圈原本由 FreeCAD 投影返回为多段圆弧，因此保持为多段 `ARC`，不强行合并。

---

## 4. 注意事项

- 曲线识别基于 FreeCAD 投影后的采样点，不改变 FreeCAD 的投影结果。
- 多边形不会被简单误判为圆：只有所有采样点都稳定落在同一圆上时，才输出 `CIRCLE` 或 `ARC`。
- STL / OBJ 本身是网格模型，原始曲面已经离散，曲线恢复效果不如 STEP / IGES。
