#!/usr/bin/env python3
"""
通过批量修改 Hydrus-2D 项目 ATMOSPH.IN 中首个 rt 或 ht 值，调用 H2D_Calc.exe 计算，
并根据 Balance.OUT 中的 ConcVol 与 WatBalT 选择较优洗盐方案。

核心约束：
- 每个方案只能修改 rt 或 ht 之一（另一列保持不变）。
- 采用“固定步长增减”的方式生成候选方案。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?")


@dataclass
class ScenarioResult:
    mode: str  # "rt" or "ht"
    value: float
    scenario_dir: str
    success: bool
    concvol: Optional[float] = None
    watbalt: Optional[float] = None
    message: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="优化 Hydrus-2D 灌溉(rt/ht)以提升洗盐效果(最小化 ConcVol, 同时关注 WatBalT)。"
    )
    parser.add_argument("--project-dir", required=True, help="Hydrus 项目目录，例如 D:/Hydrus20260303")
    parser.add_argument(
        "--calc-exe",
        required=True,
        help="Hydrus 计算程序路径，例如 D:/Hydrus2D3D/H2D_Calc.exe",
    )
    parser.add_argument("--work-dir", default="hydrus_opt_runs", help="批量计算工作目录")

    parser.add_argument("--rt-min", type=float, default=-1.0, help="rt 搜索下限 (cm/day)")
    parser.add_argument("--rt-max", type=float, default=5.0, help="rt 搜索上限 (cm/day)")
    parser.add_argument("--rt-step", type=float, default=0.5, help="rt 固定步长")

    parser.add_argument("--ht-min", type=float, default=0.0, help="ht 搜索下限 (cm)")
    parser.add_argument("--ht-max", type=float, default=20.0, help="ht 搜索上限 (cm)")
    parser.add_argument("--ht-step", type=float, default=2.0, help="ht 固定步长")

    parser.add_argument(
        "--top-k", type=int, default=5, help="输出前 K 个较优方案（按 ConcVol 升序、WatBalT 降序）"
    )
    parser.add_argument("--timeout", type=int, default=1800, help="单方案计算超时时间（秒）")
    parser.add_argument(
        "--include-baseline",
        action="store_true",
        help="包含基准方案（rt=0, ht=0，不修改）。",
    )
    parser.add_argument(
        "--skip-failed",
        action="store_true",
        help="汇总时忽略失败方案（默认会保留并记录失败原因）。",
    )
    return parser.parse_args()


def frange(start: float, end: float, step: float) -> List[float]:
    if step <= 0:
        raise ValueError("步长必须大于 0")
    values: List[float] = []
    n = 0
    # 加一个小容差避免浮点误差导致遗漏终点
    while True:
        v = start + n * step
        if v > end + 1e-12:
            break
        values.append(round(v, 10))
        n += 1
    return values


def locate_column_indices(lines: Sequence[str]) -> Tuple[int, int]:
    """在 ATMOSPH.IN 中找到包含 rt 和 ht 的表头行，并返回列索引。"""
    for line in lines:
        if not line.strip():
            continue
        tokens = line.split()
        lower = [t.lower() for t in tokens]
        if "rt" in lower and "ht" in lower:
            return lower.index("rt"), lower.index("ht")
    raise ValueError("未在 ATMOSPH.IN 中找到同时包含 rt 和 ht 的表头行")


def find_first_numeric_data_line(lines: Sequence[str], start_idx: int) -> int:
    """从 start_idx 往后找到第一行“数值数据行”。"""
    for i in range(start_idx, len(lines)):
        s = lines[i].strip()
        if not s:
            continue
        tokens = s.split()
        if all(FLOAT_RE.fullmatch(t) for t in tokens):
            return i
    raise ValueError("未找到可修改的首行数值数据")


def modify_first_rt_ht(atmosph_path: Path, mode: str, value: float) -> None:
    lines = atmosph_path.read_text(encoding="utf-8", errors="ignore").splitlines()

    rt_idx, ht_idx = locate_column_indices(lines)

    header_line_index = -1
    for i, line in enumerate(lines):
        tokens = [t.lower() for t in line.split()]
        if "rt" in tokens and "ht" in tokens:
            header_line_index = i
            break
    if header_line_index < 0:
        raise ValueError("无法定位 rt/ht 表头行")

    data_index = find_first_numeric_data_line(lines, header_line_index + 1)
    row = lines[data_index].split()

    if max(rt_idx, ht_idx) >= len(row):
        raise ValueError(f"首个数据行列数不足，无法访问 rt/ht 列: {lines[data_index]}")

    if mode == "rt":
        row[rt_idx] = f"{value:.6g}"
    elif mode == "ht":
        row[ht_idx] = f"{value:.6g}"
    else:
        raise ValueError("mode 只能是 'rt' 或 'ht'")

    lines[data_index] = " ".join(row)
    atmosph_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_hydrus(calc_exe: Path, scenario_dir: Path, timeout: int) -> Tuple[bool, str]:
    try:
        p = subprocess.run(
            [str(calc_exe)],
            cwd=str(scenario_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        if p.returncode != 0:
            return False, f"H2D_Calc.exe 返回码 {p.returncode}: {p.stderr.strip() or p.stdout.strip()}"
        return True, p.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, f"计算超时（>{timeout}s）"
    except Exception as exc:  # noqa: BLE001
        return False, f"调用计算程序异常: {exc}"


def _extract_last_float_from_line(line: str) -> Optional[float]:
    found = FLOAT_RE.findall(line)
    if not found:
        return None
    return float(found[-1])


def parse_balance_metrics(balance_path: Path) -> Tuple[Optional[float], Optional[float]]:
    """
    解析 Balance.OUT，尽量提取 ConcVol 和 WatBalT。
    优先：识别表头并按列取最后一行。
    回退：查找含关键词的行并取该行最后一个数字。
    """
    lines = balance_path.read_text(encoding="utf-8", errors="ignore").splitlines()

    concvol = None
    watbalt = None

    headers: List[str] = []
    conc_idx = -1
    wat_idx = -1

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue

        tokens = stripped.split()
        lower = [t.lower() for t in tokens]

        # 表头检测
        if "concvol" in lower or "watbalt" in lower:
            headers = tokens
            lower_h = [h.lower() for h in headers]
            conc_idx = lower_h.index("concvol") if "concvol" in lower_h else -1
            wat_idx = lower_h.index("watbalt") if "watbalt" in lower_h else -1
            continue

        # 若有表头，则尝试读取后续数据行
        if headers and all(FLOAT_RE.fullmatch(t) for t in tokens):
            if conc_idx >= 0 and conc_idx < len(tokens):
                concvol = float(tokens[conc_idx])
            if wat_idx >= 0 and wat_idx < len(tokens):
                watbalt = float(tokens[wat_idx])
            continue

        # 回退：关键字行
        low_line = stripped.lower()
        if "concvol" in low_line and concvol is None:
            maybe = _extract_last_float_from_line(stripped)
            if maybe is not None:
                concvol = maybe
        if "watbalt" in low_line and watbalt is None:
            maybe = _extract_last_float_from_line(stripped)
            if maybe is not None:
                watbalt = maybe

    return concvol, watbalt


def build_scenarios(args: argparse.Namespace) -> List[Tuple[str, float]]:
    scenarios: List[Tuple[str, float]] = []

    if args.include_baseline:
        scenarios.append(("baseline", 0.0))

    for v in frange(args.rt_min, args.rt_max, args.rt_step):
        scenarios.append(("rt", v))
    for v in frange(args.ht_min, args.ht_max, args.ht_step):
        scenarios.append(("ht", v))

    return scenarios


def score_key(r: ScenarioResult) -> Tuple[float, float]:
    c = r.concvol if r.concvol is not None else math.inf
    # WatBalT 越大越好，所以排序时取负
    w = -(r.watbalt if r.watbalt is not None else -math.inf)
    return c, w


def main() -> int:
    args = parse_args()

    project_dir = Path(args.project_dir).resolve()
    calc_exe = Path(args.calc_exe).resolve()
    work_dir = Path(args.work_dir).resolve()

    atmosph_name = "ATMOSPH.IN"
    balance_name = "Balance.OUT"

    if not project_dir.exists():
        raise FileNotFoundError(f"项目目录不存在: {project_dir}")
    if not calc_exe.exists():
        raise FileNotFoundError(f"计算程序不存在: {calc_exe}")
    if not (project_dir / atmosph_name).exists():
        raise FileNotFoundError(f"项目目录缺少 {atmosph_name}: {project_dir / atmosph_name}")

    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    scenarios = build_scenarios(args)
    results: List[ScenarioResult] = []

    for idx, (mode, value) in enumerate(scenarios, start=1):
        name = f"{idx:03d}_{mode}_{value:+g}"
        scenario_dir = work_dir / name
        shutil.copytree(project_dir, scenario_dir)

        try:
            if mode in {"rt", "ht"}:
                modify_first_rt_ht(scenario_dir / atmosph_name, mode, value)

            ok, msg = run_hydrus(calc_exe, scenario_dir, args.timeout)
            if not ok:
                results.append(
                    ScenarioResult(mode=mode, value=value, scenario_dir=str(scenario_dir), success=False, message=msg)
                )
                continue

            balance_path = scenario_dir / balance_name
            if not balance_path.exists():
                results.append(
                    ScenarioResult(
                        mode=mode,
                        value=value,
                        scenario_dir=str(scenario_dir),
                        success=False,
                        message=f"计算完成但未找到 {balance_name}",
                    )
                )
                continue

            concvol, watbalt = parse_balance_metrics(balance_path)
            results.append(
                ScenarioResult(
                    mode=mode,
                    value=value,
                    scenario_dir=str(scenario_dir),
                    success=True,
                    concvol=concvol,
                    watbalt=watbalt,
                    message=msg,
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                ScenarioResult(
                    mode=mode,
                    value=value,
                    scenario_dir=str(scenario_dir),
                    success=False,
                    message=str(exc),
                )
            )

    filtered = [r for r in results if r.success and (r.concvol is not None)]
    if not args.skip_failed:
        pass

    ranked = sorted(filtered, key=score_key)
    top = ranked[: max(args.top_k, 0)]

    summary = {
        "project_dir": str(project_dir),
        "calc_exe": str(calc_exe),
        "work_dir": str(work_dir),
        "total_scenarios": len(results),
        "successful_with_metrics": len(filtered),
        "top_k": [asdict(x) for x in top],
        "all_results": [asdict(x) for x in results],
    }

    out_json = work_dir / "optimization_summary.json"
    out_csv = work_dir / "optimization_summary.csv"
    out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    with out_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["mode", "value", "success", "concvol", "watbalt", "scenario_dir", "message"],
        )
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))

    print(f"候选方案总数: {len(results)}")
    print(f"成功且提取到 ConcVol 的方案数: {len(filtered)}")
    print(f"汇总 JSON: {out_json}")
    print(f"汇总 CSV : {out_csv}")
    print("\nTop 方案：")
    for i, r in enumerate(top, start=1):
        print(
            f"{i}. mode={r.mode:>8s}, value={r.value:+g}, "
            f"ConcVol={r.concvol}, WatBalT={r.watbalt}, dir={r.scenario_dir}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
