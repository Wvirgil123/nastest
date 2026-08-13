"""Preflight:开测前的连通性 / 权限 / 依赖 / 环境检查,设备端与本机端依赖安装。

结果写入 runs.preflight_json:
{
  "time": "...",
  "device": {"reachable": bool, "sudo": bool, "error": str,
             "packages": {"fio": true, ...}, "uname": "...",
             "governor": "...", "aes_ce": bool,
             "free_gb": float, "need_gb": float, "disk_free_ok": bool},
  "local": {"iperf3": true, ..., "sudo_ok": bool},
  "missing_device": [...], "missing_local": [...],
  "install": {"status": ..., "log": ..., "packages": [...]},
  "install_local": {"status": ..., "log": ..., "packages": [...]}
}
"""
import re
import shutil
import subprocess
import time

from . import config, db
from .sshc import SSH, SSHError

INSTALL_LOG_LIMIT = 20000


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _merge_preflight(run_id: int, **patch) -> dict:
    run = db.get_run(run_id)
    pf = dict(run.get("preflight") or {})
    pf.update(patch)
    db.update_run(run_id, preflight=pf)
    return pf


def _size_gb(s: str) -> float:
    """'512M'/'4G'/'16G' → GB。"""
    m = re.match(r"([\d.]+)\s*([KMGT]?)B?$", str(s).strip().upper())
    if not m:
        return 4.0
    v = float(m.group(1))
    unit = m.group(2) or "G"
    return v * {"K": 1 / 1024 / 1024, "M": 1 / 1024, "G": 1.0, "T": 1024.0}[unit]


def check(run_id: int) -> dict:
    """执行完整检查(同步,约 10-40s)。返回 preflight dict。"""
    run = db.get_run(run_id)
    cfg = run["config"]
    pf: dict = {"time": _now(), "device": {}, "local": {}, "install": {"status": "idle"},
                "install_local": {"status": "idle"}}

    # ---- 设备侧 ----
    ssh = SSH(cfg["device_ip"], cfg.get("ssh_user", "seeed"),
              cfg.get("ssh_pass", ""), int(cfg.get("ssh_port", 22)))
    try:
        ssh.connect(retries=2)
    except SSHError as e:
        pf["device"] = {"reachable": False, "error": str(e)}
        pf["missing_device"] = list(config.DEVICE_PACKAGES)
        pf["local"] = _local_tools(cfg)
        pf["missing_local"] = _missing_local(pf["local"])
        _merge_preflight(run_id, **pf)
        return pf

    dev: dict = {"reachable": True, "packages": {}}
    r = ssh.run("uname -srmo", timeout=20)
    dev["uname"] = r.out.strip()
    dev["sudo"] = ssh.sudo_ok()
    if not dev["sudo"]:
        dev["sudo_error"] = "sudo 验证失败,依赖安装与 sudo 类用例将受限"
    for tool in config.DEVICE_PACKAGES:
        r = ssh.run(f"command -v {tool} 2>/dev/null || true", timeout=20)
        dev["packages"][tool] = bool(r.out.strip())
    # governor / aes-ce(§3.3 / §2)
    r = ssh.run("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || true",
                timeout=20)
    dev["governor"] = r.out.strip()
    r = ssh.run("grep -cE 'driver *: *(aes-ce|ce-aes)' /proc/crypto 2>/dev/null || true", timeout=20)
    try:
        dev["aes_ce"] = int(r.out.strip() or 0) > 0
    except ValueError:
        dev["aes_ce"] = False
    # 存储卷:留空或已失效(非真实挂载点,如重建阵列后卷路径变化)时,
    # 自动探测(优先 /vol* 数据卷)并持久化到任务配置
    import shlex
    from .ctx import detect_data_volume

    def _mount_ok(v: str) -> bool:
        r = ssh.run(("awk -v m=%s '$2==m{f=1} END{exit !f}' /proc/mounts "
                     "&& echo mounted || true") % shlex.quote(v), timeout=20)
        return "mounted" in (r.out or "")

    vol = str(cfg.get("volume_path") or "").strip()
    if vol and not _mount_ok(vol):
        dev["volume_stale"] = vol
        vol = ""   # 配置的卷已不是挂载点 → 走自动探测
    if not vol:
        vol = detect_data_volume(lambda c: ssh.run(c, timeout=30))
        if vol:
            dev["volume_detected"] = True
            cfg = dict(cfg)
            cfg["volume_path"] = vol
            tdir = str(cfg.get("device_test_dir") or "").strip()
            old = str(dev.get("volume_stale") or "")
            if not tdir or (old and tdir.startswith(old.rstrip("/") + "/")):
                cfg["device_test_dir"] = f"{vol.rstrip('/')}/.nastest_perf"
            db.update_run(run_id, config=cfg)
            if dev.get("volume_stale"):
                dev["volume_warning"] = (f"原配置卷 {dev['volume_stale']} 已不是挂载点"
                                         f"(阵列可能已重建),已自动切换到 {vol}")
        else:
            vol = "/vol2"
            dev["volume_error"] = "未探测到数据卷,已回退 /vol2;请在任务配置中手工填写存储卷路径"
    dev["volume_path"] = vol
    # 剩余空间 vs fio 需求(最大 profile:numjobs=4 × fio_size)
    r = ssh.run(f"df -BG --output=avail {vol} 2>/dev/null | tail -1", timeout=20)
    m = re.search(r"(\d+)", r.out)
    free_gb = float(m.group(1)) if m else 0.0
    need_gb = _size_gb(cfg.get("fio_size", "4G")) * 4 * 1.3  # numjobs=4 + 余量
    dev["free_gb"] = free_gb
    dev["need_gb"] = round(need_gb, 1)
    dev["disk_free_ok"] = free_gb >= need_gb
    if not dev["disk_free_ok"]:
        dev["disk_error"] = (f"卷剩余 {free_gb:.0f}GB < 测试需求 ≈{need_gb:.0f}GB"
                             f"(fio_size={cfg.get('fio_size')}×4 jobs),请调小 fio_size 或清理空间")
    ssh.close()
    pf["device"] = dev
    pf["missing_device"] = [t for t, ok in dev["packages"].items() if not ok]

    # ---- 本机侧 ----
    pf["local"] = _local_tools(cfg)
    pf["missing_local"] = _missing_local(pf["local"])

    _merge_preflight(run_id, **pf)
    return pf


def _missing_local(local: dict) -> list[str]:
    return [t for t, ok in local.items()
            if ok is False and t not in ("sudo_ok", "sudo_error")]


def _local_tools(cfg: dict) -> dict:
    """本机工具检查(含 root 工具与 sudo 有效性验证)。"""
    out: dict = {}
    for t in config.LOCAL_TOOLS:
        out[t] = shutil.which(t) is not None
    for t in config.LOCAL_ROOT_TOOLS:
        out[t] = shutil.which(t) is not None
    pw = cfg.get("local_sudo_pass", "")
    if pw:
        try:
            p = subprocess.run(["sudo", "-S", "-p", "", "true"], input=pw + "\n",
                               capture_output=True, text=True, timeout=30)
            out["sudo_ok"] = p.returncode == 0
        except Exception:
            out["sudo_ok"] = False
        if not out["sudo_ok"]:
            out["sudo_error"] = "本机 sudo 密码验证失败,自动装包/挂载/改 MTU 不可用"
    else:
        out["sudo_ok"] = False
        out["sudo_error"] = "未配置本机 sudo 密码:挂载类/MTU 用例将转人工指引"
    return out


def install_device_packages(run_id: int, packages: list[str] | None = None) -> None:
    """在设备上 apt 安装缺失依赖。在后台线程调用;状态写入 preflight.install。"""
    run = db.get_run(run_id)
    cfg = run["config"]
    pf = run.get("preflight") or {}
    if packages is None:
        packages = pf.get("missing_device") or []
    packages = [p for p in packages if p in config.DEVICE_PACKAGES]
    if not packages:
        _merge_preflight(run_id, install={"status": "done", "log": "无缺失依赖,无需安装", "packages": []})
        return

    apt_pkgs = [config.DEVICE_PACKAGES[p] for p in packages]
    inst = {"status": "running", "packages": packages, "log": f"[{_now()}] 开始安装: {' '.join(apt_pkgs)}\n"}
    _merge_preflight(run_id, install=inst)

    def _log(line: str) -> None:
        nonlocal inst
        inst["log"] = (inst["log"] + line + "\n")[-INSTALL_LOG_LIMIT:]
        _merge_preflight(run_id, install=dict(inst))

    ssh = SSH(cfg["device_ip"], cfg.get("ssh_user", "seeed"),
              cfg.get("ssh_pass", ""), int(cfg.get("ssh_port", 22)))
    try:
        ssh.connect(retries=2)
        r = ssh.run("apt-get update -qq", sudo=True, timeout=300)
        _log(f"apt-get update rc={r.rc}")
        if r.err.strip():
            _log(r.err.strip()[:2000])
        r = ssh.run(f"DEBIAN_FRONTEND=noninteractive apt-get install -y {' '.join(apt_pkgs)}",
                    sudo=True, timeout=900)
        _log(f"apt-get install rc={r.rc}")
        _log((r.out or r.err).strip()[-3000:])
        ok = r.ok
        still_missing = []
        for tool in packages:
            rr = ssh.run(f"command -v {tool} 2>/dev/null || true", timeout=20)
            if not rr.out.strip():
                still_missing.append(tool)
        ssh.close()
        if ok and not still_missing:
            inst["status"] = "done"
            _log(f"[{_now()}] 安装完成,全部依赖就绪")
        else:
            inst["status"] = "error"
            _log(f"[{_now()}] 安装后仍缺失: {still_missing or '未知(rc=%d)' % r.rc}")
    except SSHError as e:
        inst["status"] = "error"
        _log(f"[{_now()}] SSH 错误: {e}")
    try:
        refresh_packages(run_id)
    except Exception:
        pass
    _merge_preflight(run_id, install=dict(inst))


def install_local_packages(run_id: int, packages: list[str] | None = None) -> None:
    """本机 apt 自动安装缺失工具(local_sudo_pass)。后台线程调用。"""
    run = db.get_run(run_id)
    cfg = run["config"]
    pw = cfg.get("local_sudo_pass", "")
    pf = run.get("preflight") or {}
    if packages is None:
        packages = pf.get("missing_local") or []
    packages = [p for p in packages if p in config.LOCAL_PACKAGES]
    if not pw:
        _merge_preflight(run_id, install_local={
            "status": "error", "packages": packages,
            "log": "未配置本机 sudo 密码,无法自动安装。请在运行配置填写后重试,"
                   "或手动: sudo apt-get install " +
                   " ".join(config.LOCAL_PACKAGES[p] for p in packages)})
        return
    if not packages:
        _merge_preflight(run_id, install_local={"status": "done", "log": "本机无缺失工具", "packages": []})
        return

    apt_pkgs = [config.LOCAL_PACKAGES[p] for p in packages]
    inst = {"status": "running", "packages": packages,
            "log": f"[{_now()}] 本机安装: {' '.join(apt_pkgs)}\n"}
    _merge_preflight(run_id, install_local=inst)

    def _log(line: str) -> None:
        nonlocal inst
        inst["log"] = (inst["log"] + line + "\n")[-INSTALL_LOG_LIMIT:]
        _merge_preflight(run_id, install_local=dict(inst))

    try:
        subprocess.run(["sudo", "-S", "-p", "", "apt-get", "update", "-qq"],
                       input=pw + "\n", capture_output=True, text=True, timeout=300)
        p = subprocess.run(["sudo", "-S", "-p", "", "apt-get", "install", "-y"] + apt_pkgs,
                           input=pw + "\n", capture_output=True, text=True, timeout=900)
        _log(f"apt-get install rc={p.returncode}")
        _log((p.stdout or p.stderr).strip()[-3000:])
        still = [t for t in packages if not shutil.which(t)]
        if p.returncode == 0 and not still:
            inst["status"] = "done"
            _log(f"[{_now()}] 本机依赖就绪")
        else:
            inst["status"] = "error"
            _log(f"[{_now()}] 仍缺失: {still or 'rc=' + str(p.returncode)}")
    except Exception as e:
        inst["status"] = "error"
        _log(f"[{_now()}] 安装异常: {e}")
    # 刷新本机工具状态
    pf = dict(db.get_run(run_id).get("preflight") or {})
    pf["local"] = _local_tools(cfg)
    pf["missing_local"] = _missing_local(pf["local"])
    db.update_run(run_id, preflight=pf)
    _merge_preflight(run_id, install_local=dict(inst))


def refresh_packages(run_id: int) -> None:
    """安装后刷新 device.packages / missing_device。"""
    run = db.get_run(run_id)
    cfg = run["config"]
    pf = dict(run.get("preflight") or {})
    ssh = SSH(cfg["device_ip"], cfg.get("ssh_user", "seeed"),
              cfg.get("ssh_pass", ""), int(cfg.get("ssh_port", 22)))
    try:
        ssh.connect(retries=2)
        dev = dict(pf.get("device") or {})
        pkgs = {}
        for tool in config.DEVICE_PACKAGES:
            r = ssh.run(f"command -v {tool} 2>/dev/null || true", timeout=20)
            pkgs[tool] = bool(r.out.strip())
        dev["packages"] = pkgs
        pf["device"] = dev
        pf["missing_device"] = [t for t, ok in pkgs.items() if not ok]
        ssh.close()
    except SSHError as e:
        pf.setdefault("device", {})["error"] = str(e)
    db.update_run(run_id, preflight=pf)
