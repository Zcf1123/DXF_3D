#!/usr/bin/env bash
set -u -o pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
INPUT_DIR="${DXF_3D_BATCH_INPUT_DIR:-${ROOT}/dxf_files/test}"
OUTPUT_BASE_SUBDIR="${DXF_3D_BATCH_OUTPUT_SUBDIR:-test}"
OUTPUT_SUBDIR="${OUTPUT_BASE_SUBDIR}"
OUTPUT_DIR="${ROOT}/outputs/${OUTPUT_SUBDIR}"
SUMMARY_JSON="${OUTPUT_DIR}/summary.json"
SUMMARY_CSV="${OUTPUT_DIR}/summary.csv"
SUMMARY_TMP="${OUTPUT_DIR}/.summary.results.jsonl"

suffix=0
while [[ -e "${OUTPUT_DIR}" ]]; do
    suffix=$((suffix + 1))
    OUTPUT_SUBDIR="${OUTPUT_BASE_SUBDIR}${suffix}"
    OUTPUT_DIR="${ROOT}/outputs/${OUTPUT_SUBDIR}"
    SUMMARY_JSON="${OUTPUT_DIR}/summary.json"
    SUMMARY_CSV="${OUTPUT_DIR}/summary.csv"
    SUMMARY_TMP="${OUTPUT_DIR}/.summary.results.jsonl"
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
if ! command -v python3 >/dev/null 2>&1; then
    printf 'python3 not found\n' >&2
    exit 1
fi

mkdir -p "${OUTPUT_DIR}"
: > "${SUMMARY_TMP}"

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

    llm_understanding="-"
    input_coverage="-"
    missing="-"
    hit_ratio="-"
    extra="-"
    run_log="${local_output_dir}/run.log"
    if [[ -f "${run_log}" ]]; then
        llm_understanding="$(sed -n 's/^.*LLM 模型理解[[:space:]]*:[[:space:]]*//p; s/^.*LLM 理解[[:space:]]*:[[:space:]]*//p' "${run_log}" | tail -n 1)"
        if [[ -z "${llm_understanding}" ]]; then
            llm_understanding="-"
        fi

        input_coverage="$(awk '
            function field_value(key, line, re, value) {
                re = key "=[[:space:]]*[0-9.]+%"
                if (match(line, re)) {
                    value = substr(line, RSTART, RLENGTH)
                    sub("^" key "=[[:space:]]*", "", value)
                    return value
                }
                return ""
            }
            /·[[:space:]]*(FRONT|LEFT|TOP|RIGHT):/ {
                view = ""
                if (match($0, /(FRONT|LEFT|TOP|RIGHT):/)) {
                    view = substr($0, RSTART, RLENGTH - 1)
                }
                value = field_value("input_coverage", $0)
                if (view != "" && value != "") {
                    items[++n] = view "=" value
                }
            }
            END {
                for (i = 1; i <= n; i++) {
                    printf "%s%s", (i == 1 ? "" : ", "), items[i]
                }
            }' "${run_log}")"
        missing="$(awk '
            function field_value(key, line, re, value) {
                re = key "=[[:space:]]*[0-9.]+%"
                if (match(line, re)) {
                    value = substr(line, RSTART, RLENGTH)
                    sub("^" key "=[[:space:]]*", "", value)
                    return value
                }
                return ""
            }
            /·[[:space:]]*(FRONT|LEFT|TOP|RIGHT):/ {
                view = ""
                if (match($0, /(FRONT|LEFT|TOP|RIGHT):/)) {
                    view = substr($0, RSTART, RLENGTH - 1)
                }
                value = field_value("missing", $0)
                if (view != "" && value != "") {
                    items[++n] = view "=" value
                }
            }
            END {
                for (i = 1; i <= n; i++) {
                    printf "%s%s", (i == 1 ? "" : ", "), items[i]
                }
            }' "${run_log}")"
        hit_ratio="$(awk '
            function field_value(key, line, re, value) {
                re = key "=[[:space:]]*[0-9.]+%"
                if (match(line, re)) {
                    value = substr(line, RSTART, RLENGTH)
                    sub("^" key "=[[:space:]]*", "", value)
                    return value
                }
                return ""
            }
            /·[[:space:]]*(FRONT|LEFT|TOP|RIGHT):/ {
                view = ""
                if (match($0, /(FRONT|LEFT|TOP|RIGHT):/)) {
                    view = substr($0, RSTART, RLENGTH - 1)
                }
                value = field_value("hit_ratio", $0)
                if (view != "" && value != "") {
                    items[++n] = view "=" value
                }
            }
            END {
                for (i = 1; i <= n; i++) {
                    printf "%s%s", (i == 1 ? "" : ", "), items[i]
                }
            }' "${run_log}")"
        extra="$(awk '
            function field_value(key, line, re, value) {
                re = key "=[[:space:]]*[0-9.]+%"
                if (match(line, re)) {
                    value = substr(line, RSTART, RLENGTH)
                    sub("^" key "=[[:space:]]*", "", value)
                    return value
                }
                return ""
            }
            /·[[:space:]]*(FRONT|LEFT|TOP|RIGHT):/ {
                view = ""
                if (match($0, /(FRONT|LEFT|TOP|RIGHT):/)) {
                    view = substr($0, RSTART, RLENGTH - 1)
                }
                value = field_value("extra", $0)
                if (view != "" && value != "") {
                    items[++n] = view "=" value
                }
            }
            END {
                for (i = 1; i <= n; i++) {
                    printf "%s%s", (i == 1 ? "" : ", "), items[i]
                }
            }' "${run_log}")"
        if [[ -z "${input_coverage}" ]]; then input_coverage="-"; fi
        if [[ -z "${missing}" ]]; then missing="-"; fi
        if [[ -z "${hit_ratio}" ]]; then hit_ratio="-"; fi
        if [[ -z "${extra}" ]]; then extra="-"; fi
    fi

    SUMMARY_TMP="${SUMMARY_TMP}" \
    PART="${part}" \
    STATUS="${status}" \
    LLM_MODEL="${llm_model}" \
    ELAPSED="${elapsed}" \
    INPUT_COVERAGE="${input_coverage}" \
    HIT_RATIO="${hit_ratio}" \
    MISSING="${missing}" \
    EXTRA="${extra}" \
    LLM_UNDERSTANDING="${llm_understanding}" \
    FCSTD="${fcstd_dst}" \
    RUN_DIR="${output_dir}" \
    python3 - <<'PY'
import json
import os


def parse_view_values(text):
    result = {"front": None, "left": None, "top": None}
    text = (text or "").strip()
    if not text or text == "-":
        return result
    for item in text.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        key = key.strip().lower()
        if key == "right":
            key = "left"
        if key in result:
            result[key] = value.strip()
    return result


def optional(value):
    value = (value or "").strip()
    return None if value in {"", "-"} else value


entry = {
    "name": os.environ["PART"],
    "status": os.environ["STATUS"],
    "llm_model": os.environ["LLM_MODEL"],
    "elapsed_s": float(os.environ["ELAPSED"]),
    "input_coverage": parse_view_values(os.environ["INPUT_COVERAGE"]),
    "hit_ratio": parse_view_values(os.environ["HIT_RATIO"]),
    "missing": parse_view_values(os.environ["MISSING"]),
    "extra": parse_view_values(os.environ["EXTRA"]),
    "llm": {
        "understanding": optional(os.environ["LLM_UNDERSTANDING"]),
    },
    "fcstd": optional(os.environ["FCSTD"]),
    "run_dir": optional(os.environ["RUN_DIR"]),
}

with open(os.environ["SUMMARY_TMP"], "a", encoding="utf-8") as f:
    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
PY

    if (( rc != 0 )); then
        overall_rc=2
    fi
done

SUMMARY_TMP="${SUMMARY_TMP}" \
SUMMARY_JSON="${SUMMARY_JSON}" \
SUMMARY_CSV="${SUMMARY_CSV}" \
OUTPUT_DIR="${OUTPUT_DIR}" \
python3 - <<'PY'
import csv
import json
import os

tmp_path = os.environ["SUMMARY_TMP"]
json_path = os.environ["SUMMARY_JSON"]
csv_path = os.environ["SUMMARY_CSV"]
output_dir = os.environ["OUTPUT_DIR"]

results = []
with open(tmp_path, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if line:
            results.append(json.loads(line))

with open(json_path, "w", encoding="utf-8") as f:
    json.dump({
        "output_dir": output_dir,
        "count": len(results),
        "results": results,
    }, f, ensure_ascii=False, indent=4)
    f.write("\n")

def compact_views(values):
    if not isinstance(values, dict):
        return ""
    items = []
    for view in ("front", "left", "top"):
        value = values.get(view)
        if value is not None:
            items.append(f"{view}={value}")
    return ", ".join(items)

with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=["name", "llm_model", "elapsed_s", "input_coverage", "hit_ratio"],
    )
    writer.writeheader()
    for item in results:
        writer.writerow({
            "name": item.get("name", ""),
            "llm_model": item.get("llm_model", ""),
            "elapsed_s": item.get("elapsed_s", ""),
            "input_coverage": compact_views(item.get("input_coverage")),
            "hit_ratio": compact_views(item.get("hit_ratio")),
        })

os.remove(tmp_path)
PY

exit "${overall_rc}"