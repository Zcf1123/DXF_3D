"""Top-level orchestrator for the DXF -> 3D pipeline.

Default invocation (intended to run inside the `cad-assistant` Docker
image):

    freecadcmd -c "import sys; sys.path.insert(0,'/app'); \
                   from DXF_3D.run import main; sys.exit(main())"

By default reads every `*.dxf` in `DXF_3D/dxf_files/` and writes a
per-run directory under `DXF_3D/outputs/run_<timestamp>_<base>/`
containing:

    <base>.FCStd            FreeCAD project
    <base>.step             STEP export
    <base>.obj              OBJ mesh
    <base>.png              three-view preview (matplotlib)
    entities.json           parsed DXF entities + metadata
    views.json              view classification result
    features.json           inferred 3D features (post-LLM if enabled)
    features_draft.json     pre-LLM features (when LLM is enabled)
    model.json              FreeCAD object summary
    generated_model.py      standalone reproduction script
    run.log                 detailed log

Stdout is intentionally minimal:
    LLM         : <model name or "disabled">
    Output dir  : <run_dir>
    FCStd       : <path>
    Status      : OK | FAILED
"""
from __future__ import annotations

import argparse
import datetime as _dt
import glob
import json
import logging
import os
import sys
import traceback
from typing import Any, Dict, List, Optional


HERE = os.path.dirname(os.path.abspath(__file__))
DXF_FILES_DIR = os.path.join(HERE, "dxf_files")
OUTPUTS_DIR = os.path.join(HERE, "outputs")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _make_run_dir(base: str) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(OUTPUTS_DIR, f"run_{ts}_{base}")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _make_logger(run_dir: str) -> logging.Logger:
    logger = logging.getLogger(f"dxf3d.{os.path.basename(run_dir)}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fh = logging.FileHandler(os.path.join(run_dir, "run.log"),
                             encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(fh)
    return logger


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def process_dxf(dxf_path: str, llm) -> Dict[str, Any]:
    base = os.path.splitext(os.path.basename(dxf_path))[0]
    run_dir = _make_run_dir(base)
    log = _make_logger(run_dir)
    summary: Dict[str, Any] = {
        "input": dxf_path,
        "output_dir": run_dir,
        "llm": llm.model if llm.enabled else f"disabled ({llm.disabled_reason})",
        "status": "FAILED",
    }

    def banner(title: str) -> None:
        log.info("─" * 60)
        log.info(title)
        log.info("─" * 60)

    try:
        banner("阶段 0 ─ 流水线启动")
        log.info("输入 DXF 文件 : %s", dxf_path)
        log.info("输出目录      : %s", run_dir)
        log.info("LLM 状态      : %s", summary["llm"])

        # 1. Parse
        banner("阶段 1 ─ 解析 DXF 实体")
        from .dxf_loader import load_dxf
        entities, meta = load_dxf(dxf_path)
        log.info("解析后端      : %s", meta.get("backend"))
        log.info("整体 bbox     : %s", meta.get("bbox"))
        log.info("实体总数      : %d", len(entities))
        kind_count: Dict[str, int] = {}
        for e in entities:
            kind_count[e.kind] = kind_count.get(e.kind, 0) + 1
        for k in sorted(kind_count):
            log.info("  %-12s = %d", k, kind_count[k])
        with open(os.path.join(run_dir, "entities.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"meta": meta,
                       "entities": [e.to_dict() for e in entities]},
                      f, indent=2, ensure_ascii=False)
        log.info("已写出        : entities.json")

        # 2. Views
        banner("阶段 2 ─ 三视图分类")
        from .view_classifier import classify_views
        bundles = classify_views(entities)
        for b in bundles:
            log.info("视图 %-18s bbox=(%.3f, %.3f) — (%.3f, %.3f)  实体数=%d",
                     b.name, b.bbox[0], b.bbox[1], b.bbox[2], b.bbox[3],
                     len(b.entities))
        view_bboxes = {b.name: list(b.bbox) for b in bundles
                       if not b.name.startswith("unknown_")}
        with open(os.path.join(run_dir, "views.json"), "w",
                  encoding="utf-8") as f:
            json.dump([b.to_dict() for b in bundles],
                      f, indent=2, ensure_ascii=False)
        log.info("已写出        : views.json")

        # 3. Project + features (draft)
        banner("阶段 3 ─ 投影并推断特征草案")
        from .projection_mapper import map_views_to_3d
        from .feature_inference import infer_features
        projected = map_views_to_3d(bundles)
        for name, pv in projected.items():
            log.info("投影 %-6s -> 平面 %s, 尺寸 %.3f × %.3f, 实体数=%d",
                     name, pv.plane, pv.width, pv.height, len(pv.entities))
        draft = infer_features(projected, bundles)
        # Log dimension-source breakdown for W/D/H
        from .geometry_estimator import _dim_measurements_by_axis
        dim_info = _dim_measurements_by_axis(bundles)
        for axis in ("W", "D", "H"):
            vals = dim_info[axis]
            if vals:
                log.info("尺寸来源      : %s = %.3f mm（来自 %d 条标注，取最大值）",
                         axis, max(vals), len(vals))
            else:
                log.info("尺寸来源      : %s = bbox 均值（无可用标注）", axis)
        # Report hole filtering
        from .feature_inference import _hole_has_hidden_evidence
        all_holes = [f for f in draft if f.kind == "hole"]
        accepted = [h for h in all_holes if _hole_has_hidden_evidence(h, projected)]
        rejected_n = len(all_holes) - len(accepted)
        if rejected_n:
            log.info("孔验证        : 候选 %d 个，通过跨视图虚线验证 %d 个，过滤假孔 %d 个",
                     len(all_holes), len(accepted), rejected_n)
        else:
            log.info("孔验证        : %d 个孔均通过跨视图虚线验证（或无虚线层，默认保留）",
                     len(all_holes))
        draft_dicts = [f.to_dict() for f in draft]
        log.info("草案特征数    : %d", len(draft))
        for f in draft:
            p = f.params
            if f.kind == "extrude_profile":
                log.info("  · 拉伸轮廓: plane=%s, source_view=%s, depth=%.3f, edges=%d",
                         p.get("plane"), p.get("source_view"),
                         p.get("depth", 0), len(p.get("edges", [])))
            elif f.kind == "base_block":
                log.info("  · 底块: W=%.3f D=%.3f H=%.3f origin=%s",
                         p.get("width", 0), p.get("depth", 0),
                         p.get("height", 0), p.get("origin"))
            elif f.kind == "hole":
                log.info("  · 通孔: axis=%s r=%.3f pos=%s 长度=%.3f (来自 %s)",
                         p.get("axis"), p.get("radius", 0), p.get("position"),
                         p.get("through_length", 0), p.get("source_view"))
        with open(os.path.join(run_dir, "features_draft.json"), "w",
                  encoding="utf-8") as f:
            json.dump(draft_dicts, f, indent=2, ensure_ascii=False)
        log.info("已写出        : features_draft.json")

        # 4. Optional LLM refinement
        banner("阶段 4 ─ LLM 复核与精修特征")
        final_dicts = draft_dicts
        if llm.enabled:
            log.info("调用模型      : %s", llm.model)
            refined, msg = llm.refine_features(view_bboxes, draft_dicts)
            log.info("LLM 返回      : %s", msg)
            if refined is not None:
                final_dicts = refined
                log.info("精修特征数    : %d", len(final_dicts))
                for d in final_dicts:
                    log.info("  · %s %s", d.get("kind"),
                             json.dumps(d.get("params", {}),
                                        ensure_ascii=False))
            else:
                log.info("沿用草案特征（LLM 修正未生效）")
        else:
            log.info("LLM 已禁用，直接使用草案特征")
        with open(os.path.join(run_dir, "features.json"), "w",
                  encoding="utf-8") as f:
            json.dump(final_dicts, f, indent=2, ensure_ascii=False)
        log.info("已写出        : features.json")

        # 5. Build FCStd
        banner("阶段 5 ─ 通过 FreeCAD 生成 3D 模型")
        from .feature_inference import Feature
        from .freecad_builder import build_model
        features = [Feature(kind=d["kind"], params=d["params"])
                    for d in final_dicts]
        artifacts = build_model(features, run_dir, base_name=base,
                                projected=projected)
        if "error" in artifacts:
            log.error("建模失败: %s", artifacts["error"])
            raise RuntimeError(artifacts["error"])
        fcstd_path = artifacts["fcstd"]
        summary["fcstd"] = fcstd_path
        log.info("FCStd 文件    : %s", fcstd_path)

        # 6. Other artifacts
        banner("阶段 6 ─ 导出附加产物")
        from .exporters import (
            export_step, export_obj, export_preview_png,
            export_iso_overview_png, export_model_json,
            export_generated_python,
        )
        step_path = os.path.join(run_dir, f"{base}.step")
        obj_path = os.path.join(run_dir, f"{base}.obj")
        png_path = os.path.join(run_dir, f"{base}.png")
        overview_png = os.path.join(run_dir, f"{base}_overview.png")
        json_path = os.path.join(run_dir, "model.json")
        py_path = os.path.join(run_dir, "generated_model.py")

        for name, fn in (
            ("STEP",            lambda: export_step(fcstd_path, step_path)),
            ("OBJ",             lambda: export_obj(fcstd_path, obj_path)),
            ("三视图预览 PNG",  lambda: export_preview_png(
                bundles, png_path)),
            ("3D 总览 PNG",      lambda: export_iso_overview_png(
                fcstd_path, overview_png)),
            ("model.json",      lambda: export_model_json(
                fcstd_path, json_path,
                {"input": dxf_path, "views": view_bboxes,
                 "llm": summary["llm"]})),
            ("可复现脚本 .py",  lambda: export_generated_python(
                features, py_path, base, fcstd_path)),
        ):
            try:
                fn()
                log.info("已导出        : %s", name)
            except Exception as exc:
                log.warning("导出 %s 失败: %s\n%s",
                            name, exc, traceback.format_exc())

        banner("阶段 7 ─ 完成")
        log.info("产物清单：")
        log.info("  FCStd            : %s", fcstd_path)
        log.info("  STEP             : %s", step_path)
        log.info("  OBJ              : %s", obj_path)
        log.info("  三视图预览 PNG    : %s", png_path)
        log.info("  3D 总览 PNG       : %s", overview_png)
        log.info("  特征 JSON         : %s",
                 os.path.join(run_dir, "features.json"))
        log.info("  可复现 Python 脚本: %s", py_path)
        log.info("  详细日志          : %s",
                 os.path.join(run_dir, "run.log"))
        summary["status"] = "OK"
    except Exception as exc:
        log.error("流水线失败: %s\n%s", exc, traceback.format_exc())
        summary["error"] = f"{type(exc).__name__}: {exc}"

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="DXF three-view to 3D pipeline (Docker default)")
    p.add_argument("dxf", nargs="*",
                   help="DXF file(s). Defaults to all *.dxf in DXF_3D/dxf_files/")
    p.add_argument("--config", default="config.json",
                   help="Path to config.json (default: ./config.json)")
    args = p.parse_args(argv)

    if args.dxf:
        targets = [os.path.abspath(t) for t in args.dxf]
    else:
        os.makedirs(DXF_FILES_DIR, exist_ok=True)
        targets = sorted(glob.glob(os.path.join(DXF_FILES_DIR, "*.dxf")) +
                         glob.glob(os.path.join(DXF_FILES_DIR, "*.DXF")))

    if not targets:
        print(f"No DXF files found in {DXF_FILES_DIR}/")
        return 1

    from .llm_planner import LLMPlanner
    llm = LLMPlanner(config_path=args.config)
    llm_label = llm.model if llm.enabled else f"disabled ({llm.disabled_reason})"
    _say(f"LLM         : {llm_label}")

    rc = 0
    for t in targets:
        s = process_dxf(t, llm)
        _say(f"Output dir  : {s['output_dir']}")
        _say(f"Status      : {s['status']}"
              + (f" — {s.get('error')}" if s["status"] != "OK" else ""))
        _say("")
        if s["status"] != "OK":
            rc = 2
    return rc


def _say(msg: str) -> None:
    """Write minimal pipeline output to stderr so FreeCAD's `\\r`-based
    progress chatter on stdout cannot clobber it."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


if __name__ == "__main__":
    sys.exit(main())
