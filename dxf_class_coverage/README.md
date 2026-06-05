# DXF Class Coverage

这是一个独立版本，可以直接复制整个 `dxf_class_coverage/` 文件夹到其他主机运行。

不依赖 FreeCAD，不依赖原 `DXF_3D` 项目其他文件；仅使用 Python 标准库。

对每个数字类别文件夹，取该文件夹内按文件名排序后的第一个 `.dxf` 作为基准文件，计算同类中每个 DXF 相对基准的三视图 coverage，并输出 CSV。

## 输入目录示例

```text
classes/
  1/
    00001926.dxf
    00002530.dxf
  2/
    00862427.dxf
    00862697.dxf
```

## 输出列

```csv
class,base,target,coverage,front,top,left
```

其中：

- `class`：数字文件夹名
- `base`：该类排序后的第一个 DXF 文件名
- `target`：当前比较的 DXF 文件名
- `front/top/left`：各视图单向 coverage
- `coverage`：`front/top/left` 的平均值

## 坐标对齐规则

每个 DXF 的 `front/top/left` 视图会分别按自身 bbox 的左下角平移到 `(0, 0)`：

```text
x' = x - xmin
y' = y - ymin
```

这样可以消除三视图在图纸版面中的位置偏移。

不会做尺寸归一化或缩放，因此不同大小的同形状模型仍会被区分。

## 使用

进入 `dxf_class_coverage/` 文件夹运行：

```bash
python batch_coverage.py /path/to/classes -o class_coverage.csv
```

也可以从任意路径运行：

```bash
python /path/to/dxf_class_coverage/batch_coverage.py /path/to/classes -o /path/to/class_coverage.csv
```

## 依赖

Python 3.9+ 即可。

`requirements.txt` 为空依赖说明文件，当前版本不需要安装第三方包。
