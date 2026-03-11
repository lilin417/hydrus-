#!/usr/bin/env python3
"""Hydrus-2D irrigation schedule optimizer.

This script adjusts `rt` or `ht` columns in ATMOSPH.IN by fixed increments,
runs Hydrus calculations, and evaluates desalination effect using Balance.OUT.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


FLOAT_RE = re.compile(r"[+-]?\d+(?:\.\d*)?(?:[Ee][+-]?\d+)?")


@dataclass
class ScenarioResult:
    mode: str
    delta: float
    applied_value: float
    concvol: float
    watbalt: float


@dataclass
class AtmosData:
    lines: list[str]
    header_idx: int
    data_indices: list[int]
    rt_idx: int
    ht_idx: int


def _is_numeric_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(FLOAT_RE.match(stripped.split()[0]))


def load_atmosph(atmosph_path: Path) -> AtmosData:
    lines = atmosph_path.read_text(encoding="utf-8", errors="ignore").splitlines()

    header_idx = -1
    rt_idx = -1
    ht_idx = -1

    for i, line in enumerate(lines):
        tokens = line.split()
        lowered = [token.lower() for token in tokens]
        if "rt" in lowered and "ht" in lowered:
            header_idx = i
            rt_idx = lowered.index("rt")
            ht_idx = lowered.index("ht")
            break

    if header_idx == -1:
        raise ValueError("未在 ATMOSPH.IN 中找到包含 rt 和 ht 的表头行。")

    data_indices: list[int] = []
    for i in range(header_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            if data_indices:
                break
            continue
        if _is_numeric_line(line):
            data_indices.append(i)
        elif data_indices:
            break

    if not data_indices:
        raise ValueError("未在 ATMOSPH.IN 中识别到大气边界数据行。")

    return AtmosData(lines, header_idx, data_indices, rt_idx, ht_idx)


def write_modified_atmosph(
    atmosph_path: Path,
    atmos: AtmosData,
    mode: str,
    delta: float,
) -> float:
    lines = atmos.lines.copy()

    if mode not in {"rt", "ht"}:
        raise ValueError("mode 必须是 rt 或 ht")

    target_idx = atmos.rt_idx if mode == "rt" else atmos.ht_idx
    other_idx = atmos.ht_idx if mode == "rt" else atmos.rt_idx

    last_applied = 0.0
    for row_idx in atmos.data_indices:
        raw = lines[row_idx]
        cols = raw.split()
        if len(cols) <= max(target_idx, other_idx):
            raise ValueError(f"数据列不足，行 {row_idx + 1}: {raw}")

        target = float(cols[target_idx])
        other = float(cols[other_idx])
        if abs(other) > 1e-12:
            raise ValueError(
                f"检测到 {mode} 方案下另一列非零（行 {row_idx + 1}，值={other}），"
                "请先将 rt/ht 互斥设置为0。"
            )

        updated = max(0.0, target + delta)
        cols[target_idx] = f"{updated:.6f}"
        cols[other_idx] = f"0.000000"
        lines[row_idx] = " ".join(cols)
        last_applied = updated

    atmosph_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return last_applied


def run_hydrus(calc_exe: Path, project_dir: Path, timeout_sec: int) -> None:
    proc = subprocess.run(
        [str(calc_exe)],
        cwd=project_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout_sec,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Hydrus 运行失败，退出码 {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )


def parse_balance(balance_path: Path) -> tuple[float, float]:
    text = balance_path.read_text(encoding="utf-8", errors="ignore")

    concvals = [float(m.group(1)) for m in re.finditer(r"ConcVol\D*([+-]?\d+(?:\.\d*)?(?:[Ee][+-]?\d+)?)", text)]
    watvals = [float(m.group(1)) for m in re.finditer(r"WatBalT\D*([+-]?\d+(?:\.\d*)?(?:[Ee][+-]?\d+)?)", text)]

    if not concvals:
        raise ValueError("Balance.OUT 中未找到 ConcVol 数值。")
    if not watvals:
        raise ValueError("Balance.OUT 中未找到 WatBalT 数值。")

    return concvals[-1], watvals[-1]


def build_deltas(step: float, n_steps: int, include_zero: bool) -> list[float]:
    deltas = []
    for i in range(-n_steps, n_steps + 1):
        if i == 0 and not include_zero:
            continue
        deltas.append(round(i * step, 10))
    return deltas


def evaluate_mode(
    mode: str,
    deltas: Iterable[float],
    atmosph_path: Path,
    calc_exe: Path,
    project_dir: Path,
    timeout_sec: int,
) -> list[ScenarioResult]:
    original = atmosph_path.read_text(encoding="utf-8", errors="ignore")
    atmos = load_atmosph(atmosph_path)
    results: list[ScenarioResult] = []

    seen_values: set[float] = set()
    try:
        for delta in deltas:
            applied_value = write_modified_atmosph(atmosph_path, atmos, mode, delta)
            rounded_value = round(applied_value, 8)
            if rounded_value in seen_values:
                continue
            seen_values.add(rounded_value)

            run_hydrus(calc_exe, project_dir, timeout_sec)
            concvol, watbalt = parse_balance(project_dir / "Balance.OUT")
            results.append(ScenarioResult(mode, delta, applied_value, concvol, watbalt))
    finally:
        atmosph_path.write_text(original, encoding="utf-8")

    return results


def save_results(results: list[ScenarioResult], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["mode", "delta", "applied_value", "ConcVol", "WatBalT"])
        for r in results:
            writer.writerow([r.mode, r.delta, r.applied_value, r.concvol, r.watbalt])


def print_top(results: list[ScenarioResult], top_n: int) -> None:
    ranked = sorted(results, key=lambda r: (r.concvol, -r.watbalt))
    print("\n=== 推荐方案（ConcVol 越低越好，WatBalT 越高越好）===")
    for i, r in enumerate(ranked[:top_n], start=1):
        print(
            f"{i}. mode={r.mode:>2} | delta={r.delta:+.4f} | "
            f"applied={r.applied_value:.4f} | ConcVol={r.concvol:.6g} | WatBalT={r.watbalt:.6g}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="自动搜索 Hydrus 灌溉方案（rt/ht）")
    parser.add_argument("--project-dir", type=Path, default=Path(r"D:\\Hydrus20260303"))
    parser.add_argument("--calc-exe", type=Path, default=Path(r"D:\\Hydrus2D3D\\H2D_Calc.exe"))
    parser.add_argument("--step", type=float, default=0.2, help="每次增减固定量（rt: cm/day; ht: cm）")
    parser.add_argument("--n-steps", type=int, default=5, help="向上/向下搜索步数")
    parser.add_argument("--include-zero", action="store_true", help="是否包含0增量基线")
    parser.add_argument("--timeout", type=int, default=1800, help="单次 Hydrus 计算超时（秒）")
    parser.add_argument("--output-csv", type=Path, default=Path("results/irrigation_scenarios.csv"))
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args()

    project_dir = args.project_dir
    atmosph_path = project_dir / "ATMOSPH.IN"
    balance_path = project_dir / "Balance.OUT"

    if not project_dir.exists():
        raise FileNotFoundError(f"项目目录不存在: {project_dir}")
    if not args.calc_exe.exists():
        raise FileNotFoundError(f"Hydrus 计算程序不存在: {args.calc_exe}")
    if not atmosph_path.exists():
        raise FileNotFoundError(f"缺少输入文件: {atmosph_path}")
    if not balance_path.exists():
        print("提示：Balance.OUT 当前不存在，将在首次计算后生成。")

    deltas = build_deltas(args.step, args.n_steps, args.include_zero)
    all_results: list[ScenarioResult] = []

    print("开始评估 rt 方案...")
    all_results.extend(
        evaluate_mode("rt", deltas, atmosph_path, args.calc_exe, project_dir, args.timeout)
    )

    print("开始评估 ht 方案...")
    all_results.extend(
        evaluate_mode("ht", deltas, atmosph_path, args.calc_exe, project_dir, args.timeout)
    )

    if not all_results:
        raise RuntimeError("未产生任何可用方案。")

    save_results(all_results, args.output_csv)
    print(f"\n已保存全部方案到: {args.output_csv}")
    print_top(all_results, args.top_n)


if __name__ == "__main__":
    main()
