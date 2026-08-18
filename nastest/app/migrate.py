"""一次性数据迁移:旧版 S3 编号 → 新版子组编号(启动时自动执行,幂等)。

旧版:S3-01..10 单一编号,多 RAID 级别靠 metrics 里 raid_<级别>_ 前缀累计;
新版:S3-BASE-01(basic 组)+ S3-<RAID级别>-01..10(五个 RAID 子组),
     用例编号本身携带 RAID 级别,各组相互独立。
     (09/10 单流顺序读写为新增用例,旧版无对应数据,不参与迁移。)

迁移规则:
  S3-01..08 → 按该用例已测级别(raid_levels_tested/raid_level_tested/配置 raid_level)
              逐级别展开为 S3-<级别>-01..08;raid_<级别>_ 前缀剥离后作为该组指标;
              扁平键(最后一次运行结果)只归入最后测试的那个级别;
              证据目录随最后级别迁移。
  S3-09 → S3-BASE-01(剥离 raid_* 键);旧 S3-10(重建监控)已删除,不再迁移。
"""
import shutil

from . import config, db

_OLD = [f"S3-{i:02d}" for i in range(1, 11)]


def _levels(metrics: dict, cfg: dict) -> list[str]:
    lvls = list(metrics.get("raid_levels_tested") or [])
    if not lvls:
        one = metrics.get("raid_level_tested")
        if one:
            lvls = [one]
    if not lvls:
        want = str(cfg.get("raid_level") or "").upper()
        if want.startswith("RAID"):
            lvls = [want]
    return [l for l in lvls if isinstance(l, str) and l.startswith("RAID")]


def _flat(metrics: dict) -> dict:
    """去掉 raid_* 命名空间键,保留扁平键(最后一次运行的结果)。"""
    return {k: v for k, v in metrics.items() if not k.startswith("raid_")}


def _move_ev(base, old_cid: str, new_cid: str) -> None:
    src, dst = base / old_cid, base / new_cid
    if src.exists() and not dst.exists():
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            pass


def s3_groups() -> None:
    from .registry import ALL_IDS
    for run in db.list_runs():
        rid = run["id"]
        cfg = run["config"]
        results = {r["case_id"]: r for r in db.list_results(rid)}
        olds = [cid for cid in _OLD if cid in results]
        if not olds:
            continue  # 已是新编号(或无 S3 数据),跳过
        ev_base = config.EVIDENCE_DIR / f"run_{rid}" / "cases"
        for cid in olds:
            row = results[cid]
            m = row.get("metrics") or {}
            common = dict(
                status=row["status"], actual=row.get("actual") or "",
                started_at=row.get("started_at"), completed_at=row.get("completed_at"),
                duration_ms=row.get("duration_ms"),
                verdict_by=row.get("verdict_by") or "", error=row.get("error") or "")
            n = cid[-2:]
            if n == "09":  # → basic 组(与 RAID 级别无关)
                db.ensure_results(rid, ["S3-BASE-01"])
                db.set_result(rid, "S3-BASE-01", metrics=_flat(m), **common)
                _move_ev(ev_base, cid, "S3-BASE-01")
                continue
            if n == "10":  # 旧重建监控用例,已删除;数据不迁移,仅清理
                continue
            # S3-01..08 → 按已测 RAID 级别逐组展开
            levels = _levels(m, cfg)
            if not levels:
                continue
            last, flat = levels[-1], _flat(m)
            for lv in levels:
                new_id = f"S3-{lv}-{n}"
                nm = {}
                if lv == last:
                    nm.update(flat)  # 扁平键 = 最后一次运行 = 最后级别的数据
                prefix = f"raid_{lv}_"
                nm.update({k[len(prefix):]: v for k, v in m.items()
                           if k.startswith(prefix)})
                db.ensure_results(rid, [new_id])
                db.set_result(rid, new_id, metrics=nm, **common)
                if lv == last:
                    _move_ev(ev_base, cid, new_id)
        for cid in olds:
            db.delete_result(rid, cid)
        # 补齐新注册表全部用例行(新增的 RAID 子组显示为未测)
        db.ensure_results(rid, ALL_IDS)
