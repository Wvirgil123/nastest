"""S7 应用场景 / S8 稳定性与持续性能 / S9 功耗与能效 用例实现。"""
import csv
import re
import subprocess
import time

from . import db, evidence, raid, sampler
from .ctx import (Ctx, _pick_fio_engine, device_nic, device_snapshot,
                  device_delta, fio_cmd, iperf_cleanup, iperf_servers,
                  make_soak_sample_fn, record_rounds, require_device_tool)

IMPL: dict[str, callable] = {}


def _impl(key: str):
    def deco(fn):
        IMPL[key] = fn
        return fn
    return deco


# ================= S7 应用场景 =================
@_impl("s7_ffmpeg")
def s7_ffmpeg(ctx: Ctx) -> str:
    require_device_tool(ctx, "ffmpeg")
    r = ctx.sh("ffmpeg -hide_banner -encoders 2>/dev/null | grep -iE 'libx264|rkmpp' | head -10",
               note="编码器探测")
    enc = None
    if "libx264" in r.out:
        enc = "libx264"
    elif "rkmpp" in r.out:
        enc = "h264_rkmpp"
    ctx.metric("rkmpp_available", "rkmpp" in r.out)
    if not enc:
        ctx.abort("blocked", "ffmpeg 无 libx264/rkmpp 编码器——请安装带硬编支持的 ffmpeg 后重测")
    preset = "-preset veryfast" if enc == "libx264" else ""
    ctx.sh(f"ffmpeg -hide_banner -y -f lavfi -i testsrc2=size=1920x1080:rate=30:duration=30 "
           f"-c:v {enc} {preset} /tmp/nastest_src.mp4 2>&1 | tail -3",
           timeout=600, note="生成 1080p30 样片(30s)")
    if not ctx.sh("test -s /tmp/nastest_src.mp4").ok:
        ctx.abort("fail", "样片生成失败(见日志)")
    from .ctx import device_snapshot
    nic = device_nic(ctx)
    pre = device_snapshot(ctx, nic)
    r = ctx.sh(f"ffmpeg -hide_banner -i /tmp/nastest_src.mp4 -c:v {enc} {preset} "
               f"-f null - 2>&1 | tr '\\r' '\\n' | grep -E 'fps=' | tail -1",
               timeout=900, note=f"转码测 fps({enc})")
    post = device_snapshot(ctx, nic)
    fps_vals = re.findall(r"fps=\s*([\d.]+)", r.out)
    if not fps_vals:
        ctx.abort("fail", "转码输出无 fps 数据(见日志)")
    fps = float(fps_vals[-1])
    ctx.metric("transcode_fps", fps)
    ctx.metric("encoder", enc)
    delta = device_delta(pre, post)
    if delta.get("cpu_pct") is not None:
        ctx.metric("transcode_cpu_pct", delta["cpu_pct"])
    ctx.sh("rm -f /tmp/nastest_src.mp4")
    rt = "实时" if fps >= 25 else "非实时"
    return (f"1080p 转码({enc}): {fps}fps({rt},≥25fps 为实时),CPU {delta.get('cpu_pct')}%"
            f"(多数 ARM SoC 仅硬解无硬编,硬编需 rkmpp)")


@_impl("s7_docker")
def s7_docker(ctx: Ctx) -> str:
    r = ctx.sh("docker info --format '{{.ServerVersion}}' 2>&1", sudo=True, note="docker daemon")
    if r.rc != 0 or "cannot connect" in r.out.lower() or "permission denied" in r.out.lower():
        ctx.sh("systemctl status docker --no-pager 2>&1 | head -12", sudo=True, note="服务状态")
        ctx.sh("journalctl -u docker --no-pager -n 15 2>&1", sudo=True, note="最近日志")
        ctx.abort("fail", "docker daemon 不可用。常见根因:① docker 数据目录所在存储卷未挂载"
                          "(如 /vol1 未激活导致 metadata.db 不可访问);② AppArmor 加载失败。"
                          "修复:在 fnOS 检查存储空间状态或迁移 docker 存储路径后 "
                          "sudo systemctl restart docker。")
    ctx.metric("docker_version", r.out.strip()[:40])
    ctx.sh("timeout 180 docker pull hello-world 2>&1 | tail -2", sudo=True, timeout=240,
           note="拉取 hello-world")
    t0 = time.time()
    rr = ctx.sh("docker run --rm hello-world 2>&1 | tail -4", sudo=True, timeout=180,
                note="容器启动计时")
    dur = round(time.time() - t0, 1)
    if rr.rc != 0:
        ctx.abort("fail", f"hello-world 运行失败(启动 {dur}s)")
    ctx.metric("container_start_s", dur)
    return f"docker 可用(v{ctx.metrics['docker_version']}),hello-world 启动 {dur}s"


# ================= S8 稳定性与持续性能 =================
@_impl("s8_soak")
def s8_soak(ctx: Ctx) -> str:
    require_device_tool(ctx, "stress-ng")
    require_device_tool(ctx, "fio")
    minutes = int(ctx.cfg.get("soak_minutes", 240))
    interval = max(10, int(ctx.cfg.get("soak_sample_interval", 60)))
    secs = minutes * 60
    tdir = ctx.cfg.get("device_test_dir", "/vol2/.nastest_perf")
    ctx.sh(f"mkdir -p {tdir}")
    nproc = int(ctx.sh("nproc").out.strip() or 4)
    ctx.log(f"综合满载浸泡 {minutes} 分钟(CPU+IO+网络),采样间隔 {interval}s")
    # 1) CPU 压力
    ctx.sh(f"nohup stress-ng --cpu {nproc} --timeout {secs}s --metrics-brief "
           f"> /tmp/nastest_soak_cpu.log 2>&1 & echo $!", note="stress-ng CPU 满载(后台)")
    # 2) IO 压力(持续混合读写)
    ctx.sh(f"nohup fio --name=soak --directory={tdir} --size=1G --rw=randrw --rwmixread=70 "
           f"--bs=4k --ioengine={_pick_fio_engine(ctx)} --iodepth=32 --numjobs=4 "
           f"--direct=1 --time_based --runtime={secs} "
           f"> /tmp/nastest_soak_fio.log 2>&1 & echo $!", note="fio 持续混合 IO(后台)")
    # 3) 网络压力(本机有 iperf3 时)
    import shutil as _sh
    net_on = False
    if _sh.which("iperf3"):
        try:
            ports = iperf_servers(ctx, [ctx.cfg.get("iperf_ports", [5201])[0]])
            ip = ctx.cfg["device_ip"]
            subprocess.Popen(["iperf3", "-c", ip, "-p", str(ports[0]), "-t", str(secs)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            net_on = True
            ctx.log("本机 iperf3 网络打流已启动(后台)")
        except Exception as e:
            ctx.log(f"网络压力启动失败(忽略): {e}")
    else:
        ctx.log("本机无 iperf3,跳过网络压力(可用 S4 预检自动安装)")
    ctx.metric("net_load", net_on)
    # 采样器
    csv_path = evidence.case_dir(ctx.run_id, ctx.case_id) / "soak.csv"
    sp = sampler.Sampler(interval, csv_path, make_soak_sample_fn(ctx))
    sampler.register(ctx.run_id, ctx.case_id, sp)
    sp.start()
    ctx.log(f"采样中,实时数据: soak.csv(界面可查最近样本)")
    try:
        end = time.time() + secs
        last_log = time.time()
        while time.time() < end:
            if ctx.stop_requested():
                ctx.abort("fail", "用户中止浸泡")
            time.sleep(min(10, max(1, end - time.time())))
            if time.time() - last_log >= 600:
                last = sp.samples[-1] if sp.samples else {}
                left = int((end - time.time()) / 60)
                ctx.log(f"[进度] 剩余 {left}min | 最近样本: {last.get('temp_c')}℃ "
                        f"freq {last.get('freq_mhz')}MHz cpu {last.get('cpu_pct')}% "
                        f"io {last.get('disk_iops')}iops")
                last_log = time.time()
    finally:
        samples = sp.stop()
        sampler.unregister(ctx.run_id, ctx.case_id)
        ctx.sh("pkill -f stress-ng 2>/dev/null; pkill -f 'fio --name=soak' 2>/dev/null; true",
               note="清理设备压力源")
        ctx.sh("pkill iperf3 2>/dev/null; true")
        if net_on:
            ctx.sh_local("pkill iperf3 2>/dev/null; true", note="清理本机 iperf3")
        ctx.sh(f"rm -f {tdir}/soak.*")
    temps = [s.get("temp_c") for s in samples if s.get("temp_c") is not None]
    ctx.metric("soak_minutes_actual", round(len(samples) * interval / 60, 1))
    ctx.metric("samples", len(samples))
    ctx.metric("max_temp_c", max(temps) if temps else None)
    return (f"浸泡完成: {len(samples)} 个样本(≈{len(samples) * interval / 60:.0f}min),"
            f"最高温度 {max(temps) if temps else '?'}℃。请运行 S8-02 分析衰减与降频。")


@_impl("s8_analyze")
def s8_analyze(ctx: Ctx) -> str:
    csv_path = evidence.case_dir(ctx.run_id, "S8-01") / "soak.csv"
    if not csv_path.exists():
        ctx.abort("blocked", "未找到 soak.csv——请先完成 S8-01 浸泡")
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                rows.append({k: float(v) for k, v in row.items()
                             if k not in ("ts",) and v not in (None, "", "None")})
            except ValueError:
                continue
    if len(rows) < 5:
        ctx.abort("fail", f"样本过少({len(rows)}),无法分析")

    def series(key):
        return [r[key] for r in rows if key in r]

    def head_tail(vals):
        k = max(1, len(vals) // 5)
        return sum(vals[:k]) / k, sum(vals[-k:]) / k

    bw = [r.get("disk_read_mbps", 0) + r.get("disk_write_mbps", 0) for r in rows]
    # 去掉浸泡启动 ramp-up 阶段 IO 尚未开始的连续 0 带宽样本,避免 head≈0 使波动率虚高;
    # 尾部掉 0 属真实异常,保留。
    s0 = 0
    while s0 < len(bw) - 1 and bw[s0] <= 0:
        s0 += 1
    bw = bw[s0:]
    bw_h, bw_t = head_tail(bw)
    variance = round(abs(bw_h - bw_t) / bw_h * 100, 1) if bw_h else 0.0
    ctx.metric("soak_samples", len(rows))
    ctx.metric("bw_head_mb_s", round(bw_h, 1))
    ctx.metric("bw_tail_mb_s", round(bw_t, 1))
    ctx.metric("soak_variance_pct", variance)
    freq = series("freq_mhz")
    throttled = False
    if freq:
        f_h, f_t = head_tail(freq)
        ctx.metric("freq_head_mhz", round(f_h))
        ctx.metric("freq_tail_mhz", round(f_t))
        if f_h and f_t < f_h * 0.9 and bw_t < bw_h * 0.85:
            throttled = True
    ctx.metric("throttled", throttled)
    temps = series("temp_c")
    ctx.metric("max_temp_c", max(temps) if temps else None)
    mem = series("mem_avail_mb")
    if mem:
        m_h, m_t = head_tail(mem)
        ctx.metric("mem_avail_head_mb", round(m_h))
        ctx.metric("mem_avail_tail_mb", round(m_t))
        ctx.metric("mem_leak_suspect", m_t < m_h * 0.6)
    verdict = []
    verdict.append(f"吞吐波动 {variance}%({'达标 <10%' if variance < 10 else '超标 ≥10%'})")
    verdict.append(f"热降频: {'检测到持续降频伴随性能下降(§5.8 不通过)' if throttled else '未检测到'}")
    if temps:
        verdict.append(f"最高温度 {max(temps)}℃")
    ctx.metric("verdict", verdict)
    ok = variance < 10 and not throttled
    if not ok:
        ctx.abort("fail", "浸泡分析未达 §5.8 标准: " + ";".join(verdict))
    return "浸泡分析(§5.8): " + ";".join(verdict)


@_impl("s8_full_capacity")
def s8_full_capacity(ctx: Ctx) -> str:
    require_device_tool(ctx, "fio")
    if not ctx.sh("command -v fallocate || true").out.strip():
        ctx.abort("blocked", "设备无 fallocate,无法快速填充")
    vol = ctx.cfg.get("volume_path", "/vol2")
    tdir = ctx.cfg.get("device_test_dir", "/vol2/.nastest_perf")
    fill_dir = f"{tdir}/fullcap"
    target_pct = 90

    def used_pct():
        r = ctx.sh(f"df --output=pcent {vol} | tail -1").out.strip().rstrip("%")
        return int(r) if r.isdigit() else -1

    start_pct = used_pct()
    ctx.log(f"当前卷使用率 {start_pct}%,目标 ≥{target_pct}%(fallocate 填充,秒级/GB)")
    ctx.sh(f"rm -rf {fill_dir}; mkdir -p {fill_dir}")
    try:
        i = 0
        while used_pct() < target_pct:
            if ctx.stop_requested():
                ctx.abort("fail", "用户中止填充")
            r = ctx.sh(f"fallocate -l 1G {fill_dir}/fill_{i:04d}.dat", timeout=120)
            if r.rc != 0:
                ctx.log("卷已满,停止填充")
                break
            i += 1
            if i % 20 == 0:
                ctx.log(f"已填充 {i}GB,当前 {used_pct()}%")
        full_pct = used_pct()
        ctx.metric("fill_gb", i)
        ctx.metric("used_pct_before", start_pct)
        ctx.metric("used_pct_full", full_pct)
        # 复测顺序写 + 4K 随机写(30s)
        from .parse import parse_fio_json
        res = {}
        for key, want in (("seq_write", "w_mbps"), ("rand_write", "w_iops")):
            r = ctx.sh(fio_cmd(ctx, key, tdir, 30, "2G"), timeout=600,
                       note=f"满容量复测 {key}(30s)")
            try:
                p = parse_fio_json(r.out)
            except ValueError:
                ctx.abort("fail", f"{key} 复测解析失败")
            res[key] = p.get(want)
            ctx.metric(f"full_{key}_{want}", p.get(want))
            ctx.sh(f"rm -f {tdir}/{key}.*")
        # 对比空卷基线(取有数据的 RAID 组:优先运行配置级别)
        base = {}
        lv = raid.pick_level(raid.results_by_id(ctx.run_id), ctx.cfg)
        ref2 = db.get_result(ctx.run_id, raid.level_case(lv, "02")) if lv else None
        ref4 = db.get_result(ctx.run_id, raid.level_case(lv, "04")) if lv else None
        if ref2 and ref2["status"] == "pass":
            base["seq_w"] = ref2["metrics"].get("seq_write_w_mbps")
        if ref4 and ref4["status"] == "pass":
            base["rand_w"] = ref4["metrics"].get("rand_write_w_iops")
        if base.get("seq_w"):
            deg = round((base["seq_w"] - res["seq_write"]) / base["seq_w"] * 100, 1)
            ctx.metric("seq_write_degrade_pct", deg)
        if base.get("rand_w"):
            deg = round((base["rand_w"] - res["rand_write"]) / base["rand_w"] * 100, 1)
            ctx.metric("rand_write_degrade_pct", deg)
        return (f"满容量({full_pct}%)复测: 顺序写 {res['seq_write']}MB/s"
                + (f"(空卷 {base['seq_w']},衰减 {ctx.metrics.get('seq_write_degrade_pct')}%)"
                   if base.get("seq_w") else "(无空卷基线,请先运行对应 RAID 组的顺序写用例)")
                + f";4K 随机写 {res['rand_write']}IOPS"
                + (f"(空卷 {base['rand_w']},衰减 {ctx.metrics.get('rand_write_degrade_pct')}%)"
                   if base.get("rand_w") else "") + "。§8:衰减<10% 优")
    finally:
        ctx.sh(f"rm -rf {fill_dir}", timeout=600, note="清理填充文件")


# ================= S9 功耗与能效 =================
@_impl("s9_power")
def s9_power(ctx: Ctx) -> str:
    # 保留此前通过界面录入的读数
    prev = db.get_result(ctx.run_id, ctx.case_id)
    if prev and prev.get("metrics"):
        ctx.metrics.update(prev["metrics"])
    m = ctx.metrics
    watts = {k: m.get(k) for k in ("watts_idle", "watts_disk_idle", "watts_single", "watts_full")
             if isinstance(m.get(k), (int, float)) and m.get(k) > 0}
    if not watts:
        ctx.abort("blocked",
                  "尚无功率计读数。请按操作指导用计量插座测量后,在本用例「录入指标」表单填入:\n"
                  "- watts_idle: 空闲(盘休眠后)\n- watts_disk_idle: 盘转空闲\n"
                  "- watts_single: 单维满载\n- watts_full: 综合满载\n"
                  "填入后再次点击运行即可自动计算能效。")
    # 吞吐参照(同 run 已测结果)
    refs = {}
    r51 = db.get_result(ctx.run_id, "S5-01")
    if r51 and r51["status"] == "pass" and r51["metrics"].get("write_mbps"):
        refs["SMB 写"] = r51["metrics"]["write_mbps"]
    r32 = None
    lv = raid.pick_level(raid.results_by_id(ctx.run_id), ctx.cfg)
    if lv:
        r32 = db.get_result(ctx.run_id, raid.level_case(lv, "02"))
    if r32 and r32["status"] == "pass" and r32["metrics"].get("seq_write_w_mbps"):
        refs["本地顺序写"] = r32["metrics"]["seq_write_w_mbps"]
    eff = {}
    for wk, wv in watts.items():
        for label, mbps in refs.items():
            eff[f"{wk}_{label}"] = round(mbps / wv, 2)
    ctx.metric("efficiency_mb_s_per_w", eff)
    txt = ", ".join(f"{k}={v}W" for k, v in watts.items())
    eff_txt = "; ".join(f"{l}: {v}MB/s/W" for l, v in eff.items()) or "无可用吞吐参照(先完成 S5-01)"
    return f"功耗读数: {txt};能效: {eff_txt}(ARM NAS 核心卖点,§5.9)"
