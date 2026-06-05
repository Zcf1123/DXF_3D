#!/usr/bin/env bash
# 3D2DXF/run.sh — 3D 文件 → DXF 三视图转换
#
# 用法:
#   ./run.sh                         # 转换 ./3d/ 下所有 STEP/STP 文件
#   ./run.sh path/to/step_folder     # 转换指定目录下所有 STEP/STP 文件
#   ./run.sh path/to/model.step      # 转换指定文件（可多个）
#   ./run.sh --hid path/to/model.step # 输出隐藏线
#   ./run.sh -d [files_or_dirs...]   # 开发模式：挂载本地源码，无需重建镜像
#
# 依赖: dxf-3d Docker 镜像（与上级项目共用，镜像名可用 DXF_3D_IMAGE 覆盖）
# 构建镜像: cd .. && docker build -t dxf-3d .
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${DXF_3D_IMAGE:-dxf-3d}"
TZ_NAME="${TZ:-Asia/Shanghai}"

DEV_MODE=0
if [[ "${1:-}" == "-d" ]]; then
    DEV_MODE=1
    shift
fi

EXTRA_MOUNTS=()

if [[ $# -eq 0 ]]; then
    PY_FILES="None"
else
    PY_FILES="["
    for f in "$@"; do
        if [[ "$f" == "--hid" ]]; then
            PY_FILES+="'--hid',"
            continue
        fi
        abs="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"
        if [[ "$abs" == "$HERE"/* ]]; then
            rel="${abs#$HERE/}"
            PY_FILES+="'/work/${rel}',"
        else
            EXTRA_MOUNTS+=("-v" "$(dirname "$abs"):$(dirname "$abs"):ro")
            PY_FILES+="'${abs}',"
        fi
    done
    PY_FILES+="]"
fi

INNER_CMD="import sys; sys.path.insert(0,'/work'); import convert; sys.exit(convert.main(${PY_FILES}))"

if [[ $DEV_MODE -eq 1 ]]; then
    EXTRA_MOUNTS+=("-v" "${HERE}:/work")
    echo "[dev] 挂载本地源码: ${HERE} -> /work" >&2
    exec docker run --rm \
        -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 -e HOME=/var/tmp -e TZ="${TZ_NAME}" \
        ${EXTRA_MOUNTS[@]+"${EXTRA_MOUNTS[@]}"} \
        "${IMAGE}" \
        freecadcmd -c "${INNER_CMD}" >/dev/null
else
    exec docker run --rm \
        -e LANG=C.UTF-8 -e LC_ALL=C.UTF-8 -e HOME=/var/tmp -e TZ="${TZ_NAME}" \
    -v "${HERE}:/work:ro" \
        -v "${HERE}/output:/work/output" \
        ${EXTRA_MOUNTS[@]+"${EXTRA_MOUNTS[@]}"} \
        "${IMAGE}" \
        freecadcmd -c "${INNER_CMD}" >/dev/null
fi
