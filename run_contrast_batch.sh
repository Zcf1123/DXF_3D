#!/usr/bin/env bash
set -u -o pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
INPUT_DIR="${DXF_3D_BATCH_INPUT_DIR:-${ROOT}/dxf_files/test}"
OUTPUT_SUBDIR="${DXF_3D_BATCH_OUTPUT_SUBDIR:-contrast}"
OUTPUT_DIR="${ROOT}/outputs/${OUTPUT_SUBDIR}"
TIME_FILE="${OUTPUT_DIR}/time.txt"
STEP_DIR="${OUTPUT_DIR}/step"

mkdir -p "${OUTPUT_DIR}"
mkdir -p "${STEP_DIR}"
touch "${TIME_FILE}"

shopt -s nullglob
dxf_files=("${INPUT_DIR}"/*.dxf "${INPUT_DIR}"/*.DXF)
shopt -u nullglob

if (( ${#dxf_files[@]} == 0 )); then
    printf 'No DXF files found in %s\n' "${INPUT_DIR}" >&2
    exit 1
fi

overall_rc=0
total=${#dxf_files[@]}
index=0

for dxf in "${dxf_files[@]}"; do
    index=$((index + 1))
    part="$(basename "${dxf}" .dxf)"
    part="$(basename "${part}" .DXF)"
    batch_log="${OUTPUT_DIR}/${part}.batch.log"

    printf '[%d/%d] %s\n' "${index}" "${total}" "${dxf}" >&2

    started_ns="$(date +%s%N)"
    output="$(DXF_3D_OUTPUT_SUBDIR="${OUTPUT_SUBDIR}" "${ROOT}/run.sh" -d --auto "$@" "${dxf}" 2>&1)"
    rc=$?
    ended_ns="$(date +%s%N)"

    printf '%s\n' "${output}" > "${batch_log}"

    elapsed="$(printf '%s\n' "${output}" | awk -F: '/^Elapsed[[:space:]]*:/ {gsub(/^[ \t]+|s[ \t]*$/, "", $2); print $2; exit}')"
    if [[ -z "${elapsed}" ]]; then
        elapsed="$(awk -v s="${started_ns}" -v e="${ended_ns}" 'BEGIN { printf "%.3f", (e - s) / 1000000000 }')"
    fi

    status="$(printf '%s\n' "${output}" | sed -n 's/^Status[[:space:]]*:[[:space:]]*//p' | tail -n 1)"
    if [[ -z "${status}" ]]; then
        status="FAILED - exit ${rc}"
    fi

    output_dir="$(printf '%s\n' "${output}" | sed -n 's/^Output dir[[:space:]]*:[[:space:]]*//p' | tail -n 1)"
    if [[ -z "${output_dir}" ]]; then
        output_dir="-"
    fi

    local_output_dir="${output_dir/#\/app\/DXF_3D/${ROOT}}"
    step_src="${local_output_dir}/${part}.step"
    if [[ -f "${step_src}" ]]; then
        cp -f "${step_src}" "${STEP_DIR}/${part}.step"
    fi

    printf '%s\t%s\t%s\t%s\n' "${part}" "${status}" "${elapsed}" "${output_dir}" >> "${TIME_FILE}"
    printf '  %s  %ss  %s\n' "${status}" "${elapsed}" "${output_dir}" >&2

    if (( rc != 0 )); then
        overall_rc=2
    fi
done

printf 'Time log: %s\n' "${TIME_FILE}" >&2
exit "${overall_rc}"