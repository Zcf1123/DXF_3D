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
import ast
import datetime as _dt
import glob
import json
import logging
import os
import re
import runpy
import shutil
import sys
import traceback
import time
from typing import Any, Dict, List, Optional


HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


def _intent_allows_rebuild(model_intent: str) -> bool:
    text = (model_intent or "").lower()
    if not text.strip():
        return False
    tokens = (
        "重建", "主体", "平面连杆", "连杆板", "摇臂", "摆臂", "曲柄连杆",
        "多孔连接板", "长圆孔连杆", "整体外轮廓", "主视图整体",
        "rebuild", "linkage", "rocker", "link plate",
    )
    return any(token in text for token in tokens)


def _projection_validation_is_poor(validation: Dict) -> bool:
    views = validation.get("views") or {}
    if not views:
        return False
    poor_views = 0
    very_poor_views = 0
    for report in views.values():
        input_coverage = float(report.get("input_coverage", 0.0) or 0.0)
        hit_ratio = float(report.get("hit_ratio", 0.0) or 0.0)
        extra = float(report.get("extra", 0.0) or 0.0)
        if input_coverage < 0.35 or hit_ratio < 0.35 or extra > 0.65:
            poor_views += 1
        if input_coverage < 0.15 or hit_ratio < 0.15 or extra > 0.85:
            very_poor_views += 1
    return very_poor_views >= 1 or poor_views >= 2


def _projection_validation_cache_reach(
    validation: Dict,
    input_coverage_threshold: float = 0.95,
    hit_ratio_threshold: float = 0.99,
) -> bool:
    views = validation.get("views") or {}
    required = ("front", "left", "top")
    for view_name in required:
        report = views.get(view_name)
        if not report:
            return False
        input_coverage = float(report.get("input_coverage", 0.0) or 0.0)
        hit_ratio = float(report.get("hit_ratio", 0.0) or 0.0)
        if input_coverage < input_coverage_threshold or hit_ratio < hit_ratio_threshold:
            return False
    return True


def _validation_report_values(report: Dict) -> Dict[str, float]:
    input_coverage = float(report.get("input_coverage", 0.0) or 0.0)
    return {
        "input_coverage": input_coverage * 100.0,
        "missing": float(report.get("missing", 1.0 - input_coverage) or 0.0) * 100.0,
        "hit_ratio": float(report.get("hit_ratio", 0.0) or 0.0) * 100.0,
        "extra": float(report.get("extra", 0.0) or 0.0) * 100.0,
    }


def _draft_has_locked_intent_geometry(features: List[Dict]) -> bool:
    reasons = {
        (feature.get("params") or {}).get("reason")
        for feature in features
    }
    required = {
        "side_connected_main_through_tube",
        "side_connected_round_end",
        "side_connected_slotted_end",
        "side_connected_left_arm",
        "side_connected_right_arm",
    }
    return required.issubset(reasons)


def _intent_understanding_description(model_intent: str) -> str:
    text = (model_intent or "").lower()
    if any(token in text for token in ("贯穿圆筒", "长圆孔端耳", "斜边连杆", "00005340")):
        return (
            "中心为贯穿圆筒主体，左侧为较薄的斜边连接臂接小圆耳，"
            "右侧为较薄的水平连接臂接长圆孔端耳；FRONT 决定外轮廓，TOP 决定各连接件深度。"
        )
    if any(token in text for token in ("平面连杆", "连杆板", "摇臂", "摆臂")):
        return "按等厚连杆/摇臂板理解：FRONT 为板状外轮廓，TOP/LEFT 校验厚度与孔槽贯穿方向。"
    return model_intent.strip()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _make_run_dir(base: str, prefix: str = "") -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = OUTPUTS_DIR
    output_subdir = os.environ.get("DXF_3D_OUTPUT_SUBDIR", "").strip().strip("/")
    if output_subdir:
        output_root = os.path.join(output_root, output_subdir)
    dirname = f"{ts}_{base}"
    if prefix:
        dirname = f"{prefix}_{dirname}"
    out_dir = os.path.join(output_root, dirname)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _auto_output_prefix(llm) -> str:
    label = str(getattr(llm, "model", "") or "auto").strip().lower()
    prefix = re.sub(r"[^0-9a-zA-Z._-]+", "-", label).strip("._-")
    return prefix or "auto"


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


def _close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def _is_llm_unavailable_failure(message: str) -> bool:
    text = str(message or "")
    return "LLM 请求失败" in text or "LLM 已禁用" in text


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def process_dxf(dxf_path: str, llm,
                single_view_extrude_depth: Optional[float] = None,
                model_intent: str = "",
                run_validation: bool = False) -> Dict[str, Any]:
    started_at = time.perf_counter()
    base = os.path.splitext(os.path.basename(dxf_path))[0]
    if not os.path.exists(dxf_path):
        return {
            "input": dxf_path,
            "output_dir": "",
            "llm": llm.model if llm.enabled else f"disabled ({llm.disabled_reason})",
            "status": "FAILED",
            "error": f"FileNotFoundError: {dxf_path}",
            "elapsed_s": round(time.perf_counter() - started_at, 3),
        }
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
        if model_intent:
            log.info("建模意图      : %s", model_intent)
            log.info("LLM 理解      : %s", _intent_understanding_description(model_intent))

        # 1. Parse
        banner("阶段 1 ─ 解析 DXF 实体")
        from ...dxf_loader import load_dxf
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
        from ...view_classifier import classify_views
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
        from ...projection_mapper import map_views_to_3d
        from .feature_inference import infer_features
        projected = map_views_to_3d(bundles)
        for name, pv in projected.items():
            log.info("投影 %-6s -> 平面 %s, 尺寸 %.3f × %.3f, 实体数=%d",
                     name, pv.plane, pv.width, pv.height, len(pv.entities))
        draft = infer_features(projected, bundles, single_view_extrude_depth, model_intent)
        # Log dimension-source breakdown for W/D/H
        from ...geometry_estimator import _dim_measurements_by_axis
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
        allow_rebuild = _intent_allows_rebuild(model_intent)
        if llm.enabled:
            log.info("调用模型      : %s", llm.model)
            if _draft_has_locked_intent_geometry(draft_dicts):
                log.info("intent 草案    : 已由图纸轮廓锁定组合体几何，跳过 LLM 重写")
            elif allow_rebuild:
                log.info("受控重建      : intent 已允许 LLM 重选主体特征")
            if not _draft_has_locked_intent_geometry(draft_dicts):
                refined, msg = llm.refine_features(
                    view_bboxes, draft_dicts, feature_view_summary, model_intent, allow_rebuild
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
                log.info("沿用 intent 草案特征（LLM 仅作为启用路径，不重写锁定几何）")
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
            validate_projection_against_views,
        )
        step_path = os.path.join(run_dir, f"{base}.step")
        obj_path = os.path.join(run_dir, f"{base}.obj")
        png_path = os.path.join(run_dir, f"{base}.png")
        normalized_png = os.path.join(run_dir, f"{base}_views_normalized.png")
        model_views_png = os.path.join(run_dir, f"{base}_model_views.png")
        overview_png = os.path.join(run_dir, f"{base}_overview.png")
        json_path = os.path.join(run_dir, "model.json")
        py_path = os.path.join(run_dir, "generated_model.py")
        validation_path = os.path.join(run_dir, "projection_validation.json")

        if run_validation:
            try:
                validation = validate_projection_against_views(
                    fcstd_path, projected, features, validation_path)
                log.info("投影验证    : %s", validation.get("status"))
                _say(f"Projection   : {validation.get('status')}")
                view_display = {"front": "FRONT", "left": "LEFT", "right": "LEFT", "top": "TOP"}
                for view_name in ("front", "left", "top"):
                    report = validation.get("views", {}).get(view_name)
                    if not report:
                        continue
                    values = _validation_report_values(report)
                    log.info(
                        "  · %s: status=%s input_coverage=%.1f%% missing=%.1f%% hit_ratio=%.1f%% extra=%.1f%% bbox_error=%s",
                        view_display.get(view_name, view_name.upper()),
                        report.get("status"),
                        values["input_coverage"],
                        values["missing"],
                        values["hit_ratio"],
                        values["extra"],
                        report.get("bbox_error"),
                    )
                    _say(
                        "  {name:<5} {status:<4} input_coverage={input_coverage:5.1f}% missing={missing:5.1f}% hit_ratio={hit_ratio:5.1f}% extra={extra:5.1f}%".format(
                            name=view_display.get(view_name, view_name.upper()),
                            status=str(report.get("status", "")),
                            **values,
                        )
                    )
                log.info("已写出        : projection_validation.json")
                if (llm.enabled and not allow_rebuild and
                        _projection_validation_is_poor(validation)):
                    log.info("投影较差    : 自动启用受控重建模式并二次精修")
                    retry_intent = (model_intent + "\n" if model_intent else "") + \
                        "投影验证很差，允许重选主体外轮廓和基础特征；优先保持真实视图尺寸、厚度、孔位。"
                    rebuilt, retry_msg = llm.refine_features(
                        view_bboxes, final_dicts, feature_view_summary,
                        retry_intent, allow_rebuild=True)
                    log.info("二次 LLM 返回  : %s", retry_msg)
                    if rebuilt is not None:
                        final_dicts = rebuilt
                        with open(os.path.join(run_dir, "features.json"), "w",
                                  encoding="utf-8") as f:
                            json.dump(final_dicts, f, indent=2, ensure_ascii=False)
                        features = [Feature(kind=d["kind"], params=d["params"])
                                    for d in final_dicts]
                        retry_artifacts = build_model(features, run_dir, base_name=base,
                                                      projected=projected)
                        if "error" in retry_artifacts:
                            log.warning("二次重建失败: %s", retry_artifacts["error"])
                        else:
                            for warning in retry_artifacts.get("warnings", []):
                                log.warning("二次建模警告: %s", warning)
                            fcstd_path = retry_artifacts["fcstd"]
                            summary["fcstd"] = fcstd_path
                            validation = validate_projection_against_views(
                                fcstd_path, projected, features, validation_path)
                            log.info("二次投影验证: %s", validation.get("status"))
                            _say(f"Projection   : {validation.get('status')} (auto rebuild)")
                            for view_name in ("front", "left", "top"):
                                report = validation.get("views", {}).get(view_name)
                                if not report:
                                    continue
                                values = _validation_report_values(report)
                                log.info(
                                    "  · %s: status=%s input_coverage=%.1f%% missing=%.1f%% hit_ratio=%.1f%% extra=%.1f%% bbox_error=%s",
                                    view_display.get(view_name, view_name.upper()),
                                    report.get("status"),
                                    values["input_coverage"],
                                    values["missing"],
                                    values["hit_ratio"],
                                    values["extra"],
                                    report.get("bbox_error"),
                                )
                                _say(
                                    "  {name:<5} {status:<4} input_coverage={input_coverage:5.1f}% missing={missing:5.1f}% hit_ratio={hit_ratio:5.1f}% extra={extra:5.1f}%".format(
                                        name=view_display.get(view_name, view_name.upper()),
                                        status=str(report.get("status", "")),
                                        **values,
                                    )
                                )
                    else:
                        log.info("二次重建未生效，保留首次建模结果")
            except Exception as exc:
                log.warning("投影验证失败: %s\n%s", exc, traceback.format_exc())
                _say(f"Projection   : WARN — validation failed: {exc}")
        else:
            log.info("投影验证    : 跳过（命令行加 --val 可启用）")

        for name, fn in (
            ("STEP",            lambda: export_step(fcstd_path, step_path)),
            ("OBJ",             lambda: export_obj(fcstd_path, obj_path)),
            ("三视图预览 PNG",  lambda: export_preview_png(
                bundles, png_path)),
            ("归一化三视图 PNG", lambda: export_normalized_views_png(
                projected, normalized_png)),
            ("模型三视图 PNG",  lambda: export_model_views_png(
                fcstd_path, model_views_png, features, projected)),
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

    summary["elapsed_s"] = round(time.perf_counter() - started_at, 3)
    log.info("总耗时        : %.3fs", summary["elapsed_s"])
    return summary


def process_dxf_auto(dxf_path: str, llm, model_intent: str = "",
                     run_validation: bool = False) -> Dict[str, Any]:
    """Direct LLM FreeCAD-script modeling route.

    This keeps parsing, view classification, projection, exports, and
    validation identical to the controlled pipeline, but lets the LLM write
    the FreeCAD modeling script directly instead of emitting Feature objects.
    """
    started_at = time.perf_counter()
    source_base = os.path.splitext(os.path.basename(dxf_path))[0]
    base = source_base
    if not os.path.exists(dxf_path):
        return {
            "input": dxf_path,
            "output_dir": "",
            "llm": llm.model if llm.enabled else f"disabled ({llm.disabled_reason})",
            "status": "FAILED",
            "error": f"FileNotFoundError: {dxf_path}",
            "elapsed_s": round(time.perf_counter() - started_at, 3),
            "mode": "auto",
        }
    run_dir = _make_run_dir(source_base, prefix=_auto_output_prefix(llm))
    log = _make_logger(run_dir)
    summary: Dict[str, Any] = {
        "input": dxf_path,
        "output_dir": run_dir,
        "llm": llm.model if llm.enabled else f"disabled ({llm.disabled_reason})",
        "status": "FAILED",
        "mode": "auto",
    }

    def banner(title: str) -> None:
        log.info("─" * 60)
        log.info(title)
        log.info("─" * 60)

    try:
        banner("阶段 0 ─ Auto 直接建模启动")
        log.info("输入 DXF 文件 : %s", dxf_path)
        log.info("输出目录      : %s", run_dir)
        log.info("输出基名      : %s", base)
        log.info("LLM 状态      : %s", summary["llm"])
        if model_intent:
            log.info("建模意图      : %s", model_intent)
            log.info("LLM 理解      : %s", _intent_understanding_description(model_intent))

        banner("阶段 1 ─ 解析 DXF 实体")
        from ...dxf_loader import load_dxf
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

        banner("阶段 2 ─ 三视图分类")
        from ...view_classifier import classify_views
        bundles = classify_views(entities)
        for b in bundles:
            log.info("视图 %-18s bbox=(%.3f, %.3f) — (%.3f, %.3f)  实体数=%d",
                     b.name, b.bbox[0], b.bbox[1], b.bbox[2], b.bbox[3],
                     len(b.entities))
        with open(os.path.join(run_dir, "views_algorithm.json"), "w",
                  encoding="utf-8") as f:
            json.dump([b.to_dict() for b in bundles],
                      f, indent=2, ensure_ascii=False)

        view_bboxes = {b.name: list(b.bbox) for b in bundles
                       if not b.name.startswith("unknown_")}
        with open(os.path.join(run_dir, "views.json"), "w",
                  encoding="utf-8") as f:
            json.dump([b.to_dict() for b in bundles],
                      f, indent=2, ensure_ascii=False)
        log.info("已写出        : views.json")

        banner("阶段 3 ─ 投影并生成 Auto 上下文")
        from ...projection_mapper import map_views_to_3d
        from ...llm.code.llm_code_planner import (
            build_auto_context, cache_successful_freecad_script,
            generate_freecad_script, repair_freecad_script_after_execution,
            generate_outline_fallback_script, validate_fcstd_arc_edges,
            normalize_fcstd_dimensions, validate_fcstd_dimensions,
        )
        projected = map_views_to_3d(bundles)
        for name, pv in projected.items():
            log.info("投影 %-6s -> 平面 %s, 尺寸 %.3f × %.3f, 实体数=%d",
                     name, pv.plane, pv.width, pv.height, len(pv.entities))
        auto_context = build_auto_context(dxf_path, bundles, projected, model_intent)
        with open(os.path.join(run_dir, "auto_context.json"), "w",
                  encoding="utf-8") as f:
            json.dump(auto_context, f, indent=2, ensure_ascii=False)
        log.info("已写出        : auto_context.json")

        banner("阶段 4 ─ LLM 直接生成 FreeCAD 脚本")
        fcstd_path = os.path.join(run_dir, f"{base}.FCStd")
        py_path = os.path.join(run_dir, "generated_model.py")
        script, script_msg = generate_freecad_script(
            llm, auto_context, base, fcstd_path, run_dir, use_cache=run_validation)
        log.info("LLM 返回      : %s", script_msg)
        if script is None:
            log.warning("LLM 脚本校验  : FAIL — %s", script_msg)
            if _is_llm_unavailable_failure(script_msg):
                raise RuntimeError(script_msg)
            fallback_script = generate_outline_fallback_script(auto_context, base, fcstd_path)
            if fallback_script is None:
                raise RuntimeError(script_msg)
            log.info("确定性兜底    : LLM 脚本未通过安全/结构校验，改用结构化轮廓兜底")
            script = fallback_script
        model_understanding = _extract_model_understanding(script)
        if model_understanding:
            log.info("LLM 模型理解  : %s", model_understanding)
        else:
            log.info("LLM 模型理解  : （生成脚本未提供）")
        dimensions_used = _extract_dimensions_used(script)
        if dimensions_used:
            log.info("LLM 使用尺寸  : %s", json.dumps(dimensions_used, ensure_ascii=False, sort_keys=True))
        else:
            log.info("LLM 使用尺寸  : （生成脚本未提供）")
        with open(py_path, "w", encoding="utf-8") as f:
            f.write(script)
        log.info("已写出        : generated_model.py")

        banner("阶段 5 ─ 执行 LLM 生成脚本")
        try:
            runpy.run_path(py_path, run_name="__main__")
        except Exception as exc:
            exec_reason = f"脚本执行失败：{type(exc).__name__}: {exc}"
            log.warning("LLM 脚本执行  : FAIL — %s", exec_reason)
            repaired_script, repair_msg = repair_freecad_script_after_execution(
                llm, auto_context, base, fcstd_path, script, exec_reason, run_dir)
            log.info("LLM 执行修复  : %s", repair_msg)
            if repaired_script is None:
                fallback_script = generate_outline_fallback_script(auto_context, base, fcstd_path)
                if fallback_script is None:
                    raise RuntimeError(f"LLM 脚本执行失败且自动修复失败：{exec_reason}；{repair_msg}")
                log.info("确定性兜底    : LLM 执行失败，改用完整线弧轮廓拉伸")
                script = fallback_script
                with open(py_path, "w", encoding="utf-8") as f:
                    f.write(script)
                if os.path.exists(fcstd_path):
                    os.remove(fcstd_path)
                runpy.run_path(py_path, run_name="__main__")
            else:
                script = repaired_script
                with open(py_path, "w", encoding="utf-8") as f:
                    f.write(script)
                if os.path.exists(fcstd_path):
                    os.remove(fcstd_path)
                try:
                    runpy.run_path(py_path, run_name="__main__")
                except Exception as repair_exc:
                    repair_exec_reason = f"修复脚本执行失败：{type(repair_exc).__name__}: {repair_exc}"
                    log.warning("LLM 修复执行  : FAIL — %s", repair_exec_reason)
                    fallback_script = generate_outline_fallback_script(auto_context, base, fcstd_path)
                    if fallback_script is None:
                        raise
                    log.info("确定性兜底    : LLM 执行失败，改用完整线弧轮廓拉伸")
                    script = fallback_script
                    with open(py_path, "w", encoding="utf-8") as f:
                        f.write(script)
                    if os.path.exists(fcstd_path):
                        os.remove(fcstd_path)
                    runpy.run_path(py_path, run_name="__main__")
            repaired_understanding = _extract_model_understanding(script)
            if repaired_understanding:
                log.info("LLM 模型理解  : %s", repaired_understanding)
            repaired_dimensions = _extract_dimensions_used(script)
            if repaired_dimensions:
                log.info("LLM 使用尺寸  : %s", json.dumps(repaired_dimensions, ensure_ascii=False, sort_keys=True))
        if not os.path.exists(fcstd_path):
            raise RuntimeError(f"LLM 脚本未生成 FCStd: {fcstd_path}")
        dim_ok, dim_reason, dim_details = validate_fcstd_dimensions(fcstd_path, auto_context)
        with open(os.path.join(run_dir, "dimension_validation.json"), "w", encoding="utf-8") as f:
            json.dump(dim_details, f, indent=2, ensure_ascii=False)
        if not dim_ok:
            log.warning("尺寸契约校验  : FAIL — %s；按要求忽略并继续输出当前结果", dim_reason)
        else:
            log.info("尺寸契约校验  : OK")
        if dim_ok:
            arc_ok, arc_reason, arc_details = validate_fcstd_arc_edges(fcstd_path, auto_context)
            with open(os.path.join(run_dir, "arc_validation.json"), "w", encoding="utf-8") as f:
                json.dump(arc_details, f, indent=2, ensure_ascii=False)
            if arc_reason == "SKIP":
                log.info("圆弧边校验    : 跳过（输入轮廓无 ARC 约束）")
            elif arc_ok:
                log.info("圆弧边校验    : OK")
            else:
                log.warning("圆弧边校验    : FAIL — %s", arc_reason)
                fallback_script = generate_outline_fallback_script(auto_context, base, fcstd_path)
                if fallback_script is not None:
                    log.info("确定性兜底    : 尝试使用 TOP 真实圆弧边轮廓拉伸")
                    script = fallback_script
                    with open(py_path, "w", encoding="utf-8") as f:
                        f.write(script)
                    if os.path.exists(fcstd_path):
                        os.remove(fcstd_path)
                    runpy.run_path(py_path, run_name="__main__")
                    dim_ok, dim_reason, dim_details = validate_fcstd_dimensions(fcstd_path, auto_context)
                    with open(os.path.join(run_dir, "dimension_validation.json"), "w", encoding="utf-8") as f:
                        json.dump(dim_details, f, indent=2, ensure_ascii=False)
                    arc_ok, arc_reason, arc_details = validate_fcstd_arc_edges(fcstd_path, auto_context)
                    with open(os.path.join(run_dir, "arc_validation.json"), "w", encoding="utf-8") as f:
                        json.dump(arc_details, f, indent=2, ensure_ascii=False)
                    if dim_ok and arc_ok:
                        log.info("确定性兜底    : OK（真实圆弧边轮廓通过尺寸与圆弧校验）")
                    elif not dim_ok:
                        log.warning("确定性兜底    : FAIL — %s", dim_reason)
                    else:
                        log.warning("确定性兜底    : FAIL — %s", arc_reason)
        if not run_validation:
            log.info("LLM 脚本缓存  : 跳过（命令行加 --val 才保存）")
        from .freecad_builder import embed_projected_views
        embed_projected_views(fcstd_path, projected)
        summary["fcstd"] = fcstd_path
        log.info("FCStd 文件    : %s", fcstd_path)

        banner("阶段 6 ─ 导出附加产物")
        from .exporters import (
            export_step, export_obj, export_preview_png,
            export_normalized_views_png,
            export_iso_overview_png, export_model_json,
            validate_projection_against_views,
        )
        from ...llm.code.hlr_exporters import export_hlr_model_views_png
        step_path = os.path.join(run_dir, f"{base}.step")
        obj_path = os.path.join(run_dir, f"{base}.obj")
        png_path = os.path.join(run_dir, f"{base}.png")
        normalized_png = os.path.join(run_dir, f"{base}_views_normalized.png")
        model_views_png = os.path.join(run_dir, f"{base}_model_views.png")
        overview_png = os.path.join(run_dir, f"{base}_overview.png")
        json_path = os.path.join(run_dir, "model.json")
        validation_path = os.path.join(run_dir, "projection_validation.json")

        if run_validation:
            try:
                validation = validate_projection_against_views(
                    fcstd_path, projected, None, validation_path)
                log.info("投影验证    : %s", validation.get("status"))
                _say(f"Projection   : {validation.get('status')}")
                view_display = {"front": "FRONT", "left": "LEFT", "right": "LEFT", "top": "TOP"}
                for view_name in ("front", "left", "top"):
                    report = validation.get("views", {}).get(view_name)
                    if not report:
                        continue
                    values = _validation_report_values(report)
                    log.info(
                        "  · %s: status=%s input_coverage=%.1f%% missing=%.1f%% hit_ratio=%.1f%% extra=%.1f%% bbox_error=%s",
                        view_display.get(view_name, view_name.upper()),
                        report.get("status"),
                        values["input_coverage"],
                        values["missing"],
                        values["hit_ratio"],
                        values["extra"],
                        report.get("bbox_error"),
                    )
                    _say(
                        "  {name:<5} {status:<4} input_coverage={input_coverage:5.1f}% missing={missing:5.1f}% hit_ratio={hit_ratio:5.1f}% extra={extra:5.1f}%".format(
                            name=view_display.get(view_name, view_name.upper()),
                            status=str(report.get("status", "")),
                            **values,
                        )
                    )
                log.info("已写出        : projection_validation.json")
                if _projection_validation_cache_reach(validation):
                    cache_successful_freecad_script(llm, auto_context, base, script)
                    log.info("LLM 脚本缓存  : 已记录成功脚本（--val，三个视图 input_coverage 均 ≥95%% 且 hit_ratio 均 ≥99%%）")
                else:
                    log.info("LLM 脚本缓存  : 跳过（三个视图 input_coverage 未全部达到 95%% 或 hit_ratio 未全部达到 99%%）")
            except Exception as exc:
                log.warning("投影验证失败: %s\n%s", exc, traceback.format_exc())
                _say(f"Projection   : WARN — validation failed: {exc}")
                log.info("LLM 脚本缓存  : 跳过（投影验证失败）")
        else:
            log.info("投影验证    : 跳过（命令行加 --val 可启用）")

        for name, fn in (
            ("STEP",            lambda: export_step(fcstd_path, step_path)),
            ("OBJ",             lambda: export_obj(fcstd_path, obj_path)),
            ("三视图预览 PNG",  lambda: export_preview_png(bundles, png_path)),
            ("归一化三视图 PNG", lambda: export_normalized_views_png(projected, normalized_png)),
            ("模型三视图 PNG",  lambda: export_hlr_model_views_png(fcstd_path, model_views_png)),
            ("3D 总览 PNG",      lambda: export_iso_overview_png(fcstd_path, overview_png)),
            ("model.json",      lambda: export_model_json(
                fcstd_path, json_path,
                {"input": dxf_path, "views": view_bboxes,
                 "llm": summary["llm"], "mode": "auto"})),
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
        log.info("  Auto 上下文       : %s", os.path.join(run_dir, "auto_context.json"))
        log.info("  LLM Python 脚本   : %s", py_path)
        log.info("  详细日志          : %s", os.path.join(run_dir, "run.log"))
        summary["status"] = "OK"
    except Exception as exc:
        log.error("Auto 流水线失败: %s\n%s", exc, traceback.format_exc())
        summary["error"] = f"{type(exc).__name__}: {exc}"

    summary["elapsed_s"] = round(time.perf_counter() - started_at, 3)
    log.info("总耗时        : %.3fs", summary["elapsed_s"])
    if summary["status"] != "OK" and _is_llm_unavailable_failure(summary.get("error", "")):
        _close_logger(log)
        try:
            shutil.rmtree(run_dir)
        except FileNotFoundError:
            pass
        summary["output_dir"] = ""
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
    p.add_argument("--no-llm", action="store_true",
                   help="Disable LLM calls where applicable")
    p.add_argument("--intent", dest="model_intent",
                   default=os.environ.get("DXF_3D_MODEL_INTENT", ""),
                   help="Natural-language modeling intent hint")
    p.add_argument("--model-intent", dest="model_intent", help=argparse.SUPPRESS)
    p.add_argument("--val", action="store_true",
                   help="Run projection validation and print Projection details")
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

    from ...llm_client import LLMClient
    llm = LLMClient(config_path=args.config, disabled=args.no_llm)
    llm_label = llm.model if llm.enabled else f"disabled ({llm.disabled_reason})"
    _say(f"LLM         : {llm_label}")

    rc = 0
    for t in targets:
        s = process_dxf_auto(t, llm, model_intent=args.model_intent,
                             run_validation=args.val)
        if s.get("output_dir"):
            _say(f"Output dir  : {s['output_dir']}")
        else:
            _say("Output dir  : 未创建（失败前未产生输出）")
        _say(f"Elapsed     : {float(s.get('elapsed_s', 0.0)):.3f}s")
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


def _extract_model_understanding(script: str) -> str:
    match = re.search(r'(?m)^\s*MODEL_UNDERSTANDING\s*=\s*(["\'])(.*?)\1', script)
    if not match:
        return ""
    return match.group(2).strip()


def _extract_dimensions_used(script: str) -> Dict[str, Any]:
    try:
        tree = ast.parse(script)
    except SyntaxError:
        return {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "DIMENSIONS_USED"
                   for target in node.targets):
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


if __name__ == "__main__":
    sys.exit(main())
