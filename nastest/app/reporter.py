"""报告生成 —— 方案 §7 报告结构。

基线信息卡 → 四问答 → 存储表 → 协议表 → 并发表 → 浸泡小时表
→ §8 评级表 → 红旗清单 → 人工项指引 → 证据位置。
Markdown 落盘(report.md);HTML 由 report.html 模板渲染(report_context)。
"""
import csv
import time
from pathlib import Path

from . import config, db, evidence, raid
from .registry import BY_ID, CASES
from .scorer import score_run

STATUS_CN = {"pass": "✅ 完成", "fail": "❌ 失败", "skipped": "⏭️ 跳过",
             "blocked": "🚫 受阻", "pending": "⬜ 未测", "running": "🔄 运行中",
             "manual_pending": "🙋 待人工"}

TIER_CN = {"peak": "峰值", "sustained": "持续", "breaking": "极限容量"}

# RAID 细分展示顺序
RAID_ORDER = ["RAID0", "RAID1", "RAID5", "RAID6", "RAID10"]

# 新版子组表:(组内序号, 负载名, bs/QD/jobs, 指标前缀, 主方向 r/w/mixed)
# 用例编号 = S3-<RAID级别>-<序号>,扁平指标键,级别随组名携带
_STORAGE_ROWS = [
    ("01", "顺序读", "1M / QD32 / 4jobs", "seq_read", "r"),
    ("02", "顺序写", "1M / QD32 / 4jobs", "seq_write", "w"),
    ("03", "4K 随机读", "4K / QD32 / 4jobs", "rand_read", "r"),
    ("04", "4K 随机写", "4K / QD32 / 4jobs", "rand_write", "w"),
    ("05", "4K 混合 70/30", "4K / QD64 / 4jobs", "randrw", "mixed"),
    ("06", "4K 随读 QD1 延迟", "4K / QD1 / 1job", "qd1_lat", "r"),
    ("07", "4K 写+fsync 延迟", "4K / QD1 / 1job", "fsync_lat", "w"),
]

# 旧编号(仅未迁移数据兜底;启动迁移正常时不会触发)
_LEGACY_ROWS = [
    ("S3-01", "顺序读", "1M / QD32 / 4jobs", "seq_read", "r"),
    ("S3-02", "顺序写", "1M / QD32 / 4jobs", "seq_write", "w"),
    ("S3-03", "4K 随机读", "4K / QD32 / 4jobs", "rand_read", "r"),
    ("S3-04", "4K 随机写", "4K / QD32 / 4jobs", "rand_write", "w"),
    ("S3-05", "4K 混合 70/30", "4K / QD64 / 4jobs", "randrw", "mixed"),
    ("S3-06", "4K 随读 QD1 延迟", "4K / QD1 / 1job", "qd1_lat", "r"),
    ("S3-07", "4K 写+fsync 延迟", "4K / QD1 / 1job", "fsync_lat", "w"),
]


def _fmt(v, unit: str = "", nd: int | None = None) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and nd is not None:
        return f"{v:.{nd}f}{unit}"
    return f"{v}{unit}"


def _metrics(results: dict, case_id: str) -> dict:
    r = results.get(case_id) or {}
    return r.get("metrics") or {}


def _m(m: dict, key: str):
    v = m.get(key)
    return v if isinstance(v, (int, float)) else None


# ---------------- 表格组装 ----------------
def _raid_levels(results: dict) -> list[str]:
    """汇总所有 S3 用例实际测过的 RAID 级别(按 RAID0/1/5/6/10 排序)。"""
    seen: list[str] = []
    for r in results.values():
        m = r.get("metrics") or {}
        for lv in list(m.get("raid_levels_tested") or []) + \
                ([m["raid_level_tested"]] if m.get("raid_level_tested") else []):
            if lv and lv not in seen:
                seen.append(lv)

    def key(lv: str):
        return (0, RAID_ORDER.index(lv)) if lv in RAID_ORDER else (1, lv)
    return sorted(seen, key=key)


def _has_raid_keys(results: dict) -> bool:
    for r in results.values():
        m = r.get("metrics") or {}
        if any(k.startswith("raid_") and
               k not in ("raid_level_tested", "raid_levels_tested") for k in m):
            return True
    return False


def _storage_row(m: dict, cid: str, name: str, spec: str, prefix: str,
                 side: str, raid: str, keyfn) -> dict:
    """按 keyfn(指标 key) 取值;keyfn 决定读 raid_<级别>_ 前缀还是主指标。"""
    if side == "mixed":
        mb_s = None
        rmb, wmb = keyfn(f"{prefix}_r_mbps"), keyfn(f"{prefix}_w_mbps")
        if rmb is not None and wmb is not None:
            mb_s = round(rmb + wmb, 1)
        iops = keyfn(f"{prefix}_mixed_iops")
    else:
        mb_s = keyfn(f"{prefix}_{side}_mbps")
        iops = keyfn(f"{prefix}_{side}_iops")
    return {
        "case_id": cid, "case": name, "spec": spec, "raid": raid,
        "mb_s": mb_s, "iops": iops,
        "p50": keyfn(f"{prefix}_{side}_p50_us"),
        "p99": keyfn(f"{prefix}_{side}_p99_us"),
        "cpu": keyfn(f"{prefix}_fio_cpu_pct"),
        "note": ("fsync 延迟 "
                 f"{_fmt(keyfn(f'{prefix}_fsync_lat_us'), 'μs')}"
                 if prefix == "fsync_lat" else ""),
    }


def storage_table(results: dict, cfg: dict) -> dict:
    """存储子系统表,按 RAID 子组分组(RAID0/1/5/6/10 各自一张子表)。

    返回 {"multi": bool, "levels": [{"raid":…, "rows":[…]}], "hint": str}。
    - 子组编号 S3-<级别>-01..08 有数据:每个测过的 RAID 级别一张子表(扁平指标);
    - 仅存旧版 raid_<级别>_ 命名空间数据(未迁移):按旧键回退渲染;
    - 全空:单张空表,RAID 标签取运行配置 raid_level。
    """
    levels = [lv for lv in RAID_ORDER
              if any(_metrics(results, raid.level_case(lv, n))
                     for n, *_ in _STORAGE_ROWS)]
    if levels:
        out_levels = []
        for lv in levels:
            rows = []
            for n, name, spec, prefix, side in _STORAGE_ROWS:
                cid = raid.level_case(lv, n)
                m = _metrics(results, cid)
                if not m:
                    rows.append({"case_id": cid, "case": name, "spec": spec,
                                 "raid": lv, "empty": True})
                    continue
                rows.append(_storage_row(m, cid, name, spec, prefix, side, lv,
                                         lambda k, m=m: _m(m, k)))
            out_levels.append({"raid": lv, "rows": rows})
        return {"multi": len(out_levels) > 1, "levels": out_levels,
                "hint": ("每个 RAID 子组一张子表。切换阵列级别后运行对应 RAID 组的"
                         "「▶ 顺序运行本组」即可补入新 RAID 数据;执行前框架自动校验阵列级别,"
                         "不符会拒跑(防止数据串组)。")}
    if _has_raid_keys(results):  # 旧版 raid_<级别>_ 命名空间数据(迁移兜底)
        out_levels = []
        for lv in _raid_levels(results):
            rows = []
            for cid, name, spec, prefix, side in _LEGACY_ROWS:
                m = _metrics(results, cid)
                rows.append(_storage_row(
                    m, cid, name, spec, prefix, side, lv,
                    lambda k, m=m, lv=lv: _m(m, f"raid_{lv}_{k}")))
            out_levels.append({"raid": lv, "rows": rows})
        return {"multi": True, "levels": out_levels, "hint": "(旧版累计数据)"}
    # 全空:占位单表
    raid_lbl = str(cfg.get("raid_level") or "?")
    rows = [{"case_id": "", "case": name, "spec": spec, "raid": raid_lbl, "empty": True}
            for _n, name, spec, _p, _s in _STORAGE_ROWS]
    return {"multi": False, "levels": [{"raid": raid_lbl, "rows": rows}], "hint": ""}


def protocol_table(results: dict) -> list[dict]:
    R: list[dict] = []

    def add(proto, load, mb_s=None, iops=None, p99=None, cpu=None, enc="", note=""):
        R.append({"proto": proto, "load": load, "mb_s": mb_s, "iops": iops,
                  "p99": p99, "cpu": cpu, "enc": enc, "note": note})

    s51 = _metrics(results, "S5-01")
    add("SMB 纯软件", "大文件写", _m(s51, "write_mbps"), enc="否")
    add("SMB 纯软件", "大文件读", _m(s51, "read_mbps"), enc="否")
    s52 = _metrics(results, "S5-02")
    add("SMB seal", "大文件写", _m(s52, "seal_write_mbps"), enc="是",
        note=f"较明文损失 {_fmt(_m(s52, 'encrypt_loss_write_pct'), '%')}")
    add("SMB seal", "大文件读", _m(s52, "seal_read_mbps"), enc="是",
        note=f"较明文损失 {_fmt(_m(s52, 'encrypt_loss_read_pct'), '%')}")
    for cid, proto in (("S5-03", "SMB 挂载"), ("S5-04", "NFS 挂载")):
        m = _metrics(results, cid)
        add(proto, "fio 顺序写", _m(m, "seq_write_w_mbps"),
            cpu=_m(m, "seq_write_fio_cpu_pct"),
            note="(direct=0 回退,含客户端缓存)" if m.get("direct_fallback") else "")
        add(proto, "fio 顺序读", _m(m, "seq_read_r_mbps"),
            cpu=_m(m, "seq_read_fio_cpu_pct"))
        add(proto, "fio 混合 IOPS", None, iops=_m(m, "randrw_mixed_iops"))
    s55 = _metrics(results, "S5-05")
    add("FTP", "上传", _m(s55, "upload_mbps"))
    add("FTP", "下载", _m(s55, "download_mbps"))
    s56 = _metrics(results, "S5-06")
    add("WebDAV", "PUT", _m(s56, "upload_mbps"))
    add("WebDAV", "GET", _m(s56, "download_mbps"))
    s58 = _metrics(results, "S5-08")
    add("rsync(SSH)", "备份推送", _m(s58, "rsync_mbps"),
        note="含 SSH 加密开销")
    s59 = _metrics(results, "S5-09")
    if s59:
        add("SMB 小文件", "1 万文件(4-64K)",
            note=(f"创建 {_fmt(_m(s59, 'small_create_files_s'))} / "
                  f"读 {_fmt(_m(s59, 'small_read_files_s'))} / "
                  f"删除 {_fmt(_m(s59, 'small_delete_files_s'))} files/s"))
    return R


def scale_table(results: dict) -> list[dict]:
    rows = []
    for cid, kind in (("S6-01", "write"), ("S6-02", "read")):
        m = _metrics(results, cid)
        curve = m.get(f"scale_{kind}_curve")
        if not isinstance(curve, list):
            continue
        for c in curve:
            rows.append({
                "kind": "并发写" if kind == "write" else "并发读",
                "n": c.get("n"), "mbps": c.get("mbps"),
                "cpu": c.get("cpu_pct"),
                "net": (None if c.get("net_rx_mbps") is None
                        else round(c.get("net_rx_mbps", 0) + c.get("net_tx_mbps", 0), 1)),
            })
    return rows


def inflections(results: dict) -> str:
    infl_w = _m(_metrics(results, "S6-01"), "write_inflection_n")
    infl_r = _m(_metrics(results, "S6-02"), "read_inflection_n")
    if infl_w is None and infl_r is None:
        return ""
    return f"拐点: 写 N={_fmt(infl_w)}, 读 N={_fmt(infl_r)}(§8:≥8 路优)"


def soak_hours(run_id: int) -> list[dict]:
    """读 S8-01 soak.csv,按小时聚合。"""
    p = evidence.case_dir(run_id, "S8-01") / "soak.csv"
    if not p.exists():
        return []
    samples = []
    with open(p, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                samples.append({k: float(v) if k != "ts" else v
                                for k, v in row.items() if v not in (None, "")})
            except ValueError:
                continue
    if not samples:
        return []
    t0 = samples[0].get("t", 0)
    hours: dict[int, list[dict]] = {}
    for s in samples:
        h = int((s.get("t", t0) - t0) // 3600)
        hours.setdefault(h, []).append(s)
    out, prev_freq = [], None
    for h in sorted(hours):
        grp = hours[h]

        def avg(key):
            vals = [s[key] for s in grp if isinstance(s.get(key), (int, float))]
            return round(sum(vals) / len(vals), 1) if vals else None

        freq = avg("freq_mhz")
        events = []
        if prev_freq and freq and freq < prev_freq * 0.9:
            events.append(f"频率下降 {prev_freq}→{freq}MHz")
        tmax = max((s["temp_c"] for s in grp if isinstance(s.get("temp_c"), (int, float))),
                   default=None)
        if tmax is not None and tmax >= 85:
            events.append(f"最高温 {tmax}℃")
        out.append({
            "hour": h, "mb_s": None if avg("disk_read_mbps") is None
            else round((avg("disk_read_mbps") or 0) + (avg("disk_write_mbps") or 0), 1),
            "iops": avg("disk_iops"), "temp_c": avg("temp_c"),
            "temp_max": tmax, "freq_mhz": freq,
            "cpu_pct": avg("cpu_pct"), "events": ";".join(events) or "—",
        })
        prev_freq = freq
    return out


def manual_guidance(results: dict) -> list[dict]:
    """人工项指引:manual_pending / blocked 的用例 + 操作指导。"""
    out = []
    for c in CASES:
        st = (results.get(c.id) or {}).get("status", "pending")
        if st in ("manual_pending", "blocked") or (c.mode == "manual" and st == "pending"):
            out.append({"id": c.id, "title": c.title, "status": st,
                        "status_cn": STATUS_CN.get(st, st), "guide": c.guide})
    return out


def baseline_card(results: dict) -> dict:
    return _metrics(results, "S0-01").get("baseline") or {}


# ---------------- 测试方法 / 原理 / 命令(报告附录于各表之后) ----------------
def methodology(cfg: dict) -> dict:
    """按报告小节给出:测试步骤方法、底层原理、实际使用的命令。

    命令中的参数取自当前任务配置(runtime/size/轮数/浸泡时长等),与框架
    实际执行一致;逐条用例的完整命令日志另见证据 stdout.log。
    """
    rounds = int(cfg.get("rounds", 3))
    rt = int(cfg.get("fio_runtime", 60))
    size = cfg.get("fio_size", "4G")
    warm = int(cfg.get("warmup_seconds", 30))
    tdir = cfg.get("device_test_dir", "/vol2/.nastest_perf")
    soak_min = int(cfg.get("soak_minutes", 240))
    soak_iv = int(cfg.get("soak_sample_interval", 60))
    soak_secs = soak_min * 60
    ip = cfg.get("device_ip", "<NAS_IP>")
    vol = cfg.get("volume_path", "/vol2")
    smb_user = cfg.get("smb_user", "<user>")
    ssh_user = cfg.get("ssh_user", "<user>")
    ports = cfg.get("iperf_ports", [5201])
    p0 = ports[0] if ports else 5201

    return {
        "storage": {
            "method": [
                f"设备端 fio,direct=1 直通存储(绕开页缓存),单 job 测试文件 {size}(需大于物理内存,防止缓存作假);",
                f"每个 profile 先热身 {warm}s,再正式跑 {rounds} 轮取中位数,轮间波动 >10% 自动标记可信度下降;",
                "7 个负载画像:顺序读/写(1M 大块)、4K 随机读/写、4K 混合读写 70/30、QD1 最低延迟、fsync 提交延迟;",
                "S3-BASE-01 自动从 /proc/mdstat 识别 RAID 成员盘做只读裸盘基线,用于换算文件系统层损耗与 RAID 效率;",
                "S3 分为 basic / RAID0 / RAID1 / RAID5 / RAID6 / RAID10 子组:人工切换阵列后运行对应组,"
                "执行前自动校验 /proc/mdstat 阵列级别,不符拒跑,防止数据串组。",
            ],
            "principle": [
                "direct=1 + 文件>内存,确保测的是磁盘/RAID 真实性能而非内存缓存;",
                "1M 大块顺序 IO 反映带宽上限(受盘顺序速率与 RAID 条带/奇偶校验开销制约);4K 随机 IO 反映 IOPS 与延迟(受寻道/队列深度制约);",
                "iodepth×numjobs 把队列打满以逼出设备峰值;QD1 单队列给出最低延迟(单路径极限);fsync=1 测每次落盘提交延迟,是数据库类负载的关键指标;",
                "percentile(50/90/99/99.9)延迟用于暴露长尾;CPU% 用于判断算力是否先于存储成为瓶颈。",
            ],
            "commands": [
                f"# 顺序读(S3-<RAID级别>-01),其余 profile 仅 --rw/--bs/--iodepth 不同\n"
                f"fio --name=seq_read --directory={tdir} --size={size} --rw=read --bs=1M "
                f"--iodepth=32 --numjobs=4 --direct=1 --time_based --runtime={rt} "
                f"--group_reporting --output-format=json",
                f"# fsync 提交延迟(S3-<RAID级别>-07)\n"
                f"fio --name=fsync_lat --directory={tdir} --size={size} --rw=write --bs=4k "
                f"--iodepth=1 --numjobs=1 --direct=1 --fsync=1 --time_based --runtime={rt} "
                f"--group_reporting --output-format=json",
                f"# 裸盘读基线(S3-BASE-01,sdX 由 /proc/mdstat 自动识别,只读安全)\n"
                f"fio --name=rawread --filename=/dev/sdX --rw=read --bs=128k "
                f"--iodepth=32 --numjobs=1 --direct=1 --time_based --runtime={rt} --output-format=json",
            ],
        },
        "protocol": {
            "method": [
                "SMB 两条路径:smbprotocol 纯软件直连(无需 root,兜底)与内核 mount -t cifs(cache=none、rsize/wsize=1M)后本机 fio;",
                "NFS:设备端自动写 /etc/exports 并 exportfs -ra,本机 mount vers=4.1 nconnect=8 后 fio;",
                "FTP(ftplib STOR/RETR)、WebDAV(httpx PUT/GET)、rsync(SSH)大文件计时;SMB 小文件 1 万个 4K~64K 写/读/删计时;",
                "SMB seal(加密)在同一大文件上开/关各传一次,计算加密损失%。",
            ],
            "principle": [
                "同一大文件在不同协议下计时,横向对比协议封装/加密/网络往返带来的开销;",
                "与本地卷(S3)吞吐相比即协议效率;mount 版用 direct/cache=none 排除客户端缓存干扰;",
                "加密损失反映 AES 硬件指令(aes-ce)是否生效:损失腰斩以上通常说明走了纯软件加密(红旗)。",
            ],
            "commands": [
                f"# SMB 挂载(S5-03)\n"
                f"mount -t cifs //{ip}/<share> /mnt/nastest_smb "
                f"-o username={smb_user},password=***,vers=3.1.1,cache=none,"
                f"rsize=1048576,wsize=1048576",
                f"# NFS 挂载(S5-04,nconnect=8)\n"
                f"mount -t nfs -o vers=4.1,rsize=1048576,wsize=1048576,nconnect=8 "
                f"{ip}:{vol}/nastest_nfs_export /mnt/nastest_nfs",
                f"# rsync 备份(S5-08,sshpass 认证)\n"
                f"sshpass -p *** rsync -a --stats -e 'ssh -o StrictHostKeyChecking=no' "
                f"src/ {ssh_user}@{ip}:{tdir}/rsync_dst/",
                "# FTP/WebDAV/SMB 纯软件由 Python ftplib/httpx/smbprotocol 计时,无独立 CLI;"
                "挂载成功后在挂载点跑与 S3 相同的 fio 子集(顺序读/写/混合)",
            ],
        },
        "scale": {
            "method": [
                "smbprotocol 线程池按 N=1/2/4/8 并发 SMB 顺序写(或先预写再并发读),每路一个独立大文件;",
                "每个 N 档开始前/后对设备打快照(/proc/stat 与网卡计数),计算该档的 CPU%、网络吞吐;",
                "汇总 N×聚合吞吐曲线,找到吞吐不再随并发增长的拐点。",
            ],
            "principle": [
                "并发扩展性暴露单核/协议栈/中断处理瓶颈:弱 ARM 核常在低并发就先饱和;",
                "拐点处的资源占用(CPU/网络)即定位瓶颈层;§8 以 ≥8 路仍线性增长为优。",
            ],
            "commands": [
                "# 并发由本机 Python 线程池驱动(smbprotocol),设备端每档前后采样:\n"
                "head -1 /proc/stat                                        # CPU 利用率\n"
                "cat /sys/class/net/<nic>/statistics/rx_bytes tx_bytes     # 网络吞吐",
            ],
        },
        "soak": {
            "method": [
                f"三路常驻压力并行:stress-ng CPU 满载 + fio 4K 混合读写(randrw 70/30)+(本机有 iperf3 时)网络打流,持续 {soak_min} 分钟;",
                f"采样器每 {soak_iv}s 记录 温度/频率/负载/内存/CPU%/磁盘 IOPS 与带宽/网络 → soak.csv;",
                "S8-02 解析 soak.csv:首末段吞吐波动%、温度-频率-吞吐相关性判降频、资源爬升检查。",
            ],
            "principle": [
                "长时间综合满载才能暴露热降频、GC、资源泄漏等短时测试看不到的问题;",
                "§5.8 达标线:吞吐波动 <10% 且无持续热降频;频率下降伴随吞吐下降即判定热降频(不通过)。",
            ],
            "commands": [
                f"# CPU 压力(后台)\nstress-ng --cpu $(nproc) --timeout {soak_secs}s --metrics-brief",
                f"# 持续混合 IO(后台)\n"
                f"fio --name=soak --directory={tdir} --size=1G --rw=randrw --rwmixread=70 "
                f"--bs=4k --iodepth=32 --numjobs=4 --direct=1 --time_based --runtime={soak_secs}",
                f"# 网络压力(本机 iperf3)\niperf3 -c {ip} -p {p0} -t {soak_secs}",
            ],
        },
    }


# ---------------- Markdown 渲染助手 ----------------
def _render_method_md(A, entry: dict) -> None:
    """在某个测试表之后追加:测试方法 / 底层原理 / 使用的命令。"""
    if not entry:
        return
    A("**📖 测试方法(步骤)**")
    A("")
    for i, s in enumerate(entry.get("method", []), 1):
        A(f"{i}. {s}")
    A("")
    A("**底层原理**")
    A("")
    for s in entry.get("principle", []):
        A(f"- {s}")
    A("")
    A("**使用的命令**(逐条完整命令日志见证据 stdout.log)")
    A("")
    for c in entry.get("commands", []):
        A("```bash")
        A(c)
        A("```")
    A("")


def _render_storage_md(A, st: dict) -> None:
    """存储表:multi 时每个 RAID 级别一张子表,否则单表含 RAID 列。"""
    if st.get("hint"):
        A(f"> ℹ️ {st['hint']}")
        A("")
    if st["multi"]:
        for lv in st["levels"]:
            A(f"### {lv['raid']}")
            A("")
            A("| 用例 | bs/QD/jobs | 吞吐(MB/s) | IOPS | P50(μs) | P99(μs) | CPU% | 备注 |")
            A("|---|---|---|---|---|---|---|---|")
            for r in lv["rows"]:
                A(f"| {r['case']} | {r['spec']} | {_fmt(r.get('mb_s'))} | "
                  f"{_fmt(r.get('iops'))} | {_fmt(r.get('p50'))} | {_fmt(r.get('p99'))} | "
                  f"{_fmt(r.get('cpu'))} | {r.get('note') or ''} |")
            A("")
    else:
        A("| RAID | 用例 | bs/QD/jobs | 吞吐(MB/s) | IOPS | P50(μs) | P99(μs) | CPU% | 备注 |")
        A("|---|---|---|---|---|---|---|---|---|")
        for r in st["levels"][0]["rows"]:
            if r.get("empty"):
                A(f"| {r['raid']} | {r['case']} | {r['spec']} | — | — | — | — | — | 未测 |")
                continue
            A(f"| {r['raid']} | {r['case']} | {r['spec']} | {_fmt(r.get('mb_s'))} | "
              f"{_fmt(r.get('iops'))} | {_fmt(r.get('p50'))} | {_fmt(r.get('p99'))} | "
              f"{_fmt(r.get('cpu'))} | {r.get('note') or ''} |")
        A("")


# ---------------- 上下文与 Markdown ----------------
def report_context(run_id: int) -> dict:
    run = db.get_run(run_id)
    results = {r["case_id"]: r for r in db.list_results(run_id)}
    score = score_run(run_id)
    rows = []
    for c in CASES:
        r = results.get(c.id) or {}
        rows.append({
            "case": c, "result": r, "status": r.get("status", "pending"),
            "mode": c.mode, "tier": c.tier,
            "duration_s": round(r["duration_ms"] / 1000, 1) if r.get("duration_ms") else None,
        })
    return {
        "run": run, "cfg": run["config"], "rows": rows, "score": score,
        "baseline": baseline_card(results),
        "tables": {
            "storage": storage_table(results, run["config"]),
            "protocol": protocol_table(results),
            "scale": scale_table(results),
            "scale_note": inflections(results),
            "soak": soak_hours(run_id),
        },
        "method": methodology(run["config"]),
        "manual_rows": manual_guidance(results),
        "domains": config.DOMAIN_ORDER, "domain_names": config.DOMAIN_NAMES,
        "tier_cn": TIER_CN, "status_cn": STATUS_CN,
        "evidence_dir": f"data/evidence/run_{run_id}",
    }


def generate_markdown(run_id: int) -> Path:
    ctx = report_context(run_id)
    run, cfg, score = ctx["run"], ctx["cfg"], ctx["score"]
    b = ctx["baseline"]
    T = ctx["tables"]
    now = time.strftime("%Y-%m-%d %H:%M")
    L: list[str] = []
    A = L.append

    A(f"# {run['name']} — ARM NAS 极限性能测试报告")
    A("")
    A(f"> 完成度 {score['completion_pct']}%({score['completed']}/{score['total']} 用例;"
      f"通过 {score['counts'].get('pass', 0)} / 失败 {score['counts'].get('fail', 0)} / "
      f"受阻 {score['counts'].get('blocked', 0)} / 待人工 "
      f"{score['counts'].get('manual_pending', 0) + score['counts'].get('pending', 0)})"
      f"——生成于 {now}")
    A("")

    # ---- 基线信息卡 ----
    A("## 一、基线信息(§3.2)")
    A("")
    A(f"- **设备**: {cfg.get('device_model', '?')}({cfg.get('device_ip', '?')}),"
      f"存储 {cfg.get('disk_desc', '')} / {cfg.get('raid_level', '')} / 卷 {cfg.get('volume_path', '')}")
    if b:
        A(f"- **内核**: {b.get('uname', '—')}")
        A(f"- **CPU**: {b.get('cores', '—')} 核,大小核={'是' if b.get('big_little') else '否'},"
          f"最高频率 {b.get('max_freq_khz', '—')}kHz,governor={b.get('governor', '—')}")
        A(f"- **AES 硬件指令(aes-ce)**: {'已启用' if b.get('aes_ce') else '❌ 未启用(红旗)'},    "
          f"内存 {b.get('mem_mb', '—')}MB,页大小 {b.get('page_size', '—')}B")
        A(f"- **卷**: `{b.get('fs_line', '—')}`")
        A(f"- **网卡**: {b.get('nic', '—')} @ {b.get('nic_speed_mbps', '—')}Mbps"
          f"(线速≈{int(cfg.get('nic_link_mbps', 1000)) // 8}MB/s)")
        for s in b.get("smart", []):
            A(f"- **SMART** {s}")
        vers = b.get("tool_versions") or {}
        A("- **工具**: " + ", ".join(f"{k} {v.split()[-1] if v else '?'}" for k, v in vers.items()))
    else:
        A("- (未执行 S0-01 基线采集)")
    A("")

    # ---- 四问答 ----
    A("## 二、四个核心问题(§1)")
    A("")
    for fq in score["four_questions"]:
        A(f"**{fq['q']}**")
        A(f"- 结论:{fq['a']}")
        A(f"- 判读:{fq['detail']}")
        A("")

    # ---- 存储表(按 RAID 细分) ----
    A("## 三、存储子系统(§5.3,中位数)")
    A("")
    _render_storage_md(A, T["storage"])
    _render_method_md(A, ctx["method"].get("storage"))

    # ---- 协议表 ----
    A("## 四、文件协议(§5.5)")
    A("")
    A("| 协议 | 负载 | 吞吐(MB/s) | IOPS | P99(μs) | CPU% | 加密 | 备注 |")
    A("|---|---|---|---|---|---|---|---|")
    for r in T["protocol"]:
        A(f"| {r['proto']} | {r['load']} | {_fmt(r.get('mb_s'))} | {_fmt(r.get('iops'))} | "
          f"{_fmt(r.get('p99'))} | {_fmt(r.get('cpu'))} | {r.get('enc') or '—'} | {r.get('note') or ''} |")
    A("")
    _render_method_md(A, ctx["method"].get("protocol"))

    # ---- 并发表 ----
    A("## 五、并发扩展(§5.6)")
    A("")
    if T["scale"]:
        A("| 类型 | 客户端数 | 聚合吞吐(MB/s) | CPU% | 网络(MB/s) |")
        A("|---|---|---|---|---|")
        for r in T["scale"]:
            A(f"| {r['kind']} | {r['n']} | {_fmt(r.get('mbps'))} | {_fmt(r.get('cpu'))} | "
              f"{_fmt(r.get('net'))} |")
        if T.get("scale_note"):
            A("")
            A(T["scale_note"])
    else:
        A("(未执行 S6-01/S6-02)")
    A("")
    _render_method_md(A, ctx["method"].get("scale"))

    # ---- 浸泡小时表 ----
    A("## 六、浸泡(§5.8)")
    A("")
    soak = T["soak"]
    if soak:
        A("| 时段 | 吞吐(MB/s) | IOPS | CPU 温度(℃) | CPU 频率(MHz) | CPU% | 功耗 | 异常事件 |")
        A("|---|---|---|---|---|---|---|---|")
        for r in soak:
            A(f"| 第{r['hour']}h | {_fmt(r.get('mb_s'))} | {_fmt(r.get('iops'))} | "
              f"{_fmt(r.get('temp_c'))} | {_fmt(r.get('freq_mhz'))} | {_fmt(r.get('cpu_pct'))} | "
              f"— | {r.get('events') or '—'} |")
        A("")
        A("*功耗列需功率计人工录入(S9-01);完整逐分钟样本见证据 `S8-01/soak.csv`。*")
    else:
        A("(未执行 S8-01 浸泡)")
    A("")
    _render_method_md(A, ctx["method"].get("soak"))

    # ---- §8 评级 ----
    A("## 七、评级(§8 参考基线:优 / 良 / 及格 / 差)")
    A("")
    A("| 指标 | 实测 | 评级 | 依据 |")
    A("|---|---|---|---|")
    for g in score["grades"]:
        A(f"| {g['name']} | {g['value']} | **{g['grade']}** | {g['basis']} |")
    A("")

    # ---- 红旗 ----
    A("## 八、ARM 红旗信号(§8)")
    A("")
    for f in score["red_flags"]:
        mark = ("🚩 触发" if f["triggered"]
                else ("✅ 未触发" if f["triggered"] is False else "❔ 数据不足"))
        A(f"- **{mark}** — {f['desc']}")
        A(f"  - {f['detail']}")
    A("")

    # ---- 人工项 ----
    if ctx["manual_rows"]:
        A("## 九、待人工项与操作指引")
        A("")
        for mr in ctx["manual_rows"]:
            A(f"### {mr['id']} {mr['title']}({mr['status_cn']})")
            A("")
            A((mr["guide"] or "(见用例详情)").strip())
            A("")

    # ---- 证据 ----
    A("## 十、证据位置")
    A("")
    A(f"每条用例的命令日志(stdout.log)、指标(metrics.json)与原始输出保存在 "
      f"`{ctx['evidence_dir']}/cases/<用例编号>/`;浸泡采样 `S8-01/soak.csv`,"
      f"基线卡 `S0-01/baseline.md`。data/ 目录含明文配置,勿外传。")
    A("")

    # ---- 用例明细附录 ----
    A("## 附录:用例明细")
    for dom in config.DOMAIN_ORDER:
        dom_rows = [r for r in ctx["rows"] if r["case"].domain == dom]
        if not dom_rows:
            continue
        A("")
        A(f"### {dom} {config.DOMAIN_NAMES[dom]}")
        A("")
        A("| 编号 | 测试项 | 测试方法 | 层次 | 模式 | 状态 | 实测结果 | 耗时 |")
        A("|---|---|---|---|---|---|---|---|")
        for row in dom_rows:
            c = row["case"]
            st = STATUS_CN.get(row["status"], row["status"])
            actual = (row["result"].get("actual") or "").replace("\n", " ").replace("|", "/")
            if len(actual) > 110:
                actual = actual[:110] + "…"
            steps = (c.steps or "").replace("\n", " ").replace("|", "/")
            dur = f"{row['duration_s']}s" if row["duration_s"] is not None else "—"
            A(f"| {c.id} | {c.title} | {steps} | {TIER_CN.get(c.tier, c.tier)} | "
              f"{'自动' if c.mode == 'auto' else '人工'} | {st} | {actual} | {dur} |")
    A("")
    A("---")
    A("*本报告由 ARM-NAS 极限性能测试框架生成;评级依据 docs/ARM-NAS-极限性能测试方案.md §8;"
      "受阻/待人工项补测后重新生成。*")

    text = "\n".join(L) + "\n"
    p = evidence.run_dir(run_id) / "report.md"
    p.write_text(text, encoding="utf-8")
    return p
