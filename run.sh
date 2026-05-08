#!/usr/bin/env bash
# DXF_3D 启动脚本（独立部署版）
#
# 用法：
#     ./run.sh                         # 处理 ./dxf_files/ 下所有 DXF
#     ./run.sh path/to/file.dxf [...]  # 处理指定 DXF
#     ./run.sh -d [file ...]           # 开发模式：挂载本地源码，无需重建镜像
#
# 镜像名可以用环境变量 DXF_3D_IMAGE 覆盖（默认 dxf-3d）。
# 镜像构建：docker build -t dxf-3d .
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${DXF_3D_IMAGE:-dxf-3d}"
TZ_NAME="${TZ:-Asia/Shanghai}"

# 开发模式标志
DEV_MODE=0
if [[ "${1:-}" == "-d" ]]; then
    DEV_MODE=1
    shift
fi

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
    -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 -e HOME=/tmp -e TZ="${TZ_NAME}" \
    -v "${HERE}/dxf_files:/app/DXF_3D/dxf_files" \
    -v "${HERE}/outputs:/app/DXF_3D/outputs" \
    -v "${HERE}/config.json:/app/DXF_3D/config.json:ro" \
    ${EXTRA_MOUNTS[@]+"${EXTRA_MOUNTS[@]}"} \
    -w /app/DXF_3D \
    "${IMAGE}" \
    freecadcmd -c "${INNER_CMD}" >/dev/null
