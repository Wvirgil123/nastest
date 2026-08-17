"""S3 RAID 子组公共工具:组识别、用例编号规则、评级用级别挑选。

S3 存储子系统按阵列形态拆分为子组(界面各带独立「顺序运行」按钮):
  basic        — 与 RAID 级别无关:S3-BASE-01 裸盘读基线 / S3-BASE-02 重建监控
  RAID0/1/5/6/10 — S3-<级别>-01..10(fio 矩阵 9 项 + 元数据)。
    01..07 为多流(numjobs=4)矩阵;09/10 为单流(numjobs=1)顺序读写,
    避免多 job 在机械盘上磁头交插,测 RAID 真实顺序带宽。
人工切换阵列后运行对应组;执行前会校验 /proc/mdstat 实际级别,防止数据串组。
"""
import re

RAID_SUBS = ["RAID0", "RAID1", "RAID5", "RAID6", "RAID10"]
S3_SUBS = ["basic"] + RAID_SUBS


def raid_group_of(case_id: str) -> str | None:
    """用例编号 → 所属 RAID 组(S3-RAID5-03 → RAID5);basic/其他域返回 None。"""
    m = re.match(r"^S3-(RAID\d+)-", case_id or "")
    return m.group(1) if m else None


def level_case(level: str, n: str) -> str:
    """RAID 组 + 序号 → 用例编号,如 ('RAID5', '02') → S3-RAID5-02。"""
    return f"S3-{level}-{n}"


def results_by_id(run_id: int) -> dict:
    from . import db
    return {r["case_id"]: r for r in db.list_results(run_id)}


def pick_level(results: dict, cfg: dict) -> str | None:
    """挑选 §8 评级 / 基线对比所用的 RAID 级别:

    优先运行配置 raid_level(若该组有已完成数据),否则取完成用例最多的组;
    全部无数据返回 None。
    """
    def passed(lv: str) -> int:
        return sum(1 for cid, r in results.items()
                   if raid_group_of(cid) == lv and r.get("status") == "pass")

    want = str(cfg.get("raid_level") or "").upper()
    if want in RAID_SUBS and passed(want):
        return want
    best = max(RAID_SUBS, key=passed)
    return best if passed(best) else None
