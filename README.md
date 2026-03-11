# Hydrus-2D 灌溉参数自动搜索脚本

该仓库提供 `optimize_irrigation.py`，用于在 Hydrus-2D 项目中自动修改 `ATMOSPH.IN` 的 `rt` 或 `ht` 列，并调用 `H2D_Calc.exe` 计算，再用 `Balance.OUT` 中指标筛选洗盐效果更优的方案。

## 功能

- 只在单个方案中修改 `rt` 或 `ht`（互斥）。
- 基于固定步长做增减扫描（例如 `-5*step ... +5*step`）。
- 每次运行后读取：
  - `ConcVol`：子区域溶质总量（越小越好）
  - `WatBalT`：该时间步水量平衡，用于评估入渗效果（通常越大越好）
- 输出全部方案到 CSV，并打印推荐 Top N 方案。

## 使用方法

```bash
python optimize_irrigation.py \
  --project-dir "D:\Hydrus20260303" \
  --calc-exe "D:\Hydrus2D3D\H2D_Calc.exe" \
  --step 0.2 \
  --n-steps 5 \
  --include-zero \
  --output-csv results/irrigation_scenarios.csv \
  --top-n 5
```

## 参数说明

- `--step`：固定增减量（rt单位 cm/day；ht单位 cm）
- `--n-steps`：向上和向下扫描的步数
- `--include-zero`：是否包含 0 变化（原始基线）
- `--timeout`：单次 Hydrus 计算超时秒数

## 结果解读

脚本按以下规则排序推荐：

1. `ConcVol` 更低优先
2. 若 `ConcVol` 相同，`WatBalT` 更高优先

> 注意：脚本每轮修改后会恢复原始 `ATMOSPH.IN`，避免永久污染输入文件。
