"""Top-level orchestrator for the DXF -> 3D pipeline.

Default invocation (intended to run inside the `cad-assistant` Docker
image):

    freecadcmd -c "import sys; sys.path.insert(0,'/app'); \
                   from DXF_3D.run import main; sys.exit(main())"

By default reads every `*.dxf` in `DXF_3D/dxf_files/` and writes a
per-run directory under `DXF_3D/outputs/<YYYYMMDD>_<HHMMSS>_<base>/`
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


def _round_num(value: float) -> float:
    return round(float(value), 6)


def _entity_semantic_summary(entity, idx: int) -> Dict[str, Any]:
    b = entity.bbox()
    item: Dict[str, Any] = {
        "id": idx,
        "kind": entity.kind,
        "layer": entity.layer,
        "linetype": entity.linetype,
        "linetype_desc": entity.extra.get("linetype_desc"),
        "bbox": [_round_num(v) for v in b] if b else None,
    }
    if entity.kind == "LINE" and len(entity.points) >= 2:
        item["points"] = [[_round_num(p[0]), _round_num(p[1])]
                          for p in entity.points[:2]]
    elif entity.kind == "CIRCLE" and entity.center is not None:
        item["center"] = [_round_num(entity.center[0]), _round_num(entity.center[1])]
        item["radius"] = _round_num(entity.radius or 0.0)
    elif entity.kind == "ARC" and entity.center is not None:
        item["center"] = [_round_num(entity.center[0]), _round_num(entity.center[1])]
        item["radius"] = _round_num(entity.radius or 0.0)
        item["start_angle"] = _round_num(entity.start_angle or 0.0)
        item["end_angle"] = _round_num(entity.end_angle or 0.0)
    return item


def _view_semantic_summary(bundles) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for bundle in bundles:
        out.append({
            "input_name": bundle.name,
            "bbox": [_round_num(v) for v in bundle.bbox],
            "entity_count": len(bundle.entities),
            "entities": [
                _entity_semantic_summary(entity, idx)
                for idx, entity in enumerate(bundle.entities)
            ],
            "annotations": [
                {
                    "kind": ann.kind,
                    "bbox": [_round_num(v) for v in ann.bbox()] if ann.bbox() else None,
                    "dim_text": ann.dim_text,
                    "dim_measurement": ann.dim_measurement,
                }
                for ann in bundle.annotations
            ],
        })
    return out


def _apply_view_review(bundles, review: Dict[str, Any]) -> Dict[str, Any]:
    by_name = {bundle.name: bundle for bundle in bundles}
    applied: Dict[str, Any] = {"views": []}
    for item in review.get("views", []):
        input_name = item["input_name"]
        bundle = by_name.get(input_name)
        if bundle is None:
            continue
        before_count = len(bundle.entities)
        keep_ids = item.get("keep_entity_ids")
        remove_ids = set(item.get("remove_entity_ids", []))
        if keep_ids is not None:
            keep_set = set(keep_ids)
        else:
            keep_set = set(range(before_count)) - remove_ids
        bundle.entities = [
            entity for idx, entity in enumerate(bundle.entities)
            if idx in keep_set and idx not in remove_ids
        ]
        if bundle.entities:
            xs0: List[float] = []
            ys0: List[float] = []
            xs1: List[float] = []
            ys1: List[float] = []
            for entity in bundle.entities:
                b = entity.bbox()
                if b is None:
                    continue
                xs0.append(b[0]); ys0.append(b[1]); xs1.append(b[2]); ys1.append(b[3])
            if xs0:
                bundle.bbox = (min(xs0), min(ys0), max(xs1), max(ys1))
        old_name = bundle.name
        bundle.name = item["canonical_name"]
        removed_ids = (set(range(before_count)) - keep_set) | remove_ids
        applied["views"].append({
            "input_name": input_name,
            "old_name": old_name,
            "canonical_name": bundle.name,
            "before_count": before_count,
            "after_count": len(bundle.entities),
            "removed_ids": sorted(removed_ids),
            "reason": item.get("reason", ""),
        })
    return applied


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _make_run_dir(base: str) -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = OUTPUTS_DIR
    output_subdir = os.environ.get("DXF_3D_OUTPUT_SUBDIR", "").strip().strip("/")
    if output_subdir:
        output_root = os.path.join(output_root, output_subdir)
    out_dir = os.path.join(output_root, f"{ts}_{base}")
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

def process_dxf(dxf_path: str, llm,
                single_view_extrude_depth: Optional[float] = None) -> Dict[str, Any]:
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
        single_view_mode = single_view_extrude_depth is not None and len(bundles) == 1
        if single_view_mode:
            bundles[0].name = "top"
            log.info("单视图拉伸    : 检测到单一视图，按 TOP/XY 轮廓拉伸 %.3f",
                     single_view_extrude_depth)
        for b in bundles:
            log.info("视图 %-18s bbox=(%.3f, %.3f) — (%.3f, %.3f)  实体数=%d",
                     b.name, b.bbox[0], b.bbox[1], b.bbox[2], b.bbox[3],
                     len(b.entities))
        with open(os.path.join(run_dir, "views_algorithm.json"), "w",
                  encoding="utf-8") as f:
            json.dump([b.to_dict() for b in bundles],
                      f, indent=2, ensure_ascii=False)

        banner("阶段 2.5 ─ 工程图语义复核（LLM，可选）")
        view_review_summary = _view_semantic_summary(bundles)
        with open(os.path.join(run_dir, "views_semantic_input.json"), "w",
                  encoding="utf-8") as f:
            json.dump(view_review_summary, f, indent=2, ensure_ascii=False)
        if single_view_mode:
            review = None
            log.info("视图语义复核  : 单视图拉伸模式，跳过 LLM 视图重命名")
        else:
            review, review_msg = llm.review_views(view_review_summary)
            log.info("视图语义复核  : %s", review_msg)
        if review is not None:
            applied_review = _apply_view_review(bundles, review)
            with open(os.path.join(run_dir, "views_semantic.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"raw": review, "applied": applied_review},
                          f, indent=2, ensure_ascii=False)
            for item in applied_review["views"]:
                log.info("  · %-8s -> %-8s 实体 %d -> %d，删除 %d 条：%s",
                         item["old_name"], item["canonical_name"],
                         item["before_count"], item["after_count"],
                         len(item["removed_ids"]), item.get("reason", ""))
        else:
            log.info("视图语义复核  : 沿用算法分类结果")

        feature_view_summary = _view_semantic_summary(bundles)

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
        draft = infer_features(projected, bundles, single_view_extrude_depth)
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
            elif f.kind == "sphere":
                log.info("  · 球体: r=%.3f center=%s (来自 %s)",
                         p.get("radius", 0), p.get("center"),
                         ",".join(p.get("source_views", [])))
            elif f.kind == "cylinder_stack":
                log.info("  · 同轴阶梯圆柱: center=%s segments=%d (来自 %s)",
                         p.get("center"), len(p.get("segments", [])),
                         ",".join(p.get("source_views", [])))
            elif f.kind == "hole":
                hole_label = "盲孔" if p.get("blind") else "通孔"
                log.info("  · %s: axis=%s r=%.3f pos=%s 长度=%.3f (来自 %s)",
                         hole_label, p.get("axis"), p.get("radius", 0),
                         p.get("position"), p.get("through_length", 0),
                         p.get("source_view"))
            elif f.kind == "profile_cut":
                log.info("  · 异形贯穿孔: plane=%s depth=%.3f edges=%d (来自 %s)",
                         p.get("plane"), p.get("depth", 0),
                         len(p.get("edges", [])), p.get("source_view"))
            elif f.kind == "edge_chamfer":
                log.info("  · 边倒角/圆弧过渡: profile=%s distance=%.3f scope=%s top_radius=%s",
                         p.get("profile", "line"), p.get("distance", 0),
                         p.get("scope"), p.get("top_radius"))
        with open(os.path.join(run_dir, "features_draft.json"), "w",
                  encoding="utf-8") as f:
            json.dump(draft_dicts, f, indent=2, ensure_ascii=False)
        log.info("已写出        : features_draft.json")

        # 4. Optional LLM refinement
        banner("阶段 4 ─ LLM 复核与精修特征")
        final_dicts = draft_dicts
        if llm.enabled:
            log.info("调用模型      : %s", llm.model)
            refined, msg = llm.refine_features(
                view_bboxes, draft_dicts, feature_view_summary
            )
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
        for warning in artifacts.get("warnings", []):
            log.warning("建模警告: %s", warning)
        fcstd_path = artifacts["fcstd"]
        summary["fcstd"] = fcstd_path
        log.info("FCStd 文件    : %s", fcstd_path)

        # 6. Other artifacts
        banner("阶段 6 ─ 导出附加产物")
        from .exporters import (
            export_step, export_obj, export_preview_png,
            export_normalized_views_png,
            export_model_views_png,
            export_iso_overview_png, export_model_json,
            export_generated_python,
        )
        step_path = os.path.join(run_dir, f"{base}.step")
        obj_path = os.path.join(run_dir, f"{base}.obj")
        png_path = os.path.join(run_dir, f"{base}.png")
        normalized_png = os.path.join(run_dir, f"{base}_views_normalized.png")
        model_views_png = os.path.join(run_dir, f"{base}_model_views.png")
        overview_png = os.path.join(run_dir, f"{base}_overview.png")
        json_path = os.path.join(run_dir, "model.json")
        py_path = os.path.join(run_dir, "generated_model.py")

        for name, fn in (
            ("STEP",            lambda: export_step(fcstd_path, step_path)),
            ("OBJ",             lambda: export_obj(fcstd_path, obj_path)),
            ("三视图预览 PNG",  lambda: export_preview_png(
                bundles, png_path)),
            ("归一化三视图 PNG", lambda: export_normalized_views_png(
                projected, normalized_png)),
            ("模型三视图 PNG",  lambda: export_model_views_png(
                fcstd_path, model_views_png, features)),
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
        log.info("  归一化三视图 PNG  : %s", normalized_png)
        log.info("  模型三视图 PNG    : %s", model_views_png)
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
    p.add_argument("--extrude-depth", type=float, default=None,
                   help="Depth for single-view TOP/XY extrusion mode")
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
        s = process_dxf(t, llm, single_view_extrude_depth=args.extrude_depth)
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
