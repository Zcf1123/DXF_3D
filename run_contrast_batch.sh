#!/usr/bin/env bash
set -u -o pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
INPUT_DIR="${DXF_3D_BATCH_INPUT_DIR:-${ROOT}/dxf_files/test}"
OUTPUT_BASE_SUBDIR="${DXF_3D_BATCH_OUTPUT_SUBDIR:-test}"
OUTPUT_SUBDIR="${OUTPUT_BASE_SUBDIR}"
OUTPUT_DIR="${ROOT}/outputs/${OUTPUT_SUBDIR}"
SUMMARY_FILE="${OUTPUT_DIR}/summary.txt"

suffix=0
while [[ -e "${OUTPUT_DIR}" ]]; do
    suffix=$((suffix + 1))
    OUTPUT_SUBDIR="${OUTPUT_BASE_SUBDIR}${suffix}"
    OUTPUT_DIR="${ROOT}/outputs/${OUTPUT_SUBDIR}"
    SUMMARY_FILE="${OUTPUT_DIR}/summary.txt"
done

shopt -s nullglob
dxf_files=("${INPUT_DIR}"/*.dxf "${INPUT_DIR}"/*.DXF)
shopt -u nullglob

if (( ${#dxf_files[@]} == 0 )); then
    printf 'No DXF files found in %s\n' "${INPUT_DIR}" >&2
    exit 1
fi

args=()
model_profile=""
config_path="${ROOT}/config.json"
while (( $# > 0 )); do
    case "$1" in
        --gpt|--qwen|--openai)
            model_profile="${1#--}"
            args+=("$1")
            ;;
        --val|--no-llm)
            args+=("$1")
            ;;
        --intent|--model-intent|--config)
            if (( $# < 2 )); then
                printf 'Missing value for %s\n' "$1" >&2
                exit 1
            fi
            if [[ "$1" == "--config" ]]; then
                if [[ "$2" = /* ]]; then
                    config_path="$2"
                else
                    config_path="${ROOT}/$2"
                fi
            fi
            args+=("$1" "$2")
            shift
            ;;
        -*)
            printf 'Unknown option: %s\n' "$1" >&2
            exit 1
            ;;
        *)
            args+=("$1")
            ;;
    esac
    shift
done

if [[ ! -x "${ROOT}/run.sh" ]]; then
    printf 'run.sh not found or not executable: %s\n' "${ROOT}/run.sh" >&2
    exit 1
fi

if [[ ! -f "${config_path}" ]]; then
    printf 'Config file not found: %s\n' "${config_path}" >&2
    exit 1
fi

if [[ -d "${ROOT}/outputs" && ! -w "${ROOT}/outputs" ]]; then
    printf 'Output root is not writable: %s\n' "${ROOT}/outputs" >&2
    exit 1
fi
if [[ ! -d "${ROOT}/outputs" && ! -w "${ROOT}" ]]; then
    printf 'Workspace root is not writable, cannot create outputs: %s\n' "${ROOT}" >&2
    exit 1
fi

CONTAINER_CLI="${DXF_3D_CONTAINER_CLI:-docker}"
IMAGE="${DXF_3D_IMAGE:-dxf-3d}"
if ! command -v "${CONTAINER_CLI}" >/dev/null 2>&1; then
    printf 'Container CLI not found: %s\n' "${CONTAINER_CLI}" >&2
    exit 1
fi
if ! "${CONTAINER_CLI}" info >/dev/null 2>&1; then
    printf 'Container engine is not available: %s\n' "${CONTAINER_CLI}" >&2
    exit 1
fi
if ! "${CONTAINER_CLI}" image inspect "${IMAGE}" >/dev/null 2>&1; then
    printf 'Docker image not found: %s\n' "${IMAGE}" >&2
    exit 1
fi

mkdir -p "${OUTPUT_DIR}"
if [[ ! -s "${SUMMARY_FILE}" ]]; then
    printf 'part\tstatus\tllm_model\tmodel_accuracy\telapsed_s\tllm_understanding\tfcstd\toutput_dir\n' >> "${SUMMARY_FILE}"
fi

overall_rc=0
total=${#dxf_files[@]}
index=0
has_val=0
for arg in "${args[@]}"; do
    if [[ "${arg}" == "--val" ]]; then
        has_val=1
        break
    fi
done
if (( has_val == 0 )); then
    args+=("--val")
fi

for dxf in "${dxf_files[@]}"; do
    index=$((index + 1))
    part="$(basename "${dxf}" .dxf)"
    part="$(basename "${part}" .DXF)"

    printf '[%d/%d] %s\n' "${index}" "${total}" "${dxf}" >&2

    started_ns="$(date +%s%N)"
    output="$(DXF_3D_OUTPUT_SUBDIR="${OUTPUT_SUBDIR}" "${ROOT}/run.sh" -d "${args[@]}" "${dxf}" 2>&1)"
    rc=$?
    ended_ns="$(date +%s%N)"

    elapsed="$(printf '%s\n' "${output}" | awk -F: '/^Elapsed[[:space:]]*:/ {gsub(/^[ \t]+|s[ \t]*$/, "", $2); print $2; exit}')"
    if [[ -z "${elapsed}" ]]; then
        elapsed="$(awk -v s="${started_ns}" -v e="${ended_ns}" 'BEGIN { printf "%.3f", (e - s) / 1000000000 }')"
    fi

    status="$(printf '%s\n' "${output}" | sed -n 's/^Status[[:space:]]*:[[:space:]]*//p' | tail -n 1)"
    if [[ -z "${status}" ]]; then
        status="FAILED - exit ${rc}"
    fi

    llm_model="$(printf '%s\n' "${output}" | sed -n 's/^LLM[[:space:]]*:[[:space:]]*//p' | tail -n 1)"
    if [[ -z "${llm_model}" ]]; then
        llm_model="${model_profile:-default}"
    fi

    output_dir="$(printf '%s\n' "${output}" | sed -n 's/^Output dir[[:space:]]*:[[:space:]]*//p' | tail -n 1)"
    if [[ -z "${output_dir}" ]]; then
        output_dir="-"
    fi

    local_output_dir="${output_dir/#\/app\/DXF_3D/${ROOT}}"
    fcstd_src="${local_output_dir}/${part}.FCStd"
    fcstd_dst="-"
    if [[ -f "${fcstd_src}" ]]; then
        fcstd_dst="${OUTPUT_DIR}/${part}.FCStd"
        cp -f "${fcstd_src}" "${fcstd_dst}"
    fi

    model_accuracy="$(printf '%s\n' "${output}" | awk '
        /^[[:space:]]*(FRONT|LEFT|TOP|RIGHT)[[:space:]]/ {
            name=$1
            if (match($0, /model=[[:space:]]*[0-9.]+%/)) {
                value=substr($0, RSTART, RLENGTH)
                sub(/^model=[[:space:]]*/, "", value)
                items[++n]=name "=" value
            }
        }
        END {
            for (i=1; i<=n; i++) {
                printf "%s%s", (i == 1 ? "" : ", "), items[i]
            }
        }')"
    if [[ -z "${model_accuracy}" ]]; then
        model_accuracy="-"
    fi

    llm_understanding="-"
    run_log="${local_output_dir}/run.log"
    if [[ -f "${run_log}" ]]; then
        llm_understanding="$(sed -n 's/^.*LLM 模型理解[[:space:]]*:[[:space:]]*//p; s/^.*LLM 理解[[:space:]]*:[[:space:]]*//p' "${run_log}" | tail -n 1)"
        if [[ -z "${llm_understanding}" ]]; then
            llm_understanding="-"
        fi
    fi

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "${part}" "${status}" "${llm_model}" "${model_accuracy}" "${elapsed}" "${llm_understanding}" "${fcstd_dst}" "${output_dir}" >> "${SUMMARY_FILE}"
    printf '  %s  llm:%s  model:%s  %ss  %s\n' "${status}" "${llm_model}" "${model_accuracy}" "${elapsed}" "${fcstd_dst}" >&2

    if (( rc != 0 )); then
        overall_rc=2
    fi
done

printf 'Summary: %s\n' "${SUMMARY_FILE}" >&2
exit "${overall_rc}"