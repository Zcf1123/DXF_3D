# Val 验证缓存实现说明

## 1. 功能目标

`--val` 模式下，系统会在生成 3D 模型后执行反投影验证：

1. 将最终 `.FCStd` 中的 `Result` 实体重新投影为 `FRONT` / `LEFT` / `TOP` 三个二维视图。
2. 将模型投影结果与输入 DXF 三视图逐项比对。
3. 只有三个视图的模型输出匹配率都达到高精度门槛时，才把本次 LLM 生成的 FreeCAD 脚本写入缓存。

缓存的作用是：后续遇到相同模型、相同提示词、相同几何上下文时，可以直接复用已验证成功的脚本，减少 LLM 调用并提高结果稳定性。

---

## 2. 缓存文件存放位置

LLM 成功脚本缓存统一保存在：

```text
outputs/.llm_script_cache/
```

每个缓存文件是一个 Python 脚本文件，文件名为：

```text
<cache_key>.py
```

其中 `cache_key` 是 SHA-256 哈希值，用来唯一标识一次可复用的建模输入。

---

## 3. 缓存 key 的生成方式

缓存 key 由以下内容共同生成：

| 字段 | 说明 |
| --- | --- |
| `model` | 当前使用的 LLM 模型名称，例如 `qwen3.5-35b-a3b` |
| `base_name` | 输入 DXF 的基础文件名 |
| `prompt_system` | FreeCAD 脚本生成提示词的 system 部分 |
| `prompt_user_template` | FreeCAD 脚本生成提示词的 user 模板 |
| `context` | 自动构造的三视图几何上下文 `auto_context` |

生成流程：

```text
payload = model + base_name + prompt + auto_context
cache_key = sha256(json(payload))
```

这样可以保证：

- 模型变了，不会误用旧缓存；
- 提示词变了，不会误用旧缓存；
- DXF 几何上下文变了，不会误用旧缓存；
- 文件名不同，也不会混用缓存。

---

## 4. 缓存读取流程

在 Auto LLM 路线中，生成 FreeCAD 脚本前会先尝试读取缓存。

流程如下：

1. 根据当前 `llm`、`base_name`、提示词和 `auto_context` 计算 `cache_key`。
2. 在 `outputs/.llm_script_cache/` 中查找：

   ```text
   <cache_key>.py
   ```

3. 如果缓存文件存在，读取脚本内容。
4. 将脚本中的运行时常量重定向为当前任务的值：

   | 常量 | 重定向内容 |
   | --- | --- |
   | `BASE_NAME` | 当前输入文件基础名 |
   | `FCSTD_PATH` | 当前输出目录下的 `.FCStd` 路径 |

5. 对缓存脚本执行静态校验。
6. 校验通过后，直接复用缓存脚本，不再请求 LLM。

缓存命中时日志会显示：

```text
复用 LLM 成功脚本缓存（<model>）
```

同时会在当前输出目录写出：

```text
llm_cache_key.txt
```

用于记录本次命中的缓存 key。

---

## 5. 缓存创建流程

缓存只在 `--val` 验证模式下创建。

当前实现流程：

1. LLM 生成 FreeCAD 脚本。
2. 程序执行脚本，生成 `.FCStd`。
3. 执行尺寸契约校验，确保模型整体 `width_x` / `depth_y` / `height_z` 符合三视图尺寸约束。
4. 如输入轮廓含 ARC，执行圆弧边校验，确保模型保留真实圆弧边。
5. 执行反投影验证，生成：

   ```text
   projection_validation.json
   ```

6. 判断三个视图是否都达到缓存保存门槛。
7. 通过门槛后，调用缓存写入逻辑，将最终 FreeCAD 脚本保存到：

   ```text
   outputs/.llm_script_cache/<cache_key>.py
   ```

如果没有使用 `--val`，不会保存缓存。

日志示例：

```text
LLM 脚本缓存  : 跳过（命令行加 --val 才保存）
```

验证通过并保存缓存时日志示例：

```text
LLM 脚本缓存  : 已记录成功脚本（--val，三个视图 output 均 ≥99%）
```

---

## 6. 反投影验证的计算方式

反投影验证以视图为单位分别计算：

```text
FRONT
LEFT
TOP
```

每个视图都会执行以下步骤。

### 6.1 输入线段提取

从输入 DXF 对应视图中提取二维线段：

| DXF 实体 | 处理方式 |
| --- | --- |
| `LINE` | 直接作为一条线段 |
| `CIRCLE` | 离散为多条短线段 |
| `ARC` | 按角度离散为多条短线段 |
| `LWPOLYLINE` / `POLYLINE` | 按相邻点拆分为线段 |

得到输入线段集合：

```text
input_segments
```

### 6.2 模型线段提取

程序打开最终 `.FCStd`，读取 `Result.Shape`，并将模型边线分别投影到：

| 视图 | 投影平面 |
| --- | --- |
| `FRONT` | XZ |
| `LEFT` | YZ |
| `TOP` | XY |

得到模型线段集合：

```text
model_segments
```

### 6.3 采样容差

每个视图按自身尺寸计算匹配容差：

```text
tolerance = max(max(view_width, view_height) * 0.02, 1e-6)
```

也就是视图最大尺寸的 2%，最小不低于 `1e-6`。

### 6.4 采样匹配

系统会对输入线段和模型线段分别采样：

```text
input_samples = sample(input_segments, tolerance)
model_samples = sample(model_segments, tolerance)
```

随后计算两个方向的匹配率。

---

## 7. 准确率指标

### 7.1 `input_coverage`

`input_coverage` 表示输入图纸中有多少内容被模型投影覆盖。

计算方式：

```text
input_coverage = matched(input_samples, model_segments, tolerance) / len(input_samples)
```

含义：

```text
输入图纸线段采样点中，有多少比例能在模型投影线段附近找到匹配
```

它主要衡量：

```text
是否漏建
```

如果 `input_coverage` 低，说明输入图纸中有一部分结构没有出现在模型投影中。

### 7.2 `model_match`

`model_match` 表示模型输出中有多少内容能被输入图纸解释。

计算方式：

```text
model_match = matched(model_samples, input_segments, tolerance) / len(model_samples)
```

含义：

```text
模型投影线段采样点中，有多少比例能在输入图纸线段附近找到匹配
```

它主要衡量：

```text
是否多建
```

如果 `model_match` 低，说明模型中存在输入图纸没有体现的额外结构。

### 7.3 `model_extra_ratio`

`model_extra_ratio` 是模型多余比例：

```text
model_extra_ratio = 1.0 - model_match
```

例如：

```text
model_match = 0.96
model_extra_ratio = 0.04
```

表示模型投影中约 4% 的内容无法被输入图纸解释。

---

## 8. 缓存保存准确率门槛

当前缓存保存采用模型输出匹配率门槛。

每个视图都必须满足：

```text
model_match >= 0.99
```

也就是：模型投影出来的线，至少 99% 都能在输入图纸中找到对应线段。

缓存保存条件为：

```text
FRONT model_match >= 0.99
LEFT  model_match >= 0.99
TOP   model_match >= 0.99
```

只有三个视图的 `model_match` 全部达到 99%，才会保存缓存文件。

`input_coverage` 仍会写入验证报告，用于观察输入图纸被模型覆盖的比例，但不参与缓存保存判断。

示例：

```text
FRONT input_coverage=59.1% model_match=100.0%
LEFT  input_coverage=59.1% model_match=100.0%
TOP   input_coverage=100.0% model_match=100.0%
```

这种情况下，三个视图的 `model_match` 都达到 99%，因此允许保存缓存。

---

## 9. 结果文件

启用 `--val` 后，每次运行会在当前输出目录生成：

```text
projection_validation.json
```

其中包含：

| 字段 | 说明 |
| --- | --- |
| `status` | 整体验证状态，`OK` 或 `WARN` |
| `views.front` | FRONT 视图验证结果 |
| `views.left` | LEFT 视图验证结果 |
| `views.top` | TOP 视图验证结果 |
| `input_coverage` | 输入覆盖率 |
| `model_match` | 模型匹配率 |
| `model_extra_ratio` | 模型多余比例 |
| `bbox_error` | 输入与模型投影 bbox 差异 |
| `unmatched_input_segments` | 未被模型覆盖的输入线段 |

---

## 10. 当前实现总结

当前缓存机制遵循以下原则：

1. 只在 `--val` 模式下保存缓存。
2. 读取缓存时，必须由相同模型、相同提示词、相同几何上下文生成相同 cache key。
3. 缓存命中后，会重定向输出路径，保证脚本写入当前运行目录。
4. 缓存脚本复用前仍会执行静态校验。
5. 缓存写入前必须完成反投影验证。
6. 三个视图的 `model_match` 都达到 99% 时，才保存缓存。

该策略保证缓存只记录高可信的 LLM 建模脚本，避免后续运行复用低质量结果。
