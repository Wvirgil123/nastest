"""评级器 —— 方案 §8 参考基线。

不做加权评分与一票否决;对每个可量化维度给出 优/良/及格/差 评级,
回答方案 §1 的四个核心问题,并自动检测 ARM 红旗信号。指标缺失 → "—"。
"""
from . import db, raid
from .registry import CASES


def _m(results: dict, case_id: str, key: str):
    r = results.get(case_id) or {}
    if r.get("status") not in ("pass",):
        return None
    v = (r.get("metrics") or {}).get(key)
    return v if isinstance(v, (int, float)) else None


def _s3(results: dict, lv: str | None, n: str, key: str):
    """S3 RAID 组指标查找;无已测级别时返回 None。"""
    if not lv:
        return None
    return _m(results, raid.level_case(lv, n), key)


def _grade(v, t_ex, t_good, t_pass, lower_better=False):
    """按 §8 阈值带评级。v=None → None。"""
    if v is None:
        return None
    if lower_better:
        if v <= t_ex:
            return "优"
        if v <= t_good:
            return "良"
        if v <= t_pass:
            return "及格"
        return "差"
    if v >= t_ex:
        return "优"
    if v >= t_good:
        return "良"
    if v >= t_pass:
        return "及格"
    return "差"


def _data_disks(cfg: dict, lv: str | None) -> int:
    n = int(cfg.get("disk_count", 4) or 4)
    lvl = (lv or str(cfg.get("raid_level", ""))).upper()
    if "RAID6" in lvl:
        return max(n - 2, 1)
    if "RAID5" in lvl:
        return max(n - 1, 1)
    if "RAID1" in lvl:
        return 1
    if "RAID10" in lvl:
        return max(n // 2, 1)
    return n


def score_run(run_id: int) -> dict:
    run = db.get_run(run_id)
    cfg = run["config"]
    results = {r["case_id"]: r for r in db.list_results(run_id)}
    lv = raid.pick_level(results, cfg)   # §8 评级采用的 RAID 组(有数据的组)
    link_mb_s = int(cfg.get("nic_link_mbps", 1000)) / 8

    grades: list[dict] = []

    def add(key, name, display, grade, basis):
        grades.append({"key": key, "name": name, "value": display,
                       "grade": grade or "—", "basis": basis})

    # 1) 网络利用率(协议吞吐 ÷ 线速)
    smb_w = _m(results, "S5-01", "write_mbps")
    smb_r = _m(results, "S5-01", "read_mbps")
    best = max([v for v in (smb_w, smb_r) if v is not None], default=None)
    util = round(best / link_mb_s * 100, 1) if best else None
    add("net_util", "网络利用率(SMB 峰值÷线速)",
        f"{util}%" if util is not None else "—",
        _grade(util, 95, 85, 70),
        f"SMB 峰值 {best if best is not None else '—'}MB/s,"
        f"线速≈{link_mb_s:.0f}MB/s(§8:≥95% 优)")

    # 2) SMB 加密损失
    lw = _m(results, "S5-02", "encrypt_loss_write_pct")
    lr = _m(results, "S5-02", "encrypt_loss_read_pct")
    loss = round((lw + lr) / 2, 1) if lw is not None and lr is not None else (lw or lr)
    add("encrypt_loss", "SMB 加密性能损失",
        f"{loss}%" if loss is not None else "—",
        _grade(loss, 20, 40, 60, lower_better=True),
        "写/读损失均值(§8:<20% 优;腰斩以上=AES 硬件指令未启用)")

    # 3) RAID 顺序写效率(÷数据盘数,以裸盘读为近似参照)
    seq_w = _s3(results, lv, "02", "seq_write_w_mbps")
    raw = _m(results, "S3-BASE-01", "raw_read_mb_s")
    dd = _data_disks(cfg, lv)
    raid_eff = round(seq_w / (raw * dd) * 100, 1) if seq_w and raw else None
    add("raid_eff", f"RAID 顺序写效率{f'({lv})' if lv else ''}",
        f"{raid_eff}%" if raid_eff is not None else "—",
        _grade(raid_eff, 90, 75, 60),
        f"顺序写 {seq_w or '—'}MB/s ÷ (裸盘 {raw or '—'}MB/s × {dd} 数据盘;"
        f"{lv or cfg.get('raid_level')}),写参照读为近似(§8:≥90% 优)")

    # 4) FS 层损耗(vs 裸盘聚合读)
    seq_r = _s3(results, lv, "01", "seq_read_r_mbps")
    fs_loss = round((1 - seq_r / (raw * dd)) * 100, 1) if seq_r and raw else None
    add("fs_loss", "文件系统层损耗(vs 裸盘)",
        f"{fs_loss}%" if fs_loss is not None else "—",
        _grade(fs_loss, 15, 30, 50, lower_better=True),
        f"顺序读 {seq_r or '—'}MB/s vs 裸盘聚合 "
        f"{round(raw * dd) if raw else '—'}MB/s(§8:<15% 优)")

    # 5) 浸泡吞吐波动
    var = _m(results, "S8-02", "soak_variance_pct")
    add("soak_var", "浸泡吞吐波动(首末段)",
        f"{var}%" if var is not None else "—",
        _grade(var, 10, 20, 30, lower_better=True),
        "§5.8 通过标准 <10%;波动大→热降频/GC/资源爬升")

    # 6) 热降频
    throttled = (results.get("S8-02") or {}).get("metrics", {}).get("throttled")
    if throttled is None:
        tgrade, tval = None, "—"
    elif throttled:
        tgrade, tval = "差", "检测到持续降频伴随性能下降"
    else:
        tgrade, tval = "优", "未检测到热降频"
    add("throttle", "热降频", tval, tgrade, "温度-频率-吞吐相关性分析(§8:无降频 优)")

    # 7) 并发拐点
    infl_w = _m(results, "S6-01", "write_inflection_n")
    infl_r = _m(results, "S6-02", "read_inflection_n")
    infl = max([v for v in (infl_w, infl_r) if v is not None], default=None)
    add("inflection", "并发拐点",
        f"N={int(infl)}" if infl is not None else "—",
        _grade(infl, 8, 4, 2),
        f"写拐点 N={int(infl_w) if infl_w else '—'},"
        f"读拐点 N={int(infl_r) if infl_r else '—'}(§8:≥8 路优)")

    # 8) 满容量性能衰减
    fc = _m(results, "S8-03", "seq_write_degrade_pct")
    add("fullcap_loss", "满容量性能衰减",
        f"{fc}%" if fc is not None else "—",
        _grade(fc, 10, 25, 40, lower_better=True),
        "≥90% 容量下顺序写对比空卷(§8:<10% 优)")

    # ---- 四问(§1) ----
    four = []
    # Q1 瓶颈定位
    parts = []
    iperf_tx = _m(results, "S4-01", "tcp_tx_mb_s")
    if iperf_tx:
        parts.append(f"裸 TCP {iperf_tx}MB/s(线速 {link_mb_s:.0f}MB/s,"
                     f"{round(iperf_tx / link_mb_s * 100)}%)")
    if smb_w:
        parts.append(f"SMB 写 {smb_w}MB/s(协议效率 "
                     f"{round(smb_w / (iperf_tx or link_mb_s) * 100) if iperf_tx else '—'}%)")
    if seq_w:
        parts.append(f"本地顺序写 {seq_w}MB/s")
    dev_cpu = _s3(results, lv, "02", "seq_write_dev_cpu_pct")
    if dev_cpu is None:  # 旧数据回退 fio 自报值
        dev_cpu = _s3(results, lv, "02", "seq_write_fio_cpu_pct")
    if dev_cpu is not None:
        parts.append(f"写入时设备 CPU {dev_cpu}%")
    four.append({"q": "瓶颈在哪一层?",
                 "a": ";".join(parts) if parts else "数据不足(需完成 S3/S4/S5)",
                 "detail": "对比链:本地卷(fio)→ 协议(SMB/NFS)→ 裸 TCP(iperf3)→ 线速;"
                           "哪一层显著掉速即瓶颈层;CPU 打满则算力瓶颈"})
    # Q2 长跑衰减
    a2 = []
    if var is not None:
        a2.append(f"浸泡吞吐波动 {var}%")
    if throttled is not None:
        a2.append("检测到热降频" if throttled else "无热降频")
    four.append({"q": "长时间满载是否衰减/降频?",
                 "a": ";".join(a2) if a2 else "数据不足(需完成 S8-01/S8-02)",
                 "detail": "§5.8:波动<10% 且无持续降频为达标"})
    # Q3 拐点
    a3 = []
    if infl_w is not None:
        a3.append(f"写 N={int(infl_w)}")
    if infl_r is not None:
        a3.append(f"读 N={int(infl_r)}")
    four.append({"q": "并发拐点在哪?",
                 "a": ",".join(a3) if a3 else "数据不足(需完成 S6-01/S6-02)",
                 "detail": "拐点=吞吐不再随客户端数增长的转折点,拐点时资源占用即瓶颈"})
    # Q4 加密开销
    a4 = []
    if loss is not None:
        a4.append(f"SMB seal 平均损失 {loss}%")
    aes128 = _m(results, "S1-02", "openssl_aes-128-gcm_mb_s")
    if aes128:
        a4.append(f"AES-128-GCM {aes128}MB/s")
    four.append({"q": "加密/压缩开销多大?",
                 "a": ";".join(a4) if a4 else "数据不足(需完成 S1-02/S5-02)",
                 "detail": "加密损失决定 SMB 加密/VPN 可用性;ARM 依赖 aes-ce 硬件指令"})

    # ---- 红旗(§8) ----
    flags = []

    def flag(desc, triggered, detail):
        flags.append({"desc": desc, "triggered": triggered, "detail": detail})

    flag("SMB 加密后吞吐腰斩以上(AES 硬件指令未启用?)",
         (loss > 50) if loss is not None else None,
         f"实测损失 {loss if loss is not None else '—'}%;对照 S0-01 的 aes-ce 驱动与 /proc/crypto")
    multi = _m(results, "S4-03", "tcp_multi_mbps")
    flag("多流打不满网卡但 CPU 已 100%(协议栈/中断瓶颈)",
         None if multi is None else (multi < link_mb_s * 8 * 0.9),
         f"4 流聚合 {multi if multi is not None else '—'}Mbps / 线速 "
         f"{int(link_mb_s * 8)}Mbps;结合浸泡 CPU 曲线人工复核中断亲和性")
    flag("长跑后性能阶梯式下降(热降频/GC)",
         (var is not None and var > 20) or bool(throttled),
         f"波动 {var if var is not None else '—'}%;对照 soak.csv 温度-频率-吞吐三联曲线")
    md_create = _s3(results, lv, "08", "files_s_create")
    rand_w = _s3(results, lv, "04", "rand_write_w_iops")
    small_flag = None
    if md_create is not None and rand_w:
        small_flag = md_create < rand_w * 0.1
    flag("小文件 IOPS 远低于裸盘(FS/RAID 元数据开销异常)", small_flag,
         f"create {md_create if md_create is not None else '—'} files/s vs "
         f"4K 随机写 {rand_w if rand_w is not None else '—'} IOPS;<10% 视为异常")

    # ---- 汇总 ----
    counts = {"pass": 0, "fail": 0, "skipped": 0, "blocked": 0,
              "pending": 0, "running": 0, "manual_pending": 0}
    for c in CASES:
        st = (results.get(c.id) or {}).get("status", "pending")
        counts[st] = counts.get(st, 0) + 1
    done = counts["pass"] + counts["fail"] + counts["skipped"]

    return {
        "run_id": run_id,
        "grades": grades,
        "four_questions": four,
        "red_flags": flags,
        "counts": counts,
        "total": len(CASES),
        "completed": done,
        "completion_pct": round(done / len(CASES) * 100, 1),
    }
