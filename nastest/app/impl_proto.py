"""S5 文件协议性能 / S6 并发与混合负载 用例实现。"""
import ftplib
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from .ctx import (CaseAbort, Ctx, device_delta, device_nic, device_snapshot,
                  fio_local, local_bigfile, local_test_dir, require_local_sudo,
                  require_local_tool, smb_pick_share, smb_register,
                  smb_share_names)

IMPL: dict[str, callable] = {}

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None
try:
    import smbclient
except ImportError:  # pragma: no cover
    smbclient = None


def _impl(key: str):
    def deco(fn):
        IMPL[key] = fn
        return fn
    return deco


# ================= SMB 通用 =================
_CHUNK = 1024 * 1024
_BLOCK = b"\xa5" * _CHUNK


def _save_cpu(ctx: Ctx, key: str, pre: dict, post: dict) -> None:
    """记录传输阶段内设备整机 CPU%(/proc/stat 快照差值)。"""
    d = device_delta(pre, post)
    if d.get("cpu_pct") is not None:
        ctx.metric(key, d["cpu_pct"])


def _smb_write_mb(path: str, mb: int) -> float:
    t0 = time.time()
    with smbclient.open_file(path, mode="wb", buffering=_CHUNK) as f:
        for _ in range(mb):
            f.write(_BLOCK)
    return time.time() - t0


def _smb_read_mb(path: str) -> tuple[float, int]:
    t0 = time.time()
    total = 0
    with smbclient.open_file(path, mode="rb", buffering=_CHUNK) as f:
        while True:
            b = f.read(_CHUNK)
            if not b:
                break
            total += len(b)
    return time.time() - t0, total


@_impl("s5_smb_soft")
def s5_smb_soft(ctx: Ctx) -> str:
    host = smb_register(ctx, encrypt=False)
    share = smb_pick_share(ctx, host)
    mb = int(ctx.cfg.get("smb_bigfile_mb", 512))
    path = f"\\\\{host}\\{share}\\nastest_perf_{mb}.bin"
    nic = device_nic(ctx)
    try:
        pre = device_snapshot(ctx, nic)
        ws = _smb_write_mb(path, mb)
        _save_cpu(ctx, "write_cpu_pct", pre, device_snapshot(ctx, nic))
        pre = device_snapshot(ctx, nic)
        rs, _ = _smb_read_mb(path)
        _save_cpu(ctx, "read_cpu_pct", pre, device_snapshot(ctx, nic))
    except Exception as e:
        ctx.abort("fail", f"SMB 大文件传输失败: {e}")
    finally:
        try:
            smbclient.remove(path)
        except Exception:
            pass
    w, r = mb / ws, mb / rs
    ctx.metric("bigfile_mb", mb)
    ctx.metric("write_mbps", round(w, 1))
    ctx.metric("read_mbps", round(r, 1))
    return (f"SMB 纯软件(共享 {share}): {mb}MB 写 {w:.1f}MB/s 读 {r:.1f}MB/s"
            f"(完整验收建议 smb_bigfile_mb ≥4096)")


@_impl("s5_smb_seal")
def s5_smb_seal(ctx: Ctx) -> str:
    mb = int(ctx.cfg.get("smb_bigfile_mb", 512))
    host = smb_register(ctx, encrypt=False)
    share = smb_pick_share(ctx, host)
    p_plain = f"\\\\{host}\\{share}\\nastest_seal_plain.bin"
    p_seal = f"\\\\{host}\\{share}\\nastest_seal_enc.bin"
    nic = device_nic(ctx)
    try:
        ctx.log("== 明文传输 ==")
        pre = device_snapshot(ctx, nic)
        pw_ = _smb_write_mb(p_plain, mb)
        _save_cpu(ctx, "plain_write_cpu_pct", pre, device_snapshot(ctx, nic))
        pre = device_snapshot(ctx, nic)
        pr_, _ = _smb_read_mb(p_plain)
        _save_cpu(ctx, "plain_read_cpu_pct", pre, device_snapshot(ctx, nic))
        ctx.log("== 加密(seal)传输 ==")
        try:
            smbclient.delete_session(host)
        except Exception:
            pass
        try:
            smbclient.register_session(host, username=ctx.cfg.get("smb_user", ""),
                                       password=ctx.cfg.get("smb_pass", ""),
                                       encrypt=True)
        except Exception as e:
            ctx.abort("blocked", f"服务端不支持 SMB 加密会话: {e}")
        pre = device_snapshot(ctx, nic)
        ew = _smb_write_mb(p_seal, mb)
        _save_cpu(ctx, "seal_write_cpu_pct", pre, device_snapshot(ctx, nic))
        pre = device_snapshot(ctx, nic)
        er_, _ = _smb_read_mb(p_seal)
        _save_cpu(ctx, "seal_read_cpu_pct", pre, device_snapshot(ctx, nic))
    except CaseAbort:
        raise
    except Exception as e:
        ctx.abort("fail", f"SMB seal 测试失败: {e}")
    finally:
        for p in (p_plain, p_seal):
            try:
                smbclient.remove(p)
            except Exception:
                pass
    plain_w, plain_r = mb / pw_, mb / pr_
    enc_w, enc_r = mb / ew, mb / er_
    loss_w = round((plain_w - enc_w) / plain_w * 100, 1)
    loss_r = round((plain_r - enc_r) / plain_r * 100, 1)
    ctx.metric("plain_write_mbps", round(plain_w, 1))
    ctx.metric("plain_read_mbps", round(plain_r, 1))
    ctx.metric("seal_write_mbps", round(enc_w, 1))
    ctx.metric("seal_read_mbps", round(enc_r, 1))
    ctx.metric("encrypt_loss_write_pct", loss_w)
    ctx.metric("encrypt_loss_read_pct", loss_r)
    return (f"SMB 加密对比: 写 {plain_w:.1f}→{enc_w:.1f}MB/s(损失 {loss_w}%), "
            f"读 {plain_r:.1f}→{enc_r:.1f}MB/s(损失 {loss_r}%)。§8:<20% 优;"
            f"腰斩以上 → AES 硬件指令未启用(红旗)")


@_impl("s5_smb_mount")
def s5_smb_mount(ctx: Ctx) -> str:
    require_local_sudo(ctx, "SMB 挂载 ")
    require_local_tool(ctx, "fio")
    share = smb_share_names(ctx)[0]
    ip = ctx.cfg["device_ip"]
    user = ctx.cfg.get("smb_user", "")
    pw = ctx.cfg.get("smb_pass", "")
    mnt = "/mnt/nastest_smb"
    ctx.sh_local(["mkdir", "-p", mnt], sudo=True)
    opts = (f"username={user},password={pw},vers=3.1.1,cache=none,"
            f"rsize=1048576,wsize=1048576,uid={os.getuid()},gid={os.getgid()}")
    r = ctx.sh_local(["mount", "-t", "cifs", f"//{ip}/{share}", mnt, "-o", opts],
                     sudo=True, timeout=60, note=f"挂载 //{ip}/{share}")
    if r.rc != 0:
        ctx.abort("fail", f"SMB 挂载失败 rc={r.rc}(见 stderr)")
    try:
        res = {}
        for key in ("seq_write", "seq_read", "randrw"):
            res[key] = fio_local(ctx, key, mnt)
        return (f"SMB 挂载 fio: 顺序写 {res['seq_write'].get('w_mbps')}MB/s, "
                f"顺序读 {res['seq_read'].get('r_mbps')}MB/s, "
                f"混合 IOPS {res['randrw'].get('r_iops', 0) + res['randrw'].get('w_iops', 0):.0f}")
    finally:
        u = ctx.sh_local(["umount", mnt], sudo=True, timeout=60)
        if u.rc != 0:
            ctx.sh_local(["umount", "-l", mnt], sudo=True, note="懒卸载兜底")


@_impl("s5_nfs_mount")
def s5_nfs_mount(ctx: Ctx) -> str:
    require_local_sudo(ctx, "NFS 挂载 ")
    require_local_tool(ctx, "fio")
    ip = ctx.cfg["device_ip"]
    vol = ctx.cfg.get("volume_path", "/vol2")
    export_path = f"{vol}/nastest_nfs_export"
    mnt = "/mnt/nastest_nfs"
    # 设备侧:目录 + exports 配置(备份恢复)
    ctx.sh(f"mkdir -p {export_path} && chmod 777 {export_path}", sudo=True)
    ctx.sh("cp /etc/exports /tmp/nastest_exports.bak 2>/dev/null || touch /tmp/nastest_exports.bak",
           sudo=True, note="备份 /etc/exports")
    ctx.sh(f'cp /tmp/nastest_exports.bak /etc/exports && '
           f'echo "{export_path} *(rw,no_subtree_check,no_root_squash,insecure)" >> /etc/exports && '
           f'exportfs -ra', sudo=True, note="添加测试导出并生效")
    try:
        ctx.sh("exportfs -v", sudo=True)
        ctx.sh_local(["mkdir", "-p", mnt], sudo=True)
        r = ctx.sh_local(["mount", "-t", "nfs",
                          "-o", "vers=4.1,rsize=1048576,wsize=1048576,nconnect=8",
                          f"{ip}:{export_path}", mnt], sudo=True, timeout=60,
                         note="NFS 挂载(nconnect=8)")
        if r.rc != 0:
            ctx.log("nconnect=8 挂载失败,回退普通挂载")
            r = ctx.sh_local(["mount", "-t", "nfs",
                              "-o", "vers=4.1,rsize=1048576,wsize=1048576",
                              f"{ip}:{export_path}", mnt], sudo=True, timeout=60)
            ctx.metric("nconnect_fallback", True)
        if r.rc != 0:
            ctx.abort("fail", f"NFS 挂载失败 rc={r.rc}")
        try:
            res = {}
            for key in ("seq_write", "seq_read", "randrw"):
                res[key] = fio_local(ctx, key, mnt)
            return (f"NFS 挂载 fio: 顺序写 {res['seq_write'].get('w_mbps')}MB/s, "
                    f"顺序读 {res['seq_read'].get('r_mbps')}MB/s, "
                    f"混合 IOPS {res['randrw'].get('r_iops', 0) + res['randrw'].get('w_iops', 0):.0f}")
        finally:
            u = ctx.sh_local(["umount", mnt], sudo=True, timeout=60)
            if u.rc != 0:
                ctx.sh_local(["umount", "-l", mnt], sudo=True, note="懒卸载兜底")
    finally:
        ctx.sh("cp /tmp/nastest_exports.bak /etc/exports && exportfs -ra && "
               "rm -f /tmp/nastest_exports.bak", sudo=True, note="恢复 /etc/exports")
        ctx.sh(f"rm -rf {export_path}", sudo=True)


@_impl("s5_ftp")
def s5_ftp(ctx: Ctx) -> str:
    ip = ctx.cfg["device_ip"]
    port = int(ctx.cfg.get("ftp_port", 21))
    user, pw = ctx.cfg.get("smb_user", ""), ctx.cfg.get("smb_pass", "")
    src = local_bigfile(ctx)
    mb = src.stat().st_size / 1024 / 1024
    fname = "nastest_perf.bin"
    try:
        ftp = ftplib.FTP()
        ftp.connect(ip, port, timeout=30)
        ftp.login(user, pw)
        ftp.set_pasv(True)
        listing = ftp.nlst()
        target = next((d for d in listing if d not in (".", "..")), None)
        if target:
            ftp.cwd(target)
            ctx.log(f"进入共享目录: {target}")
        nic = device_nic(ctx)
        ctx.log(f"上传 {mb:.0f}MB ...")
        pre = device_snapshot(ctx, nic)
        t0 = time.time()
        with open(src, "rb") as f:
            ftp.storbinary(f"STOR {fname}", f)
        up_s = time.time() - t0
        _save_cpu(ctx, "upload_cpu_pct", pre, device_snapshot(ctx, nic))
        ctx.log(f"下载 {mb:.0f}MB ...")
        pre = device_snapshot(ctx, nic)
        t0 = time.time()
        got = [0]

        def _cnt(b):
            got[0] += len(b)

        ftp.retrbinary(f"RETR {fname}", _cnt)
        dn_s = time.time() - t0
        _save_cpu(ctx, "download_cpu_pct", pre, device_snapshot(ctx, nic))
        ftp.delete(fname)
        ftp.quit()
    except CaseAbort:
        raise
    except Exception as e:
        ctx.abort("fail", f"FTP 传输失败: {e}")
    up, dn = mb / up_s, mb / dn_s
    ctx.metric("upload_mbps", round(up, 1))
    ctx.metric("download_mbps", round(dn, 1))
    return f"FTP {mb:.0f}MB: 上传 {up:.1f}MB/s, 下载 {dn:.1f}MB/s"


@_impl("s5_webdav")
def s5_webdav(ctx: Ctx) -> str:
    if httpx is None:
        ctx.abort("blocked", "本机缺少 httpx")
    import re
    base = (ctx.cfg.get("webdav_url") or f"http://{ctx.cfg['device_ip']}:5005").rstrip("/")
    user, pw = ctx.cfg.get("smb_user", ""), ctx.cfg.get("smb_pass", "")
    auth = (user, pw)
    # 发现可写集合
    sub = ""
    try:
        resp = httpx.request("PROPFIND", base, auth=auth, headers={"Depth": "1"}, timeout=15)
        for chunk in resp.text.split("<D:response>")[1:]:
            m = re.search(r"<D:href>([^<]+)</D:href>", chunk)
            if m and "collection" in chunk:
                path = m.group(1).rstrip("/")
                if path and path != "/":
                    sub = path.split("/")[-1]
                    break
    except Exception as e:
        ctx.abort("fail", f"WebDAV 探测失败: {e}")
    if not sub:
        ctx.abort("blocked", "WebDAV 根目录下没有可用集合——请在 fnOS 为共享开启 WebDAV")
    ctx.log(f"使用集合: /{sub}")
    src = local_bigfile(ctx)
    mb = src.stat().st_size / 1024 / 1024
    url = f"{base}/{sub}/nastest_perf.bin"
    nic = device_nic(ctx)
    try:
        pre = device_snapshot(ctx, nic)
        t0 = time.time()
        with open(src, "rb") as f:
            p = httpx.put(url, content=f, auth=auth,
                          timeout=httpx.Timeout(30.0, read=600.0, write=600.0))
        up_s = time.time() - t0
        _save_cpu(ctx, "upload_cpu_pct", pre, device_snapshot(ctx, nic))
        if p.status_code not in (200, 201, 204):
            ctx.abort("fail", f"WebDAV PUT 失败: HTTP {p.status_code}")
        pre = device_snapshot(ctx, nic)
        t0 = time.time()
        got = 0
        with httpx.stream("GET", url, auth=auth,
                          timeout=httpx.Timeout(30.0, read=600.0)) as r:
            if r.status_code != 200:
                ctx.abort("fail", f"WebDAV GET 失败: HTTP {r.status_code}")
            for chunk in r.iter_bytes(1024 * 1024):
                got += len(chunk)
        dn_s = time.time() - t0
        _save_cpu(ctx, "download_cpu_pct", pre, device_snapshot(ctx, nic))
        httpx.delete(url, auth=auth, timeout=60)
    except CaseAbort:
        raise
    except Exception as e:
        ctx.abort("fail", f"WebDAV 传输异常: {e}")
    up, dn = mb / up_s, got / 1024 / 1024 / dn_s
    ctx.metric("upload_mbps", round(up, 1))
    ctx.metric("download_mbps", round(dn, 1))
    return f"WebDAV({base}/{sub}) {mb:.0f}MB: PUT {up:.1f}MB/s, GET {dn:.1f}MB/s"


@_impl("s5_rsync")
def s5_rsync(ctx: Ctx) -> str:
    require_local_tool(ctx, "rsync")
    require_local_tool(ctx, "sshpass")
    ip = ctx.cfg["device_ip"]
    user = ctx.cfg.get("ssh_user", "")
    pw = ctx.cfg.get("ssh_pass", "")
    tdir = ctx.cfg.get("device_test_dir", "/vol2/.nastest_perf")
    src = local_test_dir(ctx) / "rsync_src"
    src.mkdir(exist_ok=True)
    big = local_bigfile(ctx)
    # 组装源目录:1 大文件 + 若干小文件
    link = src / big.name
    if not link.exists():
        try:
            os.link(big, link)
        except OSError:
            shutil.copyfile(big, link)
    for i in range(20):
        (src / f"small_{i}.txt").write_bytes(os.urandom(4096))
    total_mb = sum(f.stat().st_size for f in src.iterdir()) / 1024 / 1024
    ctx.sh(f"mkdir -p {tdir}/rsync_dst")
    argv = ["sshpass", "-p", pw, "rsync", "-a", "--stats",
            "-e", "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
            f"{src}/", f"{user}@{ip}:{tdir}/rsync_dst/"]
    ctx.log(f"$ sshpass -p *** rsync -a --stats {src}/ {user}@{ip}:{tdir}/rsync_dst/")
    nic = device_nic(ctx)
    pre = device_snapshot(ctx, nic)
    t0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        ctx.abort("fail", "rsync 超时")
    dur = time.time() - t0
    _save_cpu(ctx, "rsync_cpu_pct", pre, device_snapshot(ctx, nic))
    ctx.log(p.stdout[-3000:] if p.stdout else "")
    if p.returncode != 0:
        ctx.abort("fail", f"rsync 失败 rc={p.returncode}: {p.stderr.strip()[:300]}")
    mbps = round(total_mb / dur, 1)
    ctx.metric("total_mb", round(total_mb))
    ctx.metric("rsync_mbps", mbps)
    ctx.sh(f"rm -rf {tdir}/rsync_dst")
    return f"rsync {total_mb:.0f}MB → 设备: {mbps}MB/s({dur:.1f}s,含 SSH 加密开销)"


@_impl("s5_smb_small")
def s5_smb_small(ctx: Ctx) -> str:
    host = smb_register(ctx, encrypt=False)
    share = smb_pick_share(ctx, host)
    root = f"\\\\{host}\\{share}\\nastest_small"
    n = 10000
    workers = 4
    try:
        smbclient.mkdir(root)
    except Exception:
        pass
    pool = os.urandom(64 * 1024)
    sizes = [4 * 1024 + (i * 7919) % (60 * 1024) for i in range(n)]
    names = [f"{root}\\f{i}.dat" for i in range(n)]
    errors = []

    def do_create(idx):
        try:
            with smbclient.open_file(names[idx], mode="wb") as f:
                f.write(pool[:sizes[idx]])
        except Exception as e:
            errors.append(f"create {idx}: {e}")

    def do_read(idx):
        try:
            with smbclient.open_file(names[idx], mode="rb") as f:
                f.read()
        except Exception as e:
            errors.append(f"read {idx}: {e}")

    def do_del(idx):
        try:
            smbclient.remove(names[idx])
        except Exception as e:
            errors.append(f"del {idx}: {e}")

    results = {}
    for phase, fn in (("create", do_create), ("read", do_read), ("delete", do_del)):
        if ctx.stop_requested():
            ctx.abort("fail", "用户中止")
        ctx.log(f"阶段 {phase}: {n} 文件 × {workers} 线程 ...")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(fn, range(n)))
        dur = time.time() - t0
        fps = round(n / dur, 1)
        results[phase] = fps
        ctx.metric(f"small_{phase}_files_s", fps)
        ctx.log(f"{phase}: {fps} files/s ({dur:.1f}s)")
        if len(errors) > n * 0.01:
            ctx.abort("fail", f"{phase} 阶段错误过多({len(errors)}),样例: {errors[0] if errors else ''}")
    try:
        smbclient.rmdir(root)
    except Exception:
        pass
    return (f"SMB 小文件 {n} 个(4-64K): 创建 {results['create']} / 读 {results['read']} / "
            f"删除 {results['delete']} files/s(ARM 小核元数据瓶颈专项)")


# ================= S6 并发与混合负载 =================
def _scale_worker_write(root: str, idx: int, mb: int):
    with smbclient.open_file(f"{root}\\sw_{idx}.bin", mode="wb", buffering=_CHUNK) as f:
        for _ in range(mb):
            f.write(_BLOCK)


def _scale_worker_read(root: str, idx: int):
    with smbclient.open_file(f"{root}\\sw_{idx}.bin", mode="rb", buffering=_CHUNK) as f:
        while f.read(_CHUNK):
            pass


def _smb_scale(ctx: Ctx, kind: str) -> str:
    host = smb_register(ctx, encrypt=False)
    share = smb_pick_share(ctx, host)
    root = f"\\\\{host}\\{share}\\nastest_scale"
    try:
        smbclient.mkdir(root)
    except Exception:
        pass
    per_mb = 256
    ns = [1, 2, 4, 8]
    nic = device_nic(ctx)
    curve = []
    try:
        if kind == "read":  # 预写 8 个文件
            ctx.log(f"预写 {max(ns)} 个 {per_mb}MB 文件供并发读 ...")
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(lambda i: _scale_worker_write(root, i, per_mb), range(max(ns))))
        for n in ns:
            if ctx.stop_requested():
                ctx.abort("fail", "用户中止")
            pre = device_snapshot(ctx, nic)
            t0 = time.time()
            with ThreadPoolExecutor(max_workers=n) as ex:
                if kind == "write":
                    list(ex.map(lambda i: _scale_worker_write(root, i, per_mb), range(n)))
                else:
                    list(ex.map(lambda i: _scale_worker_read(root, i), range(n)))
            dur = time.time() - t0
            post = device_snapshot(ctx, nic)
            mbps = round(n * per_mb / dur, 1)
            delta = device_delta(pre, post)
            row = {"n": n, "mbps": mbps, **delta}
            curve.append(row)
            ctx.metric(f"scale_{kind}_n{n}_mbps", mbps)
            ctx.log(f"N={n}: 聚合 {mbps}MB/s ({dur:.1f}s) {delta}")
        peak = max(curve, key=lambda c: c["mbps"])
        ctx.metric(f"scale_{kind}_curve", curve)
        ctx.metric(f"{kind}_inflection_n", peak["n"])
        return (f"并发{('写' if kind == 'write' else '读')}曲线: " +
                ", ".join(f"N{c['n']}={c['mbps']}MB/s" for c in curve) +
                f";拐点 N={peak['n']}(§8:≥8 路优)")
    finally:
        for i in range(max(ns)):
            try:
                smbclient.remove(f"{root}\\sw_{i}.bin")
            except Exception:
                pass
        try:
            smbclient.rmdir(root)
        except Exception:
            pass


@_impl("s6_scale_write")
def s6_scale_write(ctx: Ctx) -> str:
    return _smb_scale(ctx, "write")


@_impl("s6_scale_read")
def s6_scale_read(ctx: Ctx) -> str:
    return _smb_scale(ctx, "read")


@_impl("s6_mixed")
def s6_mixed(ctx: Ctx) -> str:
    host = smb_register(ctx, encrypt=False)
    share = smb_pick_share(ctx, host)
    root = f"\\\\{host}\\{share}\\nastest_mixed"
    try:
        smbclient.mkdir(root)
    except Exception:
        pass
    per_mb = 256
    nic = device_nic(ctx)
    try:
        ctx.log(f"预写 4 个 {per_mb}MB 读文件 ...")
        for i in range(4):
            with smbclient.open_file(f"{root}\\rd_{i}.bin", mode="wb", buffering=_CHUNK) as f:
                for _ in range(per_mb):
                    f.write(_BLOCK)

        def reader(i):
            with smbclient.open_file(f"{root}\\rd_{i}.bin", mode="rb", buffering=_CHUNK) as f:
                while f.read(_CHUNK):
                    pass

        pre = device_snapshot(ctx, nic)
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = [ex.submit(_scale_worker_write, root, 100 + i, per_mb) for i in range(4)]
            futs += [ex.submit(reader, i) for i in range(4)]
            for f in futs:
                f.result()
        dur = time.time() - t0
        post = device_snapshot(ctx, nic)
        r_mbps = w_mbps = round(4 * per_mb / dur, 1)
        delta = device_delta(pre, post)
        ctx.metric("mixed_read_mbps", r_mbps)
        ctx.metric("mixed_write_mbps", w_mbps)
        ctx.metric("mixed", delta)
        return (f"4 读+4 写并发: 各 {r_mbps}MB/s(同时间窗),总吞吐 {r_mbps * 2}MB/s,{delta};"
                f"对照纯读/纯写曲线评估相互干扰")
    finally:
        for i in list(range(4)) + [100, 101, 102, 103]:
            try:
                smbclient.remove(f"{root}\\rd_{i}.bin" if i < 4 else f"{root}\\sw_{i}.bin")
            except Exception:
                pass
        try:
            smbclient.rmdir(root)
        except Exception:
            pass
