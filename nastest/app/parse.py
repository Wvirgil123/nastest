"""性能工具输出解析:fio --output=json 与 iperf3 -J。"""
import json
import re


def _extract_json(text: str) -> dict:
    """从可能混有非 JSON 行的输出中提取第一个 JSON 对象。"""
    start = text.find("{")
    if start < 0:
        raise ValueError("输出中未找到 JSON")
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    return json.loads(text[start:])


# ---------------- fio ----------------
def parse_fio_json(text: str) -> dict:
    """解析 fio --output=json 的汇总结果(group_reporting 单 job)。

    返回:read/write 各自的 mbps、iops、平均延迟、P50/P90/P99/P99.9(clat)。
    """
    data = _extract_json(text)
    jobs = data.get("jobs") or []
    if not jobs:
        raise ValueError("fio JSON 无 jobs")
    job = jobs[0]
    out: dict = {}
    for side in ("read", "write"):
        s = job.get(side) or {}
        if not s or not s.get("io_bytes") and not s.get("bw"):
            continue
        key = side[:1]  # r / w
        out[f"{key}_mbps"] = round((s.get("bw") or 0) / 1024.0, 2)   # KiB/s→MiB/s
        out[f"{key}_iops"] = round(s.get("iops") or 0, 1)
        lat = (s.get("lat_ns") or {}).get("mean")
        if lat:
            out[f"{key}_lat_us"] = round(lat / 1000.0, 2)
        pct = (s.get("clat_ns") or {}).get("percentile") or {}
        for p in ("50.000000", "90.000000", "99.000000", "99.900000"):
            if p in pct:
                tag = p.split(".")[0] if not p.startswith("99.9") else "999"
                out[f"{key}_p{tag}_us"] = round(pct[p] / 1000.0, 2)
    # CPU 占用(fio 自身记录,仅辅助参考):group_reporting 下 fio 报告的是
    # 各 job 的平均单核占用,×numjobs 换算为 fio 进程组总占用(单核倍数)。
    # 注意:不含内核态开销(RAID/文件系统线程、softirq),整机 CPU% 以
    # /proc/stat 快照(*_dev_cpu_pct)为准。
    usr = job.get("usr_cpu")
    sysv = job.get("sys_cpu")
    if usr is not None:
        try:
            njobs = max(1, int((job.get("job options") or {}).get("numjobs") or 1))
        except (TypeError, ValueError):
            njobs = 1
        out["fio_proc_cpu_pct"] = round(((usr or 0) + (sysv or 0)) * njobs, 1)
    return out


def parse_fio_fsync(text: str) -> dict:
    """fsync=1 用例:取 fsync 延迟分布。"""
    data = _extract_json(text)
    jobs = data.get("jobs") or []
    out: dict = {}
    if jobs:
        s = jobs[0].get("write") or {}
        out["w_iops"] = round(s.get("iops") or 0, 1)
        lat = (s.get("lat_ns") or {}).get("mean")
        if lat:
            out["w_lat_us"] = round(lat / 1000.0, 2)
        fs = jobs[0].get("fsync") or {}
        flat = (fs.get("lat_ns") or {}).get("mean")
        if flat:
            out["fsync_lat_us"] = round(flat / 1000.0, 2)
        pct = (fs.get("lat_ns") or {}).get("percentile") or {}
        for p in ("50.000000", "99.000000"):
            if p in pct:
                out[f"fsync_p{p.split('.')[0]}_us"] = round(pct[p] / 1000.0, 2)
    return out


# ---------------- iperf3 ----------------
def parse_iperf3_json(text: str) -> dict:
    """解析 iperf3 -J。TCP 返回 mbps/retransmits;UDP 返回 mbps/pps/jitter/丢包。"""
    data = _extract_json(text)
    if "error" in data:
        raise ValueError(f"iperf3 报错: {data['error']}")
    end = data.get("end") or {}
    out: dict = {}
    if "sum_received" in end:  # TCP
        bps = (end.get("sum_received") or {}).get("bits_per_second") or 0
        out["mbps"] = round(bps / 1e6, 1)
        out["mb_s"] = round(bps / 8 / 1024 / 1024, 1)
        out["retransmits"] = (end.get("sum_sent") or {}).get("retransmits", 0)
    elif "sum" in end:  # UDP
        s = end["sum"] or {}
        bps = s.get("bits_per_second") or 0
        secs = s.get("seconds") or 1
        out["mbps"] = round(bps / 1e6, 1)
        out["pps"] = round((s.get("packets") or 0) / secs)
        out["jitter_ms"] = round(s.get("jitter_ms") or 0, 3)
        out["lost_packets"] = s.get("lost_packets", 0)
        out["lost_percent"] = round(s.get("lost_percent") or 0, 2)
    return out


def parse_iperf3_multi(texts: list[str]) -> dict:
    """多个 iperf3 实例 JSON 聚合。"""
    total = 0.0
    retrans = 0
    detail = []
    for t in texts:
        d = parse_iperf3_json(t)
        total += d.get("mbps", 0)
        retrans += d.get("retransmits", 0)
        detail.append(d)
    return {"mbps": round(total, 1), "mb_s": round(total / 8, 1),
            "retransmits": retrans, "streams": detail}


def extract_number(pattern: str, text: str, cast=float, group: int = 1):
    """正则提取数值;未命中返回 None。"""
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return cast(m.group(group).replace(",", ""))
    except (ValueError, TypeError):
        return None
