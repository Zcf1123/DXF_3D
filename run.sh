#!/usr/bin/env bash
# DXF_3D 启动脚本（独立部署版）
#
# 用法：
#     ./run.sh                         # 处理 ./dxf_files/ 下所有 DXF
#     ./run.sh path/to/file.dxf [...]  # 处理指定 DXF
#     ./run.sh --gpt path/to/file.dxf  # 临时切换到 config.json 中的 gpt profile
#     ./run.sh --extrude-depth 20 path/to/top_view.dxf
#                                      # 单一俯视图按给定长度直接拉伸
#     ./run.sh --no-llm path/to/file.dxf
#                                      # 跳过 LLM 复核，优先提升速度
#     ./run.sh --intent "先拉伸圆柱，再切孔" path/to/file.dxf
#                                      # 给 LLM 一段建模意图弱提示
#     ./run.sh --direct path/to/file.dxf
#                                      # 使用确定性 direct 特征路线
#     ./run.sh -d [file ...]           # 开发模式：挂载本地源码，无需重建镜像；默认 LLM 模式
#
# 镜像名可以用环境变量 DXF_3D_IMAGE 覆盖（默认 dxf-3d）。
# 镜像构建：docker build -t dxf-3d .
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${DXF_3D_IMAGE:-dxf-3d}"
TZ_NAME="${TZ:-Asia/Shanghai}"

show_help() {
        cat <<'EOF'
用法:
    ./run.sh [-d] [模型选项] [流水线选项] [DXF文件...]

常用:
    ./run.sh -d                                  处理 dxf_files/ 下所有 DXF（默认 qwen）
    ./run.sh -d dxf_files/Drawing1.dxf           处理指定 DXF
    ./run.sh -d --gpt dxf_files/00991575.dxf     模型切换模式：本次使用 gpt profile
    ./run.sh -d --qwen dxf_files/00991575.dxf    模型切换模式：本次使用 qwen profile
    ./run.sh -d --gpt --val dxf_files/00991575.dxf
                                                                                             使用 gpt，同时启用反投影验证

模型选项:
    --qwen       模型切换模式：使用 config.json 中的 qwen profile（默认）
    --gpt        模型切换模式：使用 config.json 中的 gpt profile
    --openai     模型切换模式：使用 config.json 中的 openai profile
                             模型选项可与 --val / --direct / --intent 等流水线选项同时使用

运行选项:
    -d                       开发模式：挂载本地源码，改代码后无需重建镜像
    --direct                 使用确定性 direct 特征路线
    --auto                   使用默认 LLM 直接脚本路线
    --no-llm                 禁用 LLM 调用
    --val                    启用反投影验证
    --intent "文本"          给 LLM 建模意图弱提示
    --extrude-depth 数值     单一俯视图沿 Z 方向拉伸长度（配合 --direct）
    --config 路径            指定配置文件，默认 config.json
    -h, --help, -help        显示本帮助
EOF
}

for arg in "$@"; do
        case "$arg" in
                -h|--help|-help)
                        show_help
                        exit 0
                        ;;
        esac
done

# 开发模式标志
DEV_MODE=0
if [[ "${1:-}" == "-d" ]]; then
    DEV_MODE=1
    shift
fi

MODEL_PROFILE="${DXF_3D_CONFIG_PROFILE:-}"
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --qwen)
            MODEL_PROFILE="qwen"
            ;;
        --gpt)
            MODEL_PROFILE="gpt"
            ;;
        --openai)
            MODEL_PROFILE="openai"
            ;;
        *)
            ARGS+=("$arg")
            ;;
    esac
done
set -- "${ARGS[@]}"

# 把每个传入路径拼成 Python 列表字面量；位于 DXF_3D 目录下的文件
# 映射到 /app/DXF_3D/<rel>，外部文件原样保留（需要用户自行提供绝对路径）。
PY_LIST="["
EXTRA_MOUNTS=()
for f in "$@"; do
    if [[ -f "$f" ]]; then
        abs="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
        if [[ "$abs" == "$HERE"/* ]]; then
            rel="${abs#$HERE/}"
            PY_LIST+="'/app/DXF_3D/${rel}',"
        else
            # 容器内挂载该文件所在目录到同名路径，避免改写。
            EXTRA_MOUNTS+=("-v" "$(dirname "$abs"):$(dirname "$abs"):ro")
            PY_LIST+="'${abs}',"
        fi
    else
        PY_LIST+="'${f}',"
    fi
done
PY_LIST+="]"

INNER_CMD="import sys; sys.path.insert(0,'/app'); from DXF_3D.run import main; sys.exit(main(${PY_LIST}))"

# 开发模式额外挂载本地源码目录，覆盖镜像内的代码，无需重建镜像。
if [[ $DEV_MODE -eq 1 ]]; then
    EXTRA_MOUNTS+=("-v" "${HERE}:/app/DXF_3D")
    echo "[dev] 挂载本地源码: ${HERE} -> /app/DXF_3D" >&2
fi

# freecadcmd writes progress noise to stdout; run.py writes the concise
# pipeline summary to stderr, so it remains visible.
exec docker run --rm \
    -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 -e HOME=/var/tmp -e TZ="${TZ_NAME}" \
    -e DXF_3D_OUTPUT_SUBDIR="${DXF_3D_OUTPUT_SUBDIR:-}" \
    -e DXF_3D_DISABLE_LLM="${DXF_3D_DISABLE_LLM:-}" \
    -e DXF_3D_MODEL_INTENT="${DXF_3D_MODEL_INTENT:-}" \
    -e DXF_3D_CONFIG_PROFILE="${MODEL_PROFILE}" \
    -v "${HERE}/dxf_files:/app/DXF_3D/dxf_files" \
    -v "${HERE}/outputs:/app/DXF_3D/outputs" \
    -v "${HERE}/config.json:/app/DXF_3D/config.json:ro" \
    ${EXTRA_MOUNTS[@]+"${EXTRA_MOUNTS[@]}"} \
    -w /app/DXF_3D \
    "${IMAGE}" \
    freecadcmd -c "${INNER_CMD}" >/dev/null
