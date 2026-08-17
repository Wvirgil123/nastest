#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图表生成 CLI —— 调用框架 nastest/app/charts.py 为指定任务生成报告图表。

用法:
  python gen_charts.py           # 默认:最新一次任务
  python gen_charts.py 8         # 指定 run_id
  python gen_charts.py 8 --check # 只检查 matplotlib/数据是否满足,不写文件

输出:data/evidence/run_<id>/charts/*.png(与 report.md/HTML 报告引用同源)。
图表样式与数据抽取逻辑统一维护在 nastest/app/charts.py,本脚本仅为入口。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "nastest"))


def main() -> int:
    from app import charts, config, db  # noqa: E402  需在 path 注入后导入
    check_only = "--check" in sys.argv
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    db.init_db()
    runs = db.list_runs()
    if not runs:
        print("没有任务,请先通过 Web 后台创建并运行测试。")
        return 1
    run_id = int(args[0]) if args else runs[-1]["id"]
    run = db.get_run(run_id)
    if run is None:
        print(f"任务 {run_id} 不存在。")
        return 1

    if not charts.available():
        print("缺少 matplotlib,图表跳过(报告仍可生成)。安装: pip install matplotlib")
        return 2 if check_only else 0

    results = {r["case_id"]: r for r in db.list_results(run_id)}
    if check_only:
        rs = charts._raid_series(results)
        layers, _ = charts._bottleneck_layers(results, run["config"])
        wpts, rpts, _, _ = charts._scale_curves(results)
        print(f"run_{run_id}: RAID 级别有数据 {len(rs)} 个,瓶颈层 {len(layers)} 层,"
              f"扩展曲线 写{len(wpts)}点/读{len(rpts)}点")
        return 0

    out = charts.generate_charts(run_id, results, run["config"], force=True)
    base = config.EVIDENCE_DIR / f"run_{run_id}" / "charts"
    print(f"run_{run_id}: 生成 {len(out)}/{len(charts.CHART_FILES)} 张图表 → {base}/")
    for key, fname in out.items():
        print(f"  {key:12s} {fname}")
    skipped = set(charts.CHART_FILES) - set(out)
    if skipped:
        print(f"跳过(数据不足): {', '.join(sorted(skipped))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
