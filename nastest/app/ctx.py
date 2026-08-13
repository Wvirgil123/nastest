"""用例执行上下文与共享助手(极限性能测试)。

Ctx 由 executor 构造,贯穿单条用例:
- sh()      设备端 SSH 执行,全程写入 stdout.log 证据
- sh_local() 本机(测试客户端)执行,sudo 走 local_sudo_pass
- metric()/log()/abort()/stop_requested()
- put_file() SFTP 上传

模块级助手:轮次中位数、工具检查与自动安装、SMB 会话/共享发现、
设备资源快照、iperf3 服务端/客户端、fio 矩阵执行。
"""
import os
import shlex
import shutil
import statistics
import subprocess
import time
from pathlib import Path

from . import config, evidence
from .parse import (parse_fio_fsync, parse_fio_json, parse_iperf3_json,
                    parse_iperf3_multi)
from .sshc import ExecResult, SSHError

try:
    import smbclient
except ImportError:  # pragma: no cover
    smbclient = None


# ---------------- 存储卷自动探测(volume_path 留空时使用) ----------------
# 优先 fnOS 的 /vol1、/vol2… 数据卷,取剩余空间最大者;
# 无 /vol* 时回退:剩余空间最大的非系统 btrfs/ext4/xfs 挂载点。
_VOLUME_DETECT_CMD = r"""df -T -k | awk 'NR>1 && ($2=="btrfs"||$2=="ext4"||$2=="xfs") {
  if ($7 ~ /^\/vol[0-9]+$/) vol[$7]=$5+0
  else if ($7 != "/" && $7 !~ /^\/(boot|etc|proc|sys|dev|run|tmp|var\/lib\/docker|snap)/) other[$7]=$5+0
} END {
  best=""; max=-1
  for (m in vol) if (vol[m]>max) {max=vol[m]; best=m}
  if (best=="") for (m in other) if (other[m]>max) {max=other[m]; best=m}
  print best
}'"""


def detect_data_volume(run_fn) -> str:
    """run_fn(cmd) → 带 .out 的结果对象。返回探测到的数据卷挂载点,失败返回 ''。"""
    try:
        r = run_fn(_VOLUME_DETECT_CMD)
        lines = [ln.strip() for ln in (r.out or "").splitlines() if ln.strip()]
        return lines[-1] if lines else ""
    except Exception:
        return ""


def _is_mount_point(ctx: "Ctx", vol: str) -> bool:
    """vol 是否为真实挂载点(重建阵列后旧卷路径可能只剩空目录)。"""
    cmd = ("awk -v m=%s '$2==m{f=1} END{exit !f}' /proc/mounts "
           "&& echo mounted || true") % shlex.quote(vol)
    try:
        r = ctx.sh(cmd, timeout=20, note="校验存储卷挂载点")
        return "mounted" in (r.out or "")
    except Exception:
        return True   # 检查失败时不阻塞,按原配置继续


def resolve_storage_cfg(ctx: "Ctx") -> None:
    """确保 cfg 中 volume_path/device_test_dir 已解析且有效。

    - 留空:自动探测并持久化到任务配置;
    - 已填但不是真实挂载点(如重建阵列后卷路径变化):自动切换到探测到的
      数据卷并持久化,日志显著提示;
    - 探测失败:回退 /vol2 并记日志(用例失败时以证据日志为准)。
    """
    from . import db
    vol = str(ctx.cfg.get("volume_path") or "").strip()
    tdir = str(ctx.cfg.get("device_test_dir") or "").strip()
    detected = False
    if vol and not _is_mount_point(ctx, vol):
        alt = detect_data_volume(lambda c: ctx.sh(c, timeout=30, note="自动探测存储卷"))
        if alt and alt != vol:
            ctx.log(f"[!] 配置的存储卷 {vol} 已不是挂载点(阵列可能已重建),"
                    f"自动切换到探测到的数据卷 {alt}")
            if tdir.startswith(vol.rstrip("/") + "/") or not tdir:
                tdir = ""          # 测试目录跟随新卷重新推导
            vol = alt
            detected = True
        else:
            ctx.log(f"[!] 配置的存储卷 {vol} 不是挂载点,测试将落在该目录所在文件系统"
                    f"(可能不是数据卷);建议核对任务配置中的存储卷路径")
    if not vol:
        vol = detect_data_volume(lambda c: ctx.sh(c, timeout=30, note="自动探测存储卷"))
        detected = True
        if vol:
            ctx.log(f"[自动探测] 存储卷: {vol}")
        else:
            vol = "/vol2"
            ctx.log("[!] 未能自动探测到数据卷,回退默认 /vol2;如不正确请在任务配置中手工填写存储卷路径")
    if not tdir:
        tdir = f"{vol.rstrip('/')}/.nastest_perf"
    if vol == ctx.cfg.get("volume_path") and tdir == ctx.cfg.get("device_test_dir"):
        return
    ctx.cfg["volume_path"] = vol
    ctx.cfg["device_test_dir"] = tdir
    run = db.get_run(ctx.run_id)
    if run:
        saved = dict(run["config"])
        saved["volume_path"] = vol
        saved["device_test_dir"] = tdir
        db.update_run(ctx.run_id, config=saved)
        if detected:
            ctx.log(f"[自动探测] 测试目录: {tdir}(已写入任务配置)")


class CaseAbort(Exception):
    def __init__(self, status: str, msg: str):
        super().__init__(msg)
        self.status = status
        self.msg = msg


class Ctx:
    def __init__(self, run_id: int, cfg: dict, ssh, log_fn,
                 case_id: str = "", stop_fn=None):
        self.run_id = run_id
        self.cfg = cfg
        self.ssh = ssh
        self._log = log_fn
        self.case_id = case_id
        self._stop_fn = stop_fn
        self.metrics: dict = {}
        self.started = time.time()

    def log(self, text: str) -> None:
        self._log(text.rstrip("\n") + "\n")

    def stop_requested(self) -> bool:
        return bool(self._stop_fn and self._stop_fn())

    # ---------- 设备端 ----------
    def sh(self, cmd: str, timeout: int = 120, sudo: bool = False,
           note: str | None = None, cwd: str | None = "/tmp"):
        if note:
            self.log(f"--- {note} ---")
        self.log(f"$ {cmd}")
        try:
            r = self.ssh.run(cmd, timeout=timeout, sudo=sudo, cwd=cwd)
        except SSHError as e:
            raise CaseAbort("fail", f"SSH 执行失败: {e}")
        self._log_result(r, timeout)
        return r

    def put_file(self, local: str | Path, remote: str) -> None:
        """SFTP 上传本机文件到设备。"""
        self.log(f"[上传] {local} -> {remote}")
        try:
            cli = self.ssh._ensure()
            with cli.open_sftp() as sftp:
                sftp.put(str(local), remote)
        except Exception as e:
            raise CaseAbort("fail", f"SFTP 上传失败: {e}")

    # ---------- 本机 ----------
    def sh_local(self, cmd, timeout: int = 120, sudo: bool = False,
                 note: str | None = None, stdin_data: str | None = None):
        """本机执行。cmd 可为 argv 列表或 shell 字符串。日志不含密码。"""
        if note:
            self.log(f"--- {note} ---")
        if isinstance(cmd, str):
            display, shell, run_cmd = cmd, True, cmd
        else:
            display = " ".join(shlex.quote(str(x)) for x in cmd)
            shell, run_cmd = False, list(cmd)
        self.log(f"$ {display}")
        pw = self.cfg.get("local_sudo_pass", "")
        stdin_data = stdin_data or (pw + "\n" if sudo and pw else None)
        if sudo:
            if shell:
                run_cmd = f"echo {shlex.quote(pw)} | sudo -S -p '' bash -c {shlex.quote(cmd)}"
            else:
                run_cmd = ["sudo", "-S", "-p", ""] + list(cmd)
        start = time.time()
        try:
            p = subprocess.run(run_cmd, shell=shell, capture_output=True,
                               text=True, timeout=timeout, input=stdin_data)
            r = ExecResult(rc=p.returncode, out=p.stdout or "", err=p.stderr or "",
                           cmd=display,
                           duration_ms=int((time.time() - start) * 1000))
        except subprocess.TimeoutExpired:
            r = ExecResult(rc=-1, cmd=display, timeout=True,
                           err=f"本机命令超时({timeout}s)",
                           duration_ms=int((time.time() - start) * 1000))
        except FileNotFoundError as e:
            raise CaseAbort("blocked", f"本机缺少命令: {e}")
        self._log_result(r, timeout)
        return r

    def _log_result(self, r: ExecResult, timeout: int) -> None:
        out = (r.out or "").strip()
        if out:
            self.log(out[:20000])
        err = (r.err or "").strip()
        if err:
            self.log(f"[stderr] {err[:5000]}")
        if r.timeout:
            self.log(f"[超时] 命令超过 {timeout}s")
        self.log(f"[rc={r.rc} 耗时={r.duration_ms}ms]\n")

    def metric(self, key: str, value) -> None:
        self.metrics[key] = value

    def abort(self, status: str, msg: str):
        raise CaseAbort(status, msg)


# ================= 轮次与中位数(§3.3) =================
def rounds(ctx: Ctx) -> int:
    return max(1, int(ctx.cfg.get("rounds", 3)))


def record_rounds(ctx: Ctx, key: str, values: list[float]) -> float:
    """记录每轮值 + 中位数;波动 >10% 告警(§3.3)。"""
    med = float(statistics.median(values))
    ctx.metric(key, round(med, 2))
    ctx.metric(f"{key}_rounds", [round(v, 2) for v in values])
    if len(values) > 1 and med > 0:
        spread = (max(values) - min(values)) / med * 100
        ctx.metric(f"{key}_spread_pct", round(spread, 1))
        if spread > 10:
            ctx.log(f"[注意] {key} 轮间波动 {spread:.1f}% > 10%(§3.3),结果可信度下降,建议复测")
    return med


# ================= 工具检查 =================
def require_device_tool(ctx: Ctx, tool: str) -> None:
    r = ctx.sh(f"command -v {tool} || true")
    if not r.out.strip():
        ctx.abort("blocked", f"设备缺少 {tool}。请在预检页一键安装,或设备端执行: "
                             f"sudo apt-get install -y {config.DEVICE_PACKAGES.get(tool, tool)}")


def local_sudo_ok(ctx: Ctx) -> bool:
    return bool(ctx.cfg.get("local_sudo_pass"))


def require_local_sudo(ctx: Ctx, what: str) -> None:
    if not local_sudo_ok(ctx):
        ctx.abort("blocked",
                  f"{what}需要本机 root 权限。两种处理方式:\n"
                  f"1. 在运行配置填写「本机 sudo 密码」后重跑本用例(全自动);\n"
                  f"2. 手动完成操作后,在本页提交人工判定并附上结果。")


def require_local_tool(ctx: Ctx, tool: str) -> None:
    """本机工具检查;缺失且有 local_sudo_pass 时自动 apt 安装。"""
    if shutil.which(tool):
        return
    pkg = config.LOCAL_PACKAGES.get(tool, tool)
    if local_sudo_ok(ctx):
        ctx.log(f"本机缺少 {tool},自动安装 {pkg} ...")
        r = ctx.sh_local(["apt-get", "install", "-y", pkg], sudo=True,
                         timeout=600, note=f"自动安装 {pkg}")
        if shutil.which(tool):
            return
        ctx.abort("blocked", f"自动安装 {pkg} 失败(rc={r.rc}),请手动: sudo apt-get install {pkg}")
    ctx.abort("blocked",
              f"本机缺少 {tool}。请填写本机 sudo 密码由框架自动安装,"
              f"或手动执行: sudo apt-get install -y {pkg}")


# ================= 本机测试文件 =================
def local_test_dir(ctx: Ctx) -> Path:
    d = Path(ctx.cfg.get("local_test_dir") or str(Path.home() / ".nastest_local"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def local_bigfile(ctx: Ctx, mb: int | None = None) -> Path:
    """本机大文件缓存(随机数据),FTP/WebDAV/rsync/并发共用。"""
    mb = mb or int(ctx.cfg.get("smb_bigfile_mb", 512))
    p = local_test_dir(ctx) / f"bigfile_{mb}.bin"
    want = mb * 1024 * 1024
    if p.exists() and p.stat().st_size == want:
        return p
    ctx.log(f"生成本机测试文件 {p} ({mb}MB,随机数据)...")
    t0 = time.time()
    with open(p, "wb") as f:
        left = want
        while left > 0:
            n = min(8 * 1024 * 1024, left)
            f.write(os.urandom(n))
            left -= n
    ctx.log(f"生成完成,{(time.time() - t0):.1f}s")
    return p


# ================= SMB =================
def smb_register(ctx: Ctx, encrypt: bool = False) -> str:
    """注册 smbprotocol 会话(每次重置,确保加密开关生效)。"""
    if smbclient is None:
        ctx.abort("blocked", "本机缺少 smbprotocol 库(pip install smbprotocol)")
    host = ctx.cfg["device_ip"]
    try:
        smbclient.delete_session(host)
    except Exception:
        pass
    try:
        smbclient.register_session(host, username=ctx.cfg.get("smb_user", ""),
                                   password=ctx.cfg.get("smb_pass", ""),
                                   encrypt=encrypt)
    except Exception as e:
        ctx.abort("fail", f"SMB 会话建立失败(encrypt={encrypt}): {e}")
    return host


def smb_share_names(ctx: Ctx) -> list[str]:
    """从设备 samba 配置发现共享名。"""
    r = ctx.sh("cat /etc/samba/users/*.share.conf 2>/dev/null | grep -E '^\\[' | tr -d '[]' || true",
               sudo=True, note="发现 SMB 共享(设备配置)")
    shares = [ln.strip() for ln in r.out.splitlines() if ln.strip()]
    ctx.metric("shares", shares)
    if not shares:
        ctx.abort("blocked", "未发现任何 SMB 共享——请在 fnOS 控制台创建共享后重试")
    return shares


def smb_pick_share(ctx: Ctx, host: str) -> str:
    """探测第一个可写共享。"""
    for s in smb_share_names(ctx):
        try:
            probe = f"\\\\{host}\\{s}\\nastest_probe.txt"
            with smbclient.open_file(probe, mode="w") as f:
                f.write("probe")
            smbclient.remove(probe)
            ctx.log(f"选用共享: {s}")
            return s
        except Exception as e:
            ctx.log(f"共享 {s} 不可写: {e}")
    ctx.abort("blocked", "没有可写共享,请在 fnOS 检查共享权限")


# ================= 设备资源快照 =================
def device_nic(ctx: Ctx) -> str:
    r = ctx.sh("ip -br link | awk '$1!=\"lo\" && $2==\"UP\" {print $1; exit}'")
    nic = r.out.strip().split()[0] if r.out.strip() else "end0"
    ctx.log(f"设备主网卡: {nic}")
    return nic


def device_snapshot(ctx: Ctx, nic: str | None = None) -> dict:
    """静默采集 CPU/内存/网络计数,用于前后差值计算。"""
    nic = nic or "end0"
    try:
        r = ctx.ssh.run(
            f"head -1 /proc/stat; echo @@; grep MemAvailable /proc/meminfo; echo @@; "
            f"cat /sys/class/net/{nic}/statistics/rx_bytes /sys/class/net/{nic}/statistics/tx_bytes",
            timeout=15)
    except SSHError:
        return {}
    sec = r.out.split("@@")
    if len(sec) < 3:
        return {}
    try:
        f = sec[0].strip().split()
        total = sum(int(x) for x in f[1:])
        idle = int(f[4])
        mem_kb = int(sec[1].split()[1])
        rx, tx = [int(x) for x in sec[2].split()]
        return {"t": time.time(), "total": total, "idle": idle,
                "mem_avail_mb": round(mem_kb / 1024), "rx": rx, "tx": tx}
    except (ValueError, IndexError):
        return {}


def device_delta(a: dict, b: dict) -> dict:
    """两次快照间的 CPU%/网络速率。"""
    if not a or not b:
        return {}
    dt = max(b["t"] - a["t"], 0.001)
    d_total = b["total"] - a["total"]
    out = {}
    if d_total > 0:
        out["cpu_pct"] = round(100.0 * (1 - (b["idle"] - a["idle"]) / d_total), 1)
    out["net_rx_mbps"] = round((b["rx"] - a["rx"]) / dt / 1e6, 1)
    out["net_tx_mbps"] = round((b["tx"] - a["tx"]) / dt / 1e6, 1)
    out["dt_s"] = round(dt, 1)
    return out


# ================= iperf3 =================
def iperf_servers(ctx: Ctx, ports: list[int] | None = None) -> list[int]:
    require_device_tool(ctx, "iperf3")
    ports = ports or ctx.cfg.get("iperf_ports", [5201])
    ctx.sh("pkill iperf3 2>/dev/null || true", note="清理设备残留 iperf3")
    for p in ports:
        ctx.sh(f"iperf3 -s -D -p {p}")
    time.sleep(1)
    ctx.log(f"iperf3 服务端已启动: {ports}")
    return ports


def iperf_cleanup(ctx: Ctx) -> None:
    ctx.sh("pkill iperf3 2>/dev/null || true")


def iperf_local(ctx: Ctx, port: int, t: int, reverse: bool = False,
                udp: bool = False) -> dict:
    require_local_tool(ctx, "iperf3")
    ip = ctx.cfg["device_ip"]
    argv = ["iperf3", "-c", ip, "-p", str(port), "-t", str(t), "-J"]
    if reverse:
        argv.append("-R")
    if udp:
        argv += ["-u", "-l", "64", "-b", "0"]
    r = ctx.sh_local(argv, timeout=t + 90,
                     note=f"iperf3 {'下行(-R)' if reverse else '上行'}{' UDP' if udp else ''} {t}s")
    try:
        return parse_iperf3_json(r.out)
    except ValueError as e:
        ctx.abort("fail", f"iperf3 结果解析失败: {e}(输出见日志)")


def iperf_multi_local(ctx: Ctx, ports: list[int], t: int) -> dict:
    require_local_tool(ctx, "iperf3")
    ip = ctx.cfg["device_ip"]
    ctx.log(f"本机并发 {len(ports)} 个 iperf3 客户端,t={t}s")
    procs = [subprocess.Popen(["iperf3", "-c", ip, "-p", str(p), "-t", str(t), "-J"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for p in ports]
    outs = []
    for i, p in enumerate(procs):
        try:
            out, err = p.communicate(timeout=t + 120)
        except subprocess.TimeoutExpired:
            p.kill()
            ctx.abort("fail", f"iperf3 多流第 {i + 1} 实例超时")
        if p.returncode != 0:
            ctx.log(f"流 {ports[i]} rc={p.returncode}: {err.strip()[:200]}")
        outs.append(out)
    try:
        return parse_iperf3_multi(outs)
    except ValueError as e:
        ctx.abort("fail", f"iperf3 多流结果解析失败: {e}")


# ================= fio =================
FIO_PROFILES = {
    "seq_read":  dict(rw="read", bs="1M", iodepth=32, numjobs=4),
    "seq_write": dict(rw="write", bs="1M", iodepth=32, numjobs=4),
    "rand_read":  dict(rw="randread", bs="4k", iodepth=32, numjobs=4),
    "rand_write": dict(rw="randwrite", bs="4k", iodepth=32, numjobs=4),
    "randrw":    dict(rw="randrw", bs="4k", iodepth=64, numjobs=4,
                      extra="--rwmixread=70"),
    "qd1_lat":   dict(rw="randread", bs="4k", iodepth=1, numjobs=1),
    "fsync_lat": dict(rw="write", bs="4k", iodepth=1, numjobs=1, extra="--fsync=1"),
}


def fio_cmd(key: str, target: str, runtime: int, size: str, direct: int = 1,
            is_dir: bool = True) -> str:
    p = FIO_PROFILES[key]
    loc = f"--directory={target}" if is_dir else f"--filename={target}"
    return (f"fio --name={key} {loc} --size={size} --rw={p['rw']} --bs={p['bs']} "
            f"--iodepth={p['iodepth']} --numjobs={p['numjobs']} --direct={direct} "
            f"--time_based --runtime={runtime} {p.get('extra', '')} "
            f"--group_reporting --output-format=json")


def fio_profile(ctx: Ctx, key: str, runtime: int | None = None,
                size: str | None = None) -> dict:
    """设备端 fio 矩阵:热身 + rounds 轮取中位数。返回各指标中位数。"""
    require_device_tool(ctx, "fio")
    tdir = ctx.cfg.get("device_test_dir", "/vol2/.nastest_perf")
    if not ctx.sh(f"mkdir -p {tdir}").ok:
        ctx.sh(f"mkdir -p {tdir} && chmod 777 {tdir}", sudo=True)
    runtime = runtime or int(ctx.cfg.get("fio_runtime", 60))
    size = size or ctx.cfg.get("fio_size", "4G")
    warm = int(ctx.cfg.get("warmup_seconds", 30))
    n = rounds(ctx)
    nic = device_nic(ctx)
    ctx.log(f"profile={key} size={size} runtime={runtime}s warmup={warm}s rounds={n}")
    if warm > 0:
        r = ctx.sh(fio_cmd(key, tdir, warm, size), timeout=warm + 600,
                   note=f"热身 {warm}s")
        if r.rc != 0:
            ctx.abort("fail", f"热身 fio 失败 rc={r.rc}({key}):测试目录 {tdir} 不可用?"
                              f"常见原因:卷未挂载/不支持 direct=1/目录无写权限,详见 stdout.log")
    per_round, raws, cpus = [], [], []
    for i in range(n):
        if ctx.stop_requested():
            ctx.abort("fail", "用户中止")
        pre = device_snapshot(ctx, nic)
        r = ctx.sh(fio_cmd(key, tdir, runtime, size), timeout=runtime + 600,
                   note=f"第 {i + 1}/{n} 轮")
        delta = device_delta(pre, device_snapshot(ctx, nic))
        if delta.get("cpu_pct") is not None:
            cpus.append(delta["cpu_pct"])
        if r.rc != 0:
            ctx.abort("fail", f"fio 运行失败 rc={r.rc}({key}) 第 {i + 1} 轮:"
                              f"测试目录 {tdir} 不可用?常见原因:卷未挂载/文件系统不支持"
                              f" direct=1/目录无写权限,详见 stdout.log")
        try:
            parsed = parse_fio_fsync(r.out) if key == "fsync_lat" else parse_fio_json(r.out)
        except ValueError as e:
            ctx.abort("fail", f"fio JSON 解析失败({key}): {e},输出见 stdout.log")
        per_round.append(parsed)
        raws.append(r.out)
    evidence.save_text(ctx.run_id, ctx.case_id, f"fio_{key}_raw.txt",
                       "\n".join(f"===== round {i + 1} =====\n{t}" for i, t in enumerate(raws)))
    med: dict = {}
    keys = set().union(*[set(d) for d in per_round]) if per_round else set()
    for k in sorted(keys):
        vals = [d[k] for d in per_round if isinstance(d.get(k), (int, float))]
        if vals:
            med[k] = record_rounds(ctx, f"{key}_{k}", vals)
    if "r_iops" in med and "w_iops" in med:
        mixed = [d.get("r_iops", 0) + d.get("w_iops", 0) for d in per_round]
        med["mixed_iops"] = record_rounds(ctx, f"{key}_mixed_iops", mixed)
    if cpus:  # 设备整机 CPU%(/proc/stat 快照差值,含内核态),报告 CPU% 列以此为准
        med["dev_cpu_pct"] = record_rounds(ctx, f"{key}_dev_cpu_pct", cpus)
    ctx.sh(f"rm -f {tdir}/{key}.*", note="清理测试文件")
    if not any(med.get(k) for k in ("r_mbps", "w_mbps", "r_iops", "w_iops",
                                    "fsync_lat_us")):
        ctx.abort("fail", f"fio({key}) 完成但所有 IO 指标为 0:测试目录 {tdir} 所在"
                          f"文件系统可能不支持 direct=1 或卷状态异常,详见 stdout.log")
    return med


def fio_local(ctx: Ctx, key: str, directory: str, runtime: int | None = None,
              size: str | None = None) -> dict:
    """本机对挂载点跑 fio 子集;direct=1 失败(部分协议不支持)自动回退 direct=0。"""
    require_local_tool(ctx, "fio")
    runtime = runtime or int(ctx.cfg.get("fio_runtime", 60))
    size = size or ctx.cfg.get("fio_size", "4G")
    nic = device_nic(ctx)
    direct = 1
    pre = device_snapshot(ctx, nic)
    r = ctx.sh_local(fio_cmd(key, directory, runtime, size, direct=direct),
                     timeout=runtime + 600, note=f"本机 fio {key}(挂载点)")
    if r.rc != 0 and "invalid argument" in (r.out + r.err).lower():
        ctx.log("[!] direct=1 在该挂载上不支持,回退 direct=0(含客户端缓存影响,已在报告注明)")
        direct = 0
        ctx.metric("direct_fallback", True)
        pre = device_snapshot(ctx, nic)
        r = ctx.sh_local(fio_cmd(key, directory, runtime, size, direct=direct),
                         timeout=runtime + 600, note=f"本机 fio {key}(direct=0 重试)")
    # fio 在本机运行,设备端 CPU 用 /proc/stat 快照差值计量(NAS 侧整机占用)
    delta = device_delta(pre, device_snapshot(ctx, nic))
    if delta.get("cpu_pct") is not None:
        ctx.metric(f"{key}_dev_cpu_pct", delta["cpu_pct"])
    if r.rc != 0:
        ctx.abort("fail", f"本机 fio 运行失败 rc={r.rc}")
    try:
        parsed = parse_fio_json(r.out)
    except ValueError as e:
        ctx.abort("fail", f"fio JSON 解析失败: {e}")
    for k, v in parsed.items():
        ctx.metric(f"{key}_{k}", v)
    return parsed


def make_soak_sample_fn(ctx: Ctx):
    """浸泡采样函数:温度/频率/负载/内存/CPU%/磁盘IOPS与带宽/网络。"""
    nic = device_nic(ctx)

    def counters():
        r = ctx.ssh.run(
            "head -1 /proc/stat; echo @@; "
            "awk '$3 ~ /^sd[a-z]+$/ {r+=$4; w+=$8; sr+=$6; sw+=$10} END {print r+0, w+0, sr+0, sw+0}' /proc/diskstats; echo @@; "
            f"cat /sys/class/net/{nic}/statistics/rx_bytes /sys/class/net/{nic}/statistics/tx_bytes",
            timeout=20)
        sec = r.out.split("@@")
        f = sec[0].strip().split()
        io = sec[1].strip().split()
        net = sec[2].split()
        return {"total": sum(int(x) for x in f[1:]), "idle": int(f[4]),
                "ior": int(io[0]), "iow": int(io[1]),
                "sr": int(io[2]), "sw": int(io[3]),
                "rx": int(net[0]), "tx": int(net[1]), "t": time.time()}

    def fn():
        c1 = counters()
        time.sleep(2)
        c2 = counters()
        r2 = ctx.ssh.run(
            "cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null | sort -n | tail -1; echo @@; "
            "cut -d' ' -f1 /proc/loadavg; echo @@; "
            "grep MemAvailable /proc/meminfo | awk '{print $2}'; echo @@; "
            "cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq 2>/dev/null | "
            "awk '{s+=$1;n++} END {if(n>0) printf \"%.0f\", s/n}'", timeout=20)
        s = r2.out.split("@@")
        dt = max(c2["t"] - c1["t"], 0.1)
        d_total = c2["total"] - c1["total"]
        return {
            "temp_c": round(int(s[0].strip() or 0) / 1000, 1),
            "load1": float(s[1].strip() or 0),
            "mem_avail_mb": round(int(s[2].strip() or 0) / 1024),
            "freq_mhz": round(int(s[3].strip() or 0) / 1000),
            "cpu_pct": round(100.0 * (1 - (c2["idle"] - c1["idle"]) / d_total), 1) if d_total else None,
            "disk_iops": round(((c2["ior"] - c1["ior"]) + (c2["iow"] - c1["iow"])) / dt),
            "disk_read_mbps": round((c2["sr"] - c1["sr"]) * 512 / dt / 1e6, 1),
            "disk_write_mbps": round((c2["sw"] - c1["sw"]) * 512 / dt / 1e6, 1),
            "net_mbps": round(((c2["rx"] - c1["rx"]) + (c2["tx"] - c1["tx"])) / dt / 1e6, 1),
        }

    return fn
