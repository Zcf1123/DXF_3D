#!/usr/bin/env python3
"""Compute class-level DXF coverage against the first DXF in each class folder.

Input layout:
    root/
        1/
            a.dxf   # first sorted DXF is the base
            b.dxf
        2/
            x.dxf
            y.dxf

Output CSV columns:
    class,base,target,coverage,front,top,left
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from dxf_loader import load_dxf
from view_classifier import classify_views
from coverage_core import align_segments_to_origin, compare_segment_sets, segments_from_entities

VIEW_NAMES = ("front", "top", "left")

ViewData = Dict[str, Tuple[list, float]]


def dxf_files_in(folder: Path) -> List[Path]:
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() == ".dxf"
    )


def numeric_class_folders(root: Path) -> List[Path]:
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and p.name.isdigit()
    )


def load_dxf_view_data(path: Path) -> ViewData:
    entities, _meta = load_dxf(str(path))
    bundles = classify_views(entities)
    data: ViewData = {}
    for bundle in bundles:
        if bundle.name not in VIEW_NAMES:
            continue
        segments = segments_from_entities(bundle.entities)
        segments = align_segments_to_origin(segments)
        scale = max(float(bundle.width), float(bundle.height), 1e-6)
        data[bundle.name] = (segments, scale)
    return data


def compare_view(base_data: ViewData, target_data: ViewData, view_name: str) -> float:
    base_view = base_data.get(view_name)
    target_view = target_data.get(view_name)
    if base_view is None or target_view is None:
        return 0.0
    base_segments, scale = base_view
    target_segments, _target_scale = target_view
    report = compare_segment_sets(base_segments, target_segments, scale)
    return float(report.get("coverage", 0.0) or 0.0)


def compare_to_base(base_data: ViewData, target_path: Path) -> Dict[str, float]:
    target_data = load_dxf_view_data(target_path)
    view_scores = {
        view_name: compare_view(base_data, target_data, view_name)
        for view_name in VIEW_NAMES
    }
    view_scores["coverage"] = sum(view_scores[v] for v in VIEW_NAMES) / len(VIEW_NAMES)
    return view_scores


def iter_rows(input_root: Path) -> Iterable[Dict[str, str]]:
    for class_dir in numeric_class_folders(input_root):
        files = dxf_files_in(class_dir)
        if not files:
            continue

        base_path = files[0]
        try:
            base_data = load_dxf_view_data(base_path)
        except Exception as exc:
            print(f"[WARN] skip class {class_dir.name}: failed to load base {base_path}: {exc}", file=sys.stderr)
            continue

        for target_path in files:
            if target_path == base_path:
                scores = {"coverage": 1.0, "front": 1.0, "top": 1.0, "left": 1.0}
            else:
                try:
                    scores = compare_to_base(base_data, target_path)
                except Exception as exc:
                    print(f"[WARN] failed {class_dir.name}/{target_path.name}: {exc}", file=sys.stderr)
                    scores = {"coverage": 0.0, "front": 0.0, "top": 0.0, "left": 0.0}

            yield {
                "class": class_dir.name,
                "base": base_path.name,
                "target": target_path.name,
                "coverage": f"{scores['coverage']:.4f}",
                "front": f"{scores['front']:.4f}",
                "top": f"{scores['top']:.4f}",
                "left": f"{scores['left']:.4f}",
            }


def write_csv(input_root: Path, output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["class", "base", "target", "coverage", "front", "top", "left"]
    with output_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in iter_rows(input_root):
            writer.writerow(row)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare each DXF in every numeric class folder against that folder's first DXF."
    )
    parser.add_argument(
        "input_root",
        type=Path,
        help="Root directory containing numeric class folders, e.g. /path/to/classes",
    )
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("class_coverage.csv"),
        help="Output CSV path. Default: class_coverage.csv",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    write_csv(args.input_root, args.output)
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
