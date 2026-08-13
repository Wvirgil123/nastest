"""SSH 执行层:paramiko 封装。

- sudo 提权:echo <password> | sudo -S -p '' bash -c '...'
- 超时、输出截断(头尾保留)、断连自动重连
- seeed 用户无 home 目录:所有命令强制 cd 到指定工作目录
"""
import shlex
import socket
import time
from dataclasses import dataclass, field

import paramiko

MAX_OUTPUT = 2 * 1024 * 1024  # 单次命令输出上限 2MB,超出保留头尾


class SSHError(RuntimeError):
    pass


@dataclass
class ExecResult:
    rc: int
    out: str = ""
    err: str = ""
    cmd: str = ""
    timeout: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def summary(self, limit: int = 2000) -> str:
        return self.out[-limit:] if self.out else self.err[-limit:]


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    keep = MAX_OUTPUT // 2
    return text[:keep] + f"\n[...中间省略 {len(text) - MAX_OUTPUT} 字节...]\n" + text[-keep:]


class SSH:
    def __init__(self, host: str, user: str, password: str, port: int = 22):
        self.host, self.user, self.password, self.port = host, user, password, port
        self._client: paramiko.SSHClient | None = None

    # ---------- 连接管理 ----------
    def connect(self, retries: int = 3) -> None:
        last_err: Exception | None = None
        for i in range(retries):
            try:
                cli = paramiko.SSHClient()
                cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                cli.connect(
                    self.host, port=self.port, username=self.user, password=self.password,
                    timeout=10, allow_agent=False, look_for_keys=False,
                    banner_timeout=15, auth_timeout=15,
                )
                tr = cli.get_transport()
                if tr is not None:
                    tr.set_keepalive(30)
                self._client = cli
                return
            except (paramiko.SSHException, socket.error, OSError) as e:
                last_err = e
                time.sleep(2 * (i + 1))
        raise SSHError(f"SSH 连接 {self.user}@{self.host}:{self.port} 失败: {last_err}")

    def _ensure(self) -> paramiko.SSHClient:
        if self._client is not None:
            tr = self._client.get_transport()
            if tr is not None and tr.is_active():
                return self._client
        self.connect()
        assert self._client is not None
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    def ping(self) -> bool:
        r = self.run("echo pong", timeout=15)
        return r.ok and "pong" in r.out

    # ---------- 命令执行 ----------
    def run(self, cmd: str, timeout: int = 120, sudo: bool = False,
            cwd: str | None = "/tmp") -> ExecResult:
        """执行命令。sudo=True 时用密码走 sudo -S。cwd 默认 /tmp(seeed 无 home)。"""
        full = cmd
        if cwd:
            full = f"cd {shlex.quote(cwd)} 2>/dev/null || cd /; {full}"
        if sudo:
            full = f"echo {shlex.quote(self.password)} | sudo -S -p '' bash -c {shlex.quote(full)}"
        full = f"export LC_ALL=C.UTF-8 LANG=C.UTF-8; export PATH=$PATH:/usr/sbin:/sbin; {full}"

        start = time.time()
        try:
            cli = self._ensure()
            chan = cli.get_transport().open_session()
            chan.settimeout(timeout)
            chan.exec_command(full)
            # 读到通道 EOF(recv 返回 b'')。不能用 exit_status_ready()+缓冲区空判断:
            # 末尾一次性输出(fio --output=json)的数据包可能在 exit-status 之后才到达,
            # 轮询方式会提前 break 导致丢全部输出(实测 rc=0 但 out 为空)。
            out_b, err_b = b"", b""
            MAX_RAW = 50 * 1024 * 1024
            while len(out_b) < MAX_RAW:
                chunk = chan.recv(65536)
                if not chunk:
                    break
                out_b += chunk
            while len(err_b) < MAX_RAW:
                chunk = chan.recv_stderr(65536)
                if not chunk:
                    break
                err_b += chunk
            rc = chan.recv_exit_status()
            chan.close()
            # sudo 的密码提示可能混入 stderr,去掉
            err_s = _truncate(err_b.decode("utf-8", "replace"))
            err_s = "\n".join(
                ln for ln in err_s.splitlines()
                if "[sudo] password for" not in ln
                and "Could not chdir to home directory" not in ln
            )
            return ExecResult(
                rc=rc, out=_truncate(out_b.decode("utf-8", "replace")),
                err=err_s, cmd=cmd, duration_ms=int((time.time() - start) * 1000),
            )
        except socket.timeout:
            return ExecResult(rc=-1, cmd=cmd, timeout=True,
                              err=f"命令超时({timeout}s)", duration_ms=int((time.time() - start) * 1000))
        except (paramiko.SSHException, socket.error, OSError) as e:
            # 连接级错误:重置连接,抛出由上层决策
            self.close()
            raise SSHError(f"SSH 执行异常: {e}") from e

    def sudo_ok(self) -> bool:
        return self.run("true", sudo=True, timeout=20).ok
