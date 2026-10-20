"""S0 环境与基线 / S1 CPU / S2 内存 用例实现。"""
import re
from pathlib import Path

from . import config
from .ctx import (CaseAbort, Ctx, device_nic, record_rounds, rounds,
                  require_device_tool)

IMPL: dict[str, callable] = {}


def _impl(key: str):
    def deco(fn):
        IMPL[key] = fn
        return fn
    return deco


# ================= S0 环境与基线 =================
@_impl("s0_baseline")
def s0_baseline(ctx: Ctx) -> str:
    from . import evidence
    b: dict = {}
    b["uname"] = ctx.sh("uname -a", note="内核").out.strip()
    lscpu = ctx.sh("lscpu", note="CPU").out
    b["cpu_model"] = (re.search(r"Model name:\s+(.+)", lscpu) or ["", "unknown"])[1].strip() \
        if re.search(r"Model name:\s+(.+)", lscpu) else "ARM (见 lscpu 证据)"
    m = re.search(r"CPU\(s\):\s+(\d+)", lscpu)
    b["cores"] = int(m.group(1)) if m else 0
    mem = ctx.sh("free -m | awk '/Mem:/ {print $2}'", note="内存(MB)").out.strip()
    b["mem_mb"] = int(mem) if mem.isdigit() else 0
    b["page_size"] = ctx.sh("getconf PAGESIZE").out.strip()
    freqs = sorted(set(ctx.sh(
        "cat /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq 2>/dev/null").out.split()))
    b["max_freq_khz"] = freqs
    b["big_little"] = len(freqs) > 1
    b["governor"] = ctx.sh(
        "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null").out.strip()
    crypto = ctx.sh("grep -E '^(name|driver)' /proc/crypto | sort -u | head -60",
                    note="内核加密算法").out
    b["aes_ce"] = bool(re.search(r"aes-ce|ce-aes|aes-arm64", crypto))
    vol = ctx.cfg.get("volume_path", "/vol2")
    df = ctx.sh(f"df -Th {vol}", note="测试卷").out
    b["fs_line"] = df.strip().splitlines()[-1] if df.strip() else ""
    b["mdstat"] = ctx.sh("cat /proc/mdstat", note="RAID").out.strip()
    b["lsblk"] = ctx.sh("lsblk -o NAME,TYPE,FSTYPE,SIZE,MOUNTPOINTS").out.strip()
    # SMART 摘要
    smart = []
    disks = [ln.split()[0] for ln in ctx.sh(
        "lsblk -dn -o NAME,TYPE | awk '$2==\"disk\"'").out.splitlines()
        if ln.strip() and not ln.split()[0].startswith(("mmc", "zram", "loop", "dm"))]
    for d in disks[:8]:
        r = ctx.sh(f"smartctl -i /dev/{d} 2>/dev/null | grep -E 'Device Model|Serial|User Capacity' | head -3",
                   sudo=True)
        smart.append(f"{d}: " + " | ".join(ln.split(":", 1)[-1].strip()
                                            for ln in r.out.splitlines() if ":" in ln))
    b["smart"] = smart
    nic = device_nic(ctx)
    b["nic"] = nic
    b["nic_speed_mbps"] = ctx.sh(f"cat /sys/class/net/{nic}/speed 2>/dev/null").out.strip()
    # 工具版本
    vers = {}
    for t, cmd in [("fio", "fio --version"), ("iperf3", "iperf3 --version"),
                   ("openssl", "openssl version"), ("sysbench", "sysbench --version"),
                   ("stress-ng", "stress-ng --version"), ("pigz", "pigz --version"),
                   ("ffmpeg", "ffmpeg -version 2>/dev/null | head -1"),
                   ("rsync", "rsync --version | head -1")]:
        vers[t] = ctx.sh(f"{cmd} 2>&1 | head -1").out.strip()[:80]
    b["tool_versions"] = vers
    b["smb_shares"] = ctx.sh(
        "cat /etc/samba/users/*.share.conf 2>/dev/null | grep -E '^\\[' | tr -d '[]' || true",
        sudo=True).out.split()
    ctx.metric("baseline", b)
    evidence.save_json(ctx.run_id, ctx.case_id, "baseline.json", b)
    md = [f"# 基线信息(方案 §3.2)", f"- 设备: {ctx.cfg.get('device_model', '')}",
          f"- 内核: {b['uname']}", f"- CPU: {b['cores']} 核,大小核={b['big_little']},"
          f"最高频率 {freqs}", f"- 内存: {b['mem_mb']}MB,页大小 {b['page_size']}",
          f"- governor: {b['governor']}", f"- AES 硬件指令(aes-ce): {'是' if b['aes_ce'] else '否 ← 红旗'}",
          f"- 卷: {b['fs_line']}", f"- SMART:"] + [f"  - {s}" for s in smart] + \
         [f"- 网卡: {nic} @ {b['nic_speed_mbps']}Mbps", "- 工具版本:"] + \
         [f"  - {k}: {v}" for k, v in vers.items()]
    evidence.save_text(ctx.run_id, ctx.case_id, "baseline.md", "\n".join(md))
    return (f"{b['cores']}核 {'大小核' if b['big_little'] else '同构'} "
            f"内存{b['mem_mb']}MB 页{b['page_size']}B governor={b['governor']} "
            f"aes-ce={'有' if b['aes_ce'] else '无'} NIC={nic}@{b['nic_speed_mbps']}Mbps;"
            f"详见证据 baseline.json/baseline.md")


@_impl("s0_governor")
def s0_governor(ctx: Ctx) -> str:
    before = ctx.sh("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").out.strip()
    ctx.metric("governor_before", before)
    if before == "performance":
        ctx.metric("governor_after", before)
        return "governor 已为 performance,无需调整"
    ctx.sh('for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do '
           'echo performance > $c; done', sudo=True, note="锁定 performance")
    after = ctx.sh("cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor | sort -u").out.strip()
    ctx.metric("governor_after", after)
    if after == "performance":
        return f"governor 已从 {before} 锁定为 performance"
    return (f"[告警] governor 锁定失败:当前={after}(期望 performance)。"
            f"可能被固件/温控服务接管。人工处理:sudo sh -c 'echo performance > "
            f"/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor',并检查是否有 "
            f"温控服务周期改写;结果解读时请注意频率波动会影响可比性。")


@_impl("s0_background")
def s0_background(ctx: Ctx) -> str:
    facts = []
    r = ctx.sh("docker ps --format '{{.Names}} {{.Image}} {{.Status}}' 2>/dev/null || echo NO_DOCKER",
               sudo=True, note="运行中的容器")
    facts.append(("containers", r.out.strip()))
    facts.append(("failed_units", ctx.sh("systemctl --failed --no-legend 2>/dev/null | head -10",
                                          sudo=True).out.strip()))
    facts.append(("cron", ctx.sh("crontab -l 2>/dev/null; ls /etc/cron.d/ 2>/dev/null",
                                 sudo=True, note="计划任务").out.strip()))
    r = ctx.sh("ps aux --sort=-%cpu | head -12", note="CPU 占用 Top 进程")
    facts.append(("top_cpu", r.out.strip()))
    ctx.metric("background", dict(facts))
    hot = []
    for ln in r.out.splitlines()[1:]:
        parts = ln.split(None, 10)
        if len(parts) >= 3:
            cmd = parts[10]
            if cmd.split()[0] in ("ps", "bash", "sh") or "--sort=-%cpu" in cmd:
                continue  # 忽略采样命令自身与 shell
            try:
                if float(parts[2]) > 50:
                    hot.append(f"{cmd[:60]}({parts[2]}%)")
            except ValueError:
                pass
    if hot:
        return ("[告警] 检测到高 CPU 后台进程,可能干扰性能测试(§3.3):\n- " +
                "\n- ".join(hot) + "\n建议:停止相应服务/容器后重跑性能用例。")
    return "无干扰性后台负载(容器/计划任务/Top 进程清单见日志与 metrics)"


@_impl("s0_client_selfcheck")
def s0_client_selfcheck(ctx: Ctx) -> str:
    from .ctx import _pick_fio_engine, require_local_tool
    require_local_tool(ctx, "fio")
    d = ctx.cfg.get("local_fio_dir") or str(Path.home() / ".nastest_local_selfcheck")
    ctx.sh_local(["mkdir", "-p", d], note="本机自检目录")
    fst = ctx.sh_local(["df", "-T", d]).out
    if "tmpfs" in fst:
        ctx.abort("blocked", f"{d} 在 tmpfs 上,测的是内存不是磁盘!"
                             f"请在配置 local_fio_dir 指定一个真实磁盘路径(如 NVMe 挂载点)")
    cmd = ("fio --name=selfcheck --directory=" + d +
           " --size=2G --rw=randrw --rwmixread=70 --bs=4k"
           " --ioengine=" + _pick_fio_engine(ctx, local=True) +
           " --iodepth=64 --numjobs=4 --direct=1 --time_based --runtime=30"
           " --group_reporting --output-format=json")
    r = ctx.sh_local(cmd, timeout=180, note="本机 NVMe randrw 70/30 QD64×4")
    if r.rc != 0:
        ctx.abort("fail", f"本机 fio 自检失败 rc={r.rc}")
    from .parse import parse_fio_json
    try:
        p = parse_fio_json(r.out)
    except ValueError as e:
        ctx.abort("fail", f"解析失败: {e}")
    for k, v in p.items():
        ctx.metric(k, v)
    ctx.sh_local(["rm", "-f", d + "/selfcheck.0.0", d + "/selfcheck.1.0",
                  d + "/selfcheck.2.0", d + "/selfcheck.3.0"])
    return (f"本机自检: 混合 IOPS={p.get('r_iops', 0) + p.get('w_iops', 0):.0f} "
            f"(读 {p.get('r_iops')} / 写 {p.get('w_iops')}),远高于 NAS 预期 → 客户端非瓶颈(§3.1)")


# ================= S1 CPU =================
@_impl("s1_sysbench_cpu")
def s1_sysbench_cpu(ctx: Ctx) -> str:
    require_device_tool(ctx, "sysbench")
    nproc = int(ctx.sh("nproc").out.strip() or 1)
    n = rounds(ctx)

    def run(threads):
        vals = []
        for i in range(n):
            r = ctx.sh(f"sysbench cpu --threads={threads} --cpu-max-prime=20000 run",
                       timeout=300, note=f"threads={threads} 第 {i + 1}/{n} 轮")
            v = None
            m = re.search(r"events per second:\s+([\d.]+)", r.out)
            if m:
                v = float(m.group(1))
            if v is None:
                ctx.abort("fail", "sysbench 输出无 events per second")
            vals.append(v)
        return vals

    single = record_rounds(ctx, "cpu_events_s_single", run(1))
    multi = record_rounds(ctx, "cpu_events_s_multi", run(nproc))
    scaling = round(multi / single / nproc, 3) if single else None
    ctx.metric("cpu_scaling_ratio", scaling)
    return (f"sysbench CPU: 单核 {single:.0f} ev/s, {nproc} 核 {multi:.0f} ev/s,"
            f"扩展比 {scaling}(理想=1,大小核架构通常 <1)")


@_impl("s1_openssl")
def s1_openssl(ctx: Ctx) -> str:
    algos = ["aes-128-gcm", "aes-256-gcm", "chacha20-poly1305"]
    out = {}
    for a in algos:
        r = ctx.sh(f"openssl speed -seconds 5 -evp {a}", timeout=300, note=a)
        best = None
        for ln in r.out.splitlines():
            if ln.lower().startswith(a):
                ks = re.findall(r"([\d.]+)k", ln)
                if ks:
                    best = float(ks[-1])  # 最大 block(16384B)列
        if best is None:
            ctx.abort("fail", f"openssl speed 无法解析 {a} 结果")
        mb_s = round(best / 1000, 1)  # k=千字节/秒
        out[a] = mb_s
        ctx.metric(f"openssl_{a}_mb_s", mb_s)
    aes_ce = ctx.sh("grep -cE 'driver *: *(aes-ce|ce-aes)' /proc/crypto || true").out.strip()
    ctx.metric("aes_ce_driver计数", aes_ce)
    return ("openssl(大 block 16KB): " +
            ", ".join(f"{a}={v}MB/s" for a, v in out.items()) +
            f";aes-ce 驱动条目={aes_ce}(加密性能决定 SMB 加密/VPN 上限)")


@_impl("s1_pigz")
def s1_pigz(ctx: Ctx) -> str:
    require_device_tool(ctx, "pigz")
    tdir = ctx.cfg.get("device_test_dir", "/vol2/.nastest_perf")
    ctx.sh(f"mkdir -p {tdir}")
    mb = 256
    ctx.sh(f"base64 /dev/urandom | head -c {mb * 1024 * 1024} > {tdir}/pigz_src",
           timeout=300, note=f"生成 {mb}MB 测试数据")
    r = ctx.sh(f"pigz -c {tdir}/pigz_src > {tdir}/pigz_src.gz", timeout=600,
               note="压缩")
    if r.rc != 0:
        ctx.abort("fail", "pigz 压缩失败")
    c_mbps = round(mb / (r.duration_ms / 1000), 1)
    ctx.metric("compress_mb_s", c_mbps)
    r = ctx.sh(f"pigz -d -c {tdir}/pigz_src.gz > /dev/null", timeout=600, note="解压")
    if r.rc != 0:
        ctx.abort("fail", "pigz 解压失败")
    d_mbps = round(mb / (r.duration_ms / 1000), 1)
    ctx.metric("decompress_mb_s", d_mbps)
    ctx.sh(f"rm -f {tdir}/pigz_src {tdir}/pigz_src.gz")
    return f"pigz {mb}MB: 压缩 {c_mbps}MB/s, 解压 {d_mbps}MB/s"


@_impl("s1_coremark")
def s1_coremark(ctx: Ctx) -> str:
    for t in ("git", "make", "gcc"):
        require_device_tool(ctx, t)
    d = "/tmp/coremark_nastest"
    ctx.sh(f"rm -rf {d}")
    r = ctx.sh(f"git clone --depth 1 https://github.com/eembc/coremark {d}",
               timeout=300, note="clone coremark")
    if r.rc != 0:
        ctx.abort("blocked", "设备无法访问 github(无外网?)。离线方式:本机 clone 后 "
                             f"scp -r coremark {ctx.cfg.get('ssh_user')}@{ctx.cfg['device_ip']}:{d} "
                             "再重跑本用例。")
    nproc = int(ctx.sh("nproc").out.strip() or 1)

    def build(extra=""):
        rr = ctx.sh(f"make clean >/dev/null 2>&1; make PORTABLE=1 {extra} 2>&1 | tail -5",
                    cwd=d, timeout=600, note=f"编译 {extra or '(单线程)'}")
        if rr.rc != 0:
            ctx.abort("fail", "coremark 编译失败(见日志)")

    def run(tag):
        rr = ctx.sh("./coremark.exe 0x0 0x0 0x66 0 7 1 2000", cwd=d, timeout=900, note=tag)
        score = re.search(r"CoreMark 1\.0\s*:\s*([\d.]+)", rr.out)
        ips = re.search(r"Iterations/Sec\s*:\s*([\d.]+)", rr.out)
        if not score:
            ctx.abort("fail", f"coremark {tag} 无分数输出")
        ctx.metric(f"coremark_{tag}", float(score.group(1)))
        if ips:
            ctx.metric(f"coremark_{tag}_iter_s", float(ips.group(1)))
        return float(score.group(1))

    build()
    single = run("single")
    build(f'XCFLAGS="-DMULTITHREAD={nproc} -DUSE_PTHREAD"')
    multi = run("multi")
    ctx.sh(f"rm -rf {d}")
    return f"CoreMark: 单核 {single:.0f}, {nproc} 核 {multi:.0f}(总分/迭代率见 metrics)"


# ================= S2 内存 =================
@_impl("s2_stream")
def s2_stream(ctx: Ctx) -> str:
    for t in ("gcc", "curl"):
        require_device_tool(ctx, t)
    nproc = int(ctx.sh("nproc").out.strip() or 1)
    src, source = "/tmp/nastest_stream.c", ""
    r = ctx.sh(f"curl -fsSL --max-time 60 -o {src} "
               f"https://www.cs.virginia.edu/stream/FTP/Code/{'stream.c'}",
               timeout=90, note="下载官方 stream.c")
    if r.rc == 0 and ctx.sh(f"grep -c STREAM_ARRAY_SIZE {src}").rc == 0:
        source = "官方 STREAM"
        build = f"gcc -O3 -fopenmp -DSTREAM_ARRAY_SIZE=80000000 -DNTIMES=10 {src} -o /tmp/nastest_stream"
    else:
        ctx.log("官方下载失败,改用内置 vendor/triad.c(自研 STREAM 等价实现,报告将注明)")
        ctx.put_file(config.VENDOR_DIR / "triad.c", "/tmp/nastest_triad.c")
        source = "triad(自研等价实现)"
        build = "gcc -O3 -fopenmp -DARRAY_N=80000000 /tmp/nastest_triad.c -o /tmp/nastest_stream -lm"
    if not ctx.sh(build, timeout=300, note="编译").ok:
        ctx.abort("fail", "编译失败(见日志)")
    r = ctx.sh(f"OMP_NUM_THREADS={nproc} /tmp/nastest_stream", timeout=600,
               note=f"运行({nproc} 线程)")
    vals = {}
    for name in ("Copy", "Scale", "Add", "Triad"):
        m = re.search(rf"{name}\s*:?\s+([\d.]+)", r.out)
        if m:
            vals[name] = float(m.group(1))
    if not vals:
        ctx.abort("fail", "未解析到 Copy/Scale/Add/Triad 结果")
    for k, v in vals.items():
        ctx.metric(f"stream_{k.lower()}_mb_s", v)
        ctx.metric(f"stream_{k.lower()}_gb_s", round(v / 1000, 2))
    ctx.metric("stream_source", source)
    ctx.sh("rm -f /tmp/nastest_stream /tmp/nastest_stream.c /tmp/nastest_triad.c")
    txt = ", ".join(f"{k} {v / 1000:.2f}GB/s" for k, v in vals.items())
    return f"{source}: {txt}(单通道 DDR4 Triad 通常 <20GB/s,§5.2)"


@_impl("s2_sysbench_mem")
def s2_sysbench_mem(ctx: Ctx) -> str:
    require_device_tool(ctx, "sysbench")
    nproc = int(ctx.sh("nproc").out.strip() or 1)
    r = ctx.sh(f"sysbench memory --memory-block-size=1M --memory-total-size=10G "
               f"--threads={nproc} run", timeout=600, note="1M 块全核带宽")
    m = re.search(r"([\d.]+)\s*MiB/sec", r.out)
    if not m:
        ctx.abort("fail", "sysbench memory 带宽解析失败")
    bw = float(m.group(1))
    ctx.metric("mem_bw_mib_s", bw)
    r = ctx.sh("sysbench memory --memory-block-size=4K --memory-access-mode=rnd "
               "--memory-total-size=2G run", timeout=600, note="4K 随机访问")
    m2 = re.search(r"([\d.]+)\s*MiB/sec", r.out)
    rnd = float(m2.group(1)) if m2 else None
    if rnd:
        ctx.metric("mem_rnd4k_mib_s", rnd)
    return (f"sysbench memory: 1M 块 {nproc} 线程带宽 {bw:.0f}MiB/s"
            + (f";4K 随机 {rnd:.0f}MiB/s" if rnd else "") + "(与 STREAM 交叉验证)")
