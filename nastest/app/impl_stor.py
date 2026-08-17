"""S3 存储子系统 / S4 网络层 用例实现。"""
import re
import statistics
import time

from . import db
from .ctx import (Ctx, _pick_fio_engine, device_delta, device_nic,
                  device_snapshot, fio_cmd, fio_profile, iperf_cleanup,
                  iperf_local, iperf_multi_local, iperf_servers, record_rounds,
                  require_device_tool, require_local_sudo, require_local_tool,
                  rounds)
from .parse import parse_fio_json, parse_fio_json_multi

IMPL: dict[str, callable] = {}


def _impl(key: str):
    def deco(fn):
        IMPL[key] = fn
        return fn
    return deco


# ================= S3 RAID 子组校验(basic / RAID0/1/5/6/10) =================
def detect_md_raid(ctx: Ctx) -> str | None:
    """解析 /proc/mdstat 识别当前 md 阵列级别(RAID0/1/5/6/10);无阵列返回 None。

    只匹配阵列行(mdX : active raidN …)。注意首行 Personalities 是内核能力清单
    ([raid0] [raid1] …),不能参与匹配,否则永远误判为 RAID0。
    """
    md = ctx.sh("cat /proc/mdstat", note="识别当前 RAID 级别").out
    m = re.search(r"\bmd\d+\s*:\s*active\s+(?:linear|raid(\d+))", md)
    if not m or not m.group(1):
        return None
    return f"RAID{m.group(1)}"


def _verify_raid_group(ctx: Ctx) -> str | None:
    """执行前校验阵列级别与本用例所属 RAID 组一致,防止切换阵列前后数据串组。

    返回所属组名;basic 组(组名 None)不校验。检测不到 md 阵列时仅告警(可能是
    dm/硬件 RAID),按用例所在组记录;检测到但不一致 → blocked 拒跑。
    """
    from .raid import raid_group_of
    group = raid_group_of(ctx.case_id)
    if group is None:
        return None
    det = detect_md_raid(ctx)
    if det is None:
        ctx.log(f"[!] /proc/mdstat 未识别到 md 阵列(可能是 dm/硬 RAID),"
                f"无法自动校验,按本组 {group} 记录")
        return group
    if det != group:
        ctx.abort("blocked",
                  f"阵列级别不符:当前 /proc/mdstat 为 {det},本用例属于 {group} 组。"
                  f"请改跑 {det} 组的用例,或把阵列切换为 {group} 后再运行本组(防止数据串组)")
    ctx.log(f"[RAID] 校验通过:阵列级别 {det} 与本组一致")
    return group


# ================= S3 存储子系统(fio 矩阵,扁平指标,级别由用例编号携带) =================
@_impl("s3_seq_read")
def s3_seq_read(ctx: Ctx) -> str:
    _verify_raid_group(ctx)
    m = fio_profile(ctx, "seq_read")
    return (f"顺序读 1M QD32×4: {m.get('r_mbps')}MB/s (单轮值 {m.get('seq_read_r_mbps_rounds')}),"
            f" CPU(整机) {m.get('dev_cpu_pct')}%")


@_impl("s3_seq_write")
def s3_seq_write(ctx: Ctx) -> str:
    _verify_raid_group(ctx)
    m = fio_profile(ctx, "seq_write")
    return (f"顺序写 1M QD32×4: {m.get('w_mbps')}MB/s,CPU(整机) {m.get('dev_cpu_pct')}%"
            f"(RAID 写含奇偶校验开销,关注 CPU 是否瓶颈)")


@_impl("s3_seq_read_1job")
def s3_seq_read_1job(ctx: Ctx) -> str:
    _verify_raid_group(ctx)
    m = fio_profile(ctx, "seq_read_1job")
    return (f"顺序读 1M QD32×1(单流): {m.get('r_mbps')}MB/s,CPU(整机) {m.get('dev_cpu_pct')}%"
            f"(单流避免多 job 磁头交插,反映 RAID 真实顺序带宽;对比 01 多流)")


@_impl("s3_seq_write_1job")
def s3_seq_write_1job(ctx: Ctx) -> str:
    _verify_raid_group(ctx)
    m = fio_profile(ctx, "seq_write_1job")
    return (f"顺序写 1M QD32×1(单流): {m.get('w_mbps')}MB/s,CPU(整机) {m.get('dev_cpu_pct')}%"
            f"(单流顺序写真实带宽;对比 02 多流)")


@_impl("s3_rand_read")
def s3_rand_read(ctx: Ctx) -> str:
    _verify_raid_group(ctx)
    m = fio_profile(ctx, "rand_read")
    return (f"4K 随机读 QD32×4: {m.get('r_iops')} IOPS,平均延迟 {m.get('r_lat_us')}μs,"
            f"P99 {m.get('r_p99_us')}μs")


@_impl("s3_rand_write")
def s3_rand_write(ctx: Ctx) -> str:
    _verify_raid_group(ctx)
    m = fio_profile(ctx, "rand_write")
    return (f"4K 随机写 QD32×4: {m.get('w_iops')} IOPS,平均延迟 {m.get('w_lat_us')}μs,"
            f"P99 {m.get('w_p99_us')}μs")


@_impl("s3_randrw")
def s3_randrw(ctx: Ctx) -> str:
    _verify_raid_group(ctx)
    m = fio_profile(ctx, "randrw")
    return (f"4K 混合读写 70/30 QD64×4: 混合 {m.get('mixed_iops')} IOPS"
            f"(读 {m.get('r_iops')} / 写 {m.get('w_iops')});"
            f"读 P50/P90/P99/P99.9 = {m.get('r_p50_us')}/{m.get('r_p90_us')}/"
            f"{m.get('r_p99_us')}/{m.get('r_p999_us')}μs")


@_impl("s3_qd1_lat")
def s3_qd1_lat(ctx: Ctx) -> str:
    _verify_raid_group(ctx)
    m = fio_profile(ctx, "qd1_lat")
    return f"4K 随机读 QD1: 平均延迟 {m.get('r_lat_us')}μs,P99 {m.get('r_p99_us')}μs(单路径极限)"


@_impl("s3_fsync_lat")
def s3_fsync_lat(ctx: Ctx) -> str:
    _verify_raid_group(ctx)
    m = fio_profile(ctx, "fsync_lat")
    return (f"4K 写+fsync=1: fsync 延迟 {m.get('fsync_lat_us')}μs,"
            f"写 IOPS {m.get('w_iops')}(数据库类负载关键指标)")


@_impl("s3_metadata")
def s3_metadata(ctx: Ctx) -> str:
    _verify_raid_group(ctx)
    if not ctx.sh("command -v python3 || true").out.strip():
        ctx.abort("blocked", "设备无 python3,无法运行元数据脚本")
    tdir = ctx.cfg.get("device_test_dir", "/vol2/.nastest_perf")
    ctx.sh(f"mkdir -p {tdir}")
    script = (
        "import os, sys, time\n"
        "base, n = sys.argv[1], int(sys.argv[2])\n"
        "os.makedirs(base, exist_ok=True)\n"
        "t0=time.time()\n"
        "for i in range(n):\n"
        "    with open(f'{base}/f{i}.tmp','w') as f: f.write('x'*64)\n"
        "t1=time.time()\n"
        "for i in range(n): os.stat(f'{base}/f{i}.tmp')\n"
        "t2=time.time()\n"
        "for i in range(n):\n"
        "    with open(f'{base}/f{i}.tmp') as f: f.read()\n"
        "t3=time.time()\n"
        "for i in range(n): os.remove(f'{base}/f{i}.tmp')\n"
        "t4=time.time()\n"
        "print(f'CREATE {n/(t1-t0):.1f}')\n"
        "print(f'STAT {n/(t2-t1):.1f}')\n"
        "print(f'READ {n/(t3-t2):.1f}')\n"
        "print(f'DELETE {n/(t4-t3):.1f}')\n"
        "os.rmdir(base)\n"
    )
    ctx.sh(f"cat > /tmp/nastest_md.py <<'PYEOF'\n{script}PYEOF", note="写入元数据脚本")
    r = ctx.sh(f"python3 /tmp/nastest_md.py {tdir}/md_test 10000", timeout=1800,
               note="1 万文件 create/stat/read/delete")
    vals = {}
    for ln in r.out.splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0] in ("CREATE", "STAT", "READ", "DELETE"):
            try:
                vals[parts[0]] = float(parts[1])
            except ValueError:
                pass
    if len(vals) < 4:
        ctx.abort("fail", "元数据脚本输出不完整(见日志)")
    for k, v in vals.items():
        ctx.metric(f"files_s_{k.lower()}", v)
    ctx.sh("rm -f /tmp/nastest_md.py")
    return ("元数据 1 万文件: " +
            ", ".join(f"{k} {v:.0f} files/s" for k, v in vals.items()) +
            "(小文件 IOPS 远低于裸盘 → FS/RAID 元数据开销,§8 红旗判定依据)")


@_impl("s3_raw_read")
def s3_raw_read(ctx: Ctx) -> str:
    require_device_tool(ctx, "fio")
    md = ctx.sh("cat /proc/mdstat").out
    m = re.search(r"(sd[a-z]+)\d?\[\d+\]", md)
    if not m:
        ctx.abort("blocked", "无法从 /proc/mdstat 识别 RAID 成员盘")
    dev = f"/dev/{m.group(1)}"
    ctx.log(f"[只读测试] 目标成员盘 {dev}(read 不写盘,安全)")
    runtime = int(ctx.cfg.get("fio_runtime", 60))
    per = []
    for i in range(rounds(ctx)):
        r = ctx.sh(f"fio --name=rawread --filename={dev} --rw=read --bs=128k "
                   f"--ioengine={_pick_fio_engine(ctx)} --iodepth=32 --numjobs=1 "
                   f"--direct=1 --time_based --runtime={runtime} "
                   f"--output-format=json", timeout=runtime + 300, sudo=True,
                   note=f"裸盘读 第 {i + 1} 轮")
        from .parse import parse_fio_json
        try:
            per.append(parse_fio_json(r.out)["r_mbps"])
        except (ValueError, KeyError):
            ctx.abort("fail", "裸盘 fio 解析失败")
    med = record_rounds(ctx, "raw_read_mb_s", per)
    return (f"单裸盘 {dev} 顺序读: {med}MB/s"
            f"(用于计算 FS 层损耗与 RAID 效率,§5.3)")


# ---- 裸盘逐盘 + 全盘并发读写(S3-BASE-03/04) ----
# 参数对齐常用手工基准:单盘 bs=1M QD8 size=2G,并发每盘一个 job QD4 size=1G;
# 裸盘顺序吞吐非常稳定,单轮 30s + rounds 轮中位数足够,避免占用整盘过久。
_RAW_RUNTIME = 30
_RAW_SIZE_ONE = "2G"    # 单盘测试工作区(direct=1 直通,不经缓存)
_RAW_SIZE_ALL = "1G"    # 并发时每盘工作区


def _detect_raw_disks(ctx: Ctx) -> list[str]:
    """枚举数据裸盘(/dev/sd*):整盘、机械旋转(ROTA=1)、SATA/SAS 接口。

    排除分区、NVMe/eMMC/SD(ROTA=0)与 USB 外接盘,防止误测系统盘/外接盘。
    """
    r = ctx.sh("lsblk -dnpo NAME,TYPE,ROTA,TRAN",
               note="枚举裸盘(整盘/机械/SATA)")
    if r.rc != 0:   # 老版本 lsblk 无 TRAN 列
        r = ctx.sh("lsblk -dnpo NAME,TYPE,ROTA")
    disks = []
    for ln in r.out.splitlines():
        p = ln.split()
        if len(p) >= 3 and p[1] == "disk" and p[2] == "1" \
                and p[0].startswith("/dev/"):
            if len(p) < 4 or p[3].lower() in ("sata", "sas", "ata"):
                disks.append(p[0])
    disks.sort()
    if not disks:
        ctx.abort("blocked", "未检测到 SATA 机械裸盘(lsblk ROTA=1);"
                             "若盘在 USB/NVMe 上或 lsblk 输出异常,请人工核查后手动测试")
    want = int(ctx.cfg.get("disk_count") or 0)
    if want and len(disks) != want:
        ctx.log(f"[!] 检测到 {len(disks)} 块裸盘,配置 disk_count={want},"
                f"按实际检测的盘执行: {disks}")
    ctx.log(f"[裸盘] 目标盘: {disks}")
    return disks


def _md_member_devs(ctx: Ctx) -> set[str]:
    """/proc/mdstat 中活动 md 阵列的成员块设备名(含分区,如 sda1)。"""
    md = ctx.sh("cat /proc/mdstat").out
    return {m.group(1) for m in re.finditer(r"\b(sd[a-z]+\d*)\[\d+\]", md)}


def _used_parts(ctx: Ctx, dev: str) -> list[str]:
    """该盘(含分区)上的挂载点/swap;返回 ['sda1:/vol1', …] 形式,空=未占用。"""
    r = ctx.sh(f"lsblk -nlo NAME,MOUNTPOINT {dev}")
    used = []
    for ln in r.out.splitlines():
        p = ln.split(None, 1)
        if len(p) == 2 and p[1].strip():
            used.append(f"{p[0]}:{p[1].strip()}")
    return used


def _raw_disk_test(ctx: Ctx, rw: str) -> str:
    """rw=read/write:逐盘 fio + 全盘并发 fio,各 rounds 轮取中位数。"""
    require_device_tool(ctx, "fio")
    engine = _pick_fio_engine(ctx)
    disks = _detect_raw_disks(ctx)
    prefix = "raw_read" if rw == "read" else "raw_write"
    cn = "读" if rw == "read" else "写"
    side = rw[0]

    if rw == "write":
        members = _md_member_devs(ctx)
        problems = []
        for d in disks:
            name = d.rsplit("/", 1)[-1]
            md_hit = sorted(x for x in members if x == name or x.startswith(name))
            used = _used_parts(ctx, d)
            if md_hit:
                problems.append(f"{d} 是活动 md 阵列成员({','.join(md_hit)})")
            if used:
                problems.append(f"{d} 存在挂载/swap 分区({';'.join(used)})")
        if problems:
            ctx.abort("blocked",
                      "裸盘写为破坏性操作,以下盘仍在使用:\n- " + "\n- ".join(problems) +
                      "\n请先停止阵列(sudo mdadm --stop /dev/mdX)/卸载后再运行;"
                      "或改跑只读的 S3-BASE-03")
        ctx.log(f"[⚠ 破坏性操作] 将向每块裸盘头部区域写入({prefix}),"
                f"盘上数据将全部失效")
    ctx.metric(f"{prefix}_disks", [d.rsplit("/", 1)[-1] for d in disks])

    # 逐盘
    per_disk: dict[str, float] = {}
    for d in disks:
        name = d.rsplit("/", 1)[-1]
        vals = []
        for i in range(rounds(ctx)):
            if ctx.stop_requested():
                ctx.abort("fail", "用户中止")
            r = ctx.sh(f"fio --name={prefix}_{name} --filename={d} --rw={rw} --bs=1M "
                       f"--size={_RAW_SIZE_ONE} --ioengine={engine} --iodepth=8 "
                       f"--direct=1 --time_based --runtime={_RAW_RUNTIME} "
                       f"--output-format=json", timeout=_RAW_RUNTIME + 300,
                       sudo=True, note=f"裸盘{cn} {name} 第 {i + 1} 轮")
            if r.rc != 0:
                ctx.abort("fail", f"{d} 裸盘 fio 失败 rc={r.rc}(详见 stdout.log)")
            try:
                vals.append(parse_fio_json(r.out)[f"{side}_mbps"])
            except (ValueError, KeyError):
                ctx.abort("fail", f"{d} 裸盘 fio 解析失败(输出见 stdout.log)")
        per_disk[name] = record_rounds(ctx, f"{prefix}_{name}_mb_s", vals)

    # 全盘并发(每盘一个 job)
    nic = device_nic(ctx)
    jobs = " ".join(f"--name=d{i + 1} --filename={d}" for i, d in enumerate(disks))
    cmd = (f"fio --rw={rw} --bs=1M --size={_RAW_SIZE_ALL} --ioengine={engine} "
           f"--iodepth=4 --direct=1 --time_based --runtime={_RAW_RUNTIME} "
           f"--group_reporting --output-format=json {jobs}")
    totals, cpus, core_rounds = [], [], []
    for i in range(rounds(ctx)):
        if ctx.stop_requested():
            ctx.abort("fail", "用户中止")
        pre = device_snapshot(ctx, nic)
        r = ctx.sh(cmd, timeout=_RAW_RUNTIME + 300, sudo=True,
                   note=f"全盘并发{cn}({len(disks)} job)第 {i + 1} 轮")
        delta = device_delta(pre, device_snapshot(ctx, nic))
        if delta.get("cpu_pct") is not None:
            cpus.append(delta["cpu_pct"])
        if delta.get("cpu_per_core"):
            core_rounds.append(delta["cpu_per_core"])
        if r.rc != 0:
            ctx.abort("fail", f"全盘并发 fio 失败 rc={r.rc}(详见 stdout.log)")
        try:
            totals.append(parse_fio_json_multi(r.out)[f"{side}_mbps"])
        except (ValueError, KeyError):
            ctx.abort("fail", "全盘并发 fio 解析失败(输出见 stdout.log)")
    all_med = record_rounds(ctx, f"{prefix}_all_mb_s", totals)
    if cpus:
        record_rounds(ctx, f"{prefix}_all_dev_cpu_pct", cpus)
    if core_rounds:   # 逐核中位数(同 fio_profile 口径)
        ncore = min(len(c) for c in core_rounds)
        per_core = []
        for i in range(ncore):
            vals = [c[i] for c in core_rounds if c[i] is not None]
            per_core.append(round(float(statistics.median(vals)), 1) if vals else None)
        ctx.metric(f"{prefix}_all_dev_cpu_per_core", per_core)

    per_str = ", ".join(f"{name} {v}" for name, v in per_disk.items())
    slow = (min(per_disk.values()) < 0.85 * max(per_disk.values())
            if len(per_disk) > 1 else False)
    warn = ";盘间差距 >15%,疑似慢盘/异常盘,建议复测并关注 SMART" if slow else ""
    return (f"裸盘{cn}({len(disks)} 盘): {per_str} MB/s;"
            f"{len(disks)} 盘并发{cn} {all_med}MB/s{warn}")


@_impl("s3_raw_read_all")
def s3_raw_read_all(ctx: Ctx) -> str:
    return _raw_disk_test(ctx, "read")


@_impl("s3_raw_write_all")
def s3_raw_write_all(ctx: Ctx) -> str:
    return _raw_disk_test(ctx, "write")


@_impl("s3_rebuild_monitor")
def s3_rebuild_monitor(ctx: Ctx) -> str:
    """人工拔盘后点击运行:确认降级 → 监控重建速度曲线 → 叠加 fio 测 IO 衰减。"""
    def mdstat():
        return ctx.sh("cat /proc/mdstat").out

    st = mdstat()
    degraded = bool(re.search(r"\[_[U_]*\]|\[[U_]*_\]|degraded", st))
    recovering = "recovery" in st or "resync" in st or "reshape" in st
    if not degraded and not recovering:
        ctx.abort("blocked", "阵列当前正常(未降级)。请先按操作指导拔出一块成员盘"
                             "(或 sudo mdadm --fail /dev/mdX --remove /dev/sdX)后重新运行。")
    ctx.log("阵列已降级/重建中,开始全程监控(每 30s 记录一次速度)")
    # 叠加业务 IO:4K 随机读,记录重建期间 IOPS
    tdir = ctx.cfg.get("device_test_dir", "/vol2/.nastest_perf")
    ctx.sh(f"mkdir -p {tdir}")
    ctx.sh(f"fio --name=rebuildload --directory={tdir} --size=1G --rw=randread --bs=4k "
           f"--ioengine={_pick_fio_engine(ctx)} --iodepth=16 --numjobs=2 --direct=1 "
           f"--time_based --runtime=14400 "
           f"> /dev/null 2>&1 &", note="叠加业务 IO 负载(后台)")
    curve = []
    t0 = time.time()
    last_prog = -1.0
    while True:
        if ctx.stop_requested():
            break
        st = mdstat()
        prog = re.search(r"=\s*([\d.]+)%", st)
        speed = re.search(r"speed=([\d.]+)K", st)
        p = float(prog.group(1)) if prog else None
        curve.append({"t_min": round((time.time() - t0) / 60, 1),
                      "progress_pct": p,
                      "speed_kbps": float(speed.group(1)) if speed else None})
        if p is not None and p != last_prog:
            ctx.log(f"重建 {p}% 速度 {speed.group(1) if speed else '?'}KB/s")
            last_prog = p
        if not ("recovery" in st or "resync" in st or "reshape" in st):
            if re.search(r"\[UU+\]|\[[U]+U\]", st.replace(" ", "")) or not re.search(
                    r"\[_[U_]*\]|\[[U_]*_\]", st):
                ctx.log("重建完成")
                break
        time.sleep(30)
    ctx.sh("pkill -f 'fio --name=rebuildload' 2>/dev/null || true")
    ctx.sh(f"rm -f {tdir}/rebuildload.*")
    dur = round((time.time() - t0) / 60, 1)
    speeds = [c["speed_kbps"] for c in curve if c["speed_kbps"]]
    avg_speed = round(sum(speeds) / len(speeds) / 1024, 1) if speeds else None
    ctx.metric("rebuild_minutes", dur)
    ctx.metric("rebuild_avg_speed_mb_s", avg_speed)
    ctx.metric("rebuild_curve", curve[::4])
    det = detect_md_raid(ctx)
    if det:
        ctx.metric("rebuild_raid_level", det)
    base_iops, ref_id = None, None
    if det:  # 优先取当前阵列级别对应组的随读基线
        ref = db.get_result(ctx.run_id, f"S3-{det}-03")
        if ref and ref["status"] == "pass":
            base_iops = ref["metrics"].get("rand_read_r_iops")
            ref_id = f"S3-{det}-03"
    if base_iops is None:  # 回退:任一组已测的随读 IOPS
        from .raid import RAID_SUBS
        for lv in RAID_SUBS:
            ref = db.get_result(ctx.run_id, f"S3-{lv}-03")
            if ref and ref["status"] == "pass" and ref["metrics"].get("rand_read_r_iops"):
                base_iops = ref["metrics"]["rand_read_r_iops"]
                ref_id = f"S3-{lv}-03"
                break
    if base_iops:
        ctx.metric("baseline_randread_iops", base_iops)
        ctx.log(f"重建前基线({ref_id}) 随机读 {base_iops} IOPS,"
                f"请对照重建期间业务 IO 表现评估衰减")
    return (f"重建监控 {dur} 分钟,平均速度 {avg_speed}MB/s,曲线见 metrics.rebuild_curve;"
            f"请人工确认重建期间业务 IO 衰减程度(§8:<30% 优)并在备注记录")


# ================= S4 网络层 =================
def _iperf_rounds(ctx: Ctx, port: int, t: int, reverse: bool, key: str) -> dict:
    per = []
    n = rounds(ctx)
    for i in range(n):
        d = iperf_local(ctx, port, t, reverse=reverse)
        ctx.log(f"第 {i + 1}/{n} 轮: {d.get('mbps')}Mbps")
        per.append(d)
    mbps = [d["mbps"] for d in per]
    med_mbps = record_rounds(ctx, f"{key}_mbps", mbps)
    ctx.metric(f"{key}_mb_s", round(med_mbps / 8, 1))
    ctx.metric(f"{key}_retransmits", max(d.get("retransmits", 0) for d in per))
    return {"mbps": med_mbps}


@_impl("s4_tcp_tx")
def s4_tcp_tx(ctx: Ctx) -> str:
    ports = iperf_servers(ctx, [ctx.cfg.get("iperf_ports", [5201])[0]])
    try:
        r = _iperf_rounds(ctx, ports[0], 60, reverse=False, key="tcp_tx")
        link = int(ctx.cfg.get("nic_link_mbps", 1000))
        util = round(r["mbps"] / link * 100, 1)
        ctx.metric("link_util_pct", util)
        return (f"TCP 上行(客户端→NAS): {r['mbps']}Mbps = {r['mbps'] / 8:.1f}MB/s,"
                f"链路利用率 {util}%(1GbE 线速 ≈118MB/s)")
    finally:
        iperf_cleanup(ctx)


@_impl("s4_tcp_rx")
def s4_tcp_rx(ctx: Ctx) -> str:
    ports = iperf_servers(ctx, [ctx.cfg.get("iperf_ports", [5201])[0]])
    try:
        r = _iperf_rounds(ctx, ports[0], 60, reverse=True, key="tcp_rx")
        return f"TCP 下行(NAS→客户端): {r['mbps']}Mbps = {r['mbps'] / 8:.1f}MB/s(对比上行判断方向性瓶颈)"
    finally:
        iperf_cleanup(ctx)


@_impl("s4_tcp_multi")
def s4_tcp_multi(ctx: Ctx) -> str:
    ports = iperf_servers(ctx)
    try:
        n = rounds(ctx)
        vals = []
        for i in range(n):
            d = iperf_multi_local(ctx, ports, 30)
            ctx.log(f"第 {i + 1}/{n} 轮聚合: {d.get('mbps')}Mbps")
            vals.append(d["mbps"])
        med = record_rounds(ctx, "tcp_multi_mbps", vals)
        single_ref = None
        ref = None
        from . import db
        ref = db.get_result(ctx.run_id, "S4-01")
        if ref and ref["status"] == "pass":
            single_ref = ref["metrics"].get("tcp_tx_mbps")
        if single_ref:
            ctx.metric("multi_vs_single_ratio", round(med / single_ref, 2))
        return (f"TCP {len(ports)} 实例聚合: {med}Mbps"
                + (f"(单流 {single_ref}Mbps,叠加比 {round(med / single_ref, 2)})" if single_ref else "")
                + "——弱 ARM 单实例先饱和时,多实例可排除误判(§5.4)")
    finally:
        iperf_cleanup(ctx)


@_impl("s4_udp_pps")
def s4_udp_pps(ctx: Ctx) -> str:
    ports = iperf_servers(ctx, [ctx.cfg.get("iperf_ports", [5201])[0]])
    try:
        per = []
        for i in range(rounds(ctx)):
            d = iperf_local(ctx, ports[0], 30, udp=True)
            ctx.log(f"第 {i + 1} 轮: {d.get('pps')}pps 丢包 {d.get('lost_percent')}%")
            per.append(d)
        pps = record_rounds(ctx, "udp_pps", [d["pps"] for d in per])
        ctx.metric("udp_jitter_ms", round(sum(d.get("jitter_ms", 0) for d in per) / len(per), 3))
        ctx.metric("udp_lost_pct", round(sum(d.get("lost_percent", 0) for d in per) / len(per), 2))
        return (f"UDP 64B 小包: {pps:.0f} PPS,丢包 {ctx.metrics['udp_lost_pct']}%,"
                f"jitter {ctx.metrics['udp_jitter_ms']}ms(ARM 网络栈与中断处理考验)")
    finally:
        iperf_cleanup(ctx)


@_impl("s4_mtu9000")
def s4_mtu9000(ctx: Ctx) -> str:
    require_local_sudo(ctx, "修改本机 MTU ")
    require_local_tool(ctx, "iperf3")
    ip = ctx.cfg["device_ip"]
    # 本机出口网卡
    r = ctx.sh_local(["ip", "route", "get", ip], note="本机出口网卡")
    m = re.search(r"dev (\S+)", r.out)
    if not m:
        ctx.abort("fail", "无法识别本机到设备的出口网卡")
    lnic = m.group(1)
    from .ctx import device_nic
    dnic = device_nic(ctx)
    old_l = ctx.sh_local(["cat", f"/sys/class/net/{lnic}/mtu"]).out.strip()
    old_d = ctx.sh(f"cat /sys/class/net/{dnic}/mtu").out.strip()
    ctx.log(f"MTU 现状: 本机 {lnic}={old_l}, 设备 {dnic}={old_d}")
    try:
        # 基线(当前 MTU)
        ports = iperf_servers(ctx, [ctx.cfg.get("iperf_ports", [5201])[0]])
        base = iperf_local(ctx, ports[0], 30)["mbps"]
        ctx.metric("mtu1500_mbps", base)
        iperf_cleanup(ctx)
        # 两端改 9000
        ctx.sh_local(["ip", "link", "set", "dev", lnic, "mtu", "9000"], sudo=True,
                     note=f"本机 {lnic} MTU=9000")
        ctx.sh(f"ip link set {dnic} mtu 9000", sudo=True, note=f"设备 {dnic} MTU=9000")
        ping = ctx.sh_local(["ping", "-c", "3", "-M", "do", "-s", "8972", ip],
                            timeout=30, note="巨帧 ping 验证(8972+28=9000)")
        jumbo_ok = ping.rc == 0
        ctx.metric("jumbo_ping_ok", jumbo_ok)
        if not jumbo_ok:
            ctx.log("[!] 巨帧 ping 失败——交换机可能不支持 jumbo frame,结果将降速")
        ports = iperf_servers(ctx, [ctx.cfg.get("iperf_ports", [5201])[0]])
        big = iperf_local(ctx, ports[0], 30)["mbps"]
        ctx.metric("mtu9000_mbps", big)
        iperf_cleanup(ctx)
        gain = round((big - base) / base * 100, 1) if base else None
        ctx.metric("mtu_gain_pct", gain)
        return (f"MTU9000: {base}→{big}Mbps(收益 {gain}%),巨帧ping {'通' if jumbo_ok else '不通(交换机限制?)'}")
    finally:
        ctx.sh_local(["ip", "link", "set", "dev", lnic, "mtu", old_l or "1500"],
                     sudo=True, note=f"恢复本机 MTU={old_l}")
        ctx.sh(f"ip link set {dnic} mtu {old_d or '1500'}", sudo=True,
               note=f"恢复设备 MTU={old_d}")
        iperf_cleanup(ctx)
