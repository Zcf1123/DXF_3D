# Prompt 文件规范

`DXF_3D/prompts/` 下的每个 `.md` 文件就是一个可被流水线加载的 prompt。
一个 prompt 文件由若干 `## <SECTION>` 二级标题分块组成。`load_prompt(name)`
会按下列约定解析：

| 区块名      | 是否必需 | 含义 |
| ---------- | -------- | --- |
| `SYSTEM`   | 必需     | 写入 OpenAI `messages[0].role = "system"`。介绍模型角色、硬约束。 |
| `USER`     | 必需     | `messages[1].role = "user"` 模板，可包含 `{{key}}` 占位符。 |
| `OUTPUT`   | 可选     | 描述返回值格式（JSON Schema / fenced 块），仅作文档。 |
| `EXAMPLES` | 可选     | few-shot，若存在会被插入到 `system` 之后、`user` 之前作为 `assistant` 示例（用 `--- input ---` / `--- output ---` 分隔）。 |

## 占位符替换规则

- `{{ key }}` 形式，左右空格忽略。
- 替换前会用 `json.dumps(value, ensure_ascii=False, indent=2)` 序列化非字符串值。
- 找不到占位符不报错，原样保留（便于调试）。

## 模型与温度

模型名取自 `config.json` 的 `openai_model`，`temperature` 固定为 `0.0`。

## 失败回退

任何网络 / 解析失败都不会中断流水线：会把异常写入 `outputs/<run_id>/run.log`，
然后回退到非 LLM 的算法路径。终端只额外打印一行 `LLM disabled: <reason>`。
