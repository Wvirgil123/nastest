"""执行引擎:自动阶段调度 + 单用例执行 + 状态持久化。

- 自动阶段按 config.DOMAIN_ORDER 的域顺序执行 mode=auto 的用例;
- 人工用例统一放到最后(manual 阶段),支持"点击运行自动校验"与人工提交判定;
- 所有结果逐条落库,重启后可 resume。
"""
import threading
import time
import traceback

from . import config, db, evidence
from .caseimpl import IMPL, CaseAbort, Ctx
from .ctx import resolve_storage_cfg
from .raid import RAID_SUBS
from .registry import BY_ID, CASES, MANUAL_IDS
from .sshc import SSH, SSHError

RUNNERS: dict[int, "Executor"] = {}
_RUNNERS_LOCK = threading.Lock()


def get_executor(run_id: int) -> "Executor":
    with _RUNNERS_LOCK:
        ex = RUNNERS.get(run_id)
        if ex is None:
            ex = Executor(run_id)
            RUNNERS[run_id] = ex
        return ex


def has_active_threads(run_id: int) -> bool:
    """任务是否仍有运行中的用例或存活的调度线程(此时不允许删除任务)。"""
    if any(r["status"] == "running" for r in db.list_results(run_id)):
        return True
    with _RUNNERS_LOCK:
        ex = RUNNERS.get(run_id)
    return ex is not None and ex.thread is not None and ex.thread.is_alive()


def drop_executor(run_id: int) -> None:
    """删除任务后清空调度器引用。"""
    with _RUNNERS_LOCK:
        RUNNERS.pop(run_id, None)


def ordered_auto_ids() -> list[str]:
    """自动阶段执行顺序。S3 的 RAID 子组(RAID0/1/5/6/10)不进入自动阶段——
    需人工切换阵列后在对应子组点「顺序运行」,防止阵列级别与用例组不符。"""
    ids: list[str] = []
    for dom in config.DOMAIN_ORDER:
        ids += [c.id for c in CASES
                if c.domain == dom and c.mode == "auto" and c.sub not in RAID_SUBS]
    return ids


def manual_ids() -> list[str]:
    ids: list[str] = []
    for dom in config.DOMAIN_ORDER:
        ids += [c.id for c in CASES if c.domain == dom and c.mode == "manual"]
    return ids


class Executor:
    def __init__(self, run_id: int):
        self.run_id = run_id
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._case_lock = threading.Lock()
        self.thread: threading.Thread | None = None

    # ================= 控制 =================
    def start(self) -> None:
        """启动自动阶段(幂等)。"""
        run = db.get_run(self.run_id)
        if run["status"] == "running" and self.thread and self.thread.is_alive():
            return
        self._stop.clear()
        self._pause.clear()
        db.update_run(self.run_id, status="running")
        self.thread = threading.Thread(target=self._auto_phase, daemon=True,
                                       name=f"run-{self.run_id}")
        self.thread.start()

    def pause(self) -> None:
        self._pause.set()
        db.update_run(self.run_id, status="paused")

    def resume(self) -> None:
        self._pause.clear()
        self.start()

    def abort(self) -> None:
        self._stop.set()
        self._pause.clear()
        db.update_run(self.run_id, status="aborted", note=(db.get_run(self.run_id) or {}).get("note", "") + "\n[已中止]")

    def _interrupted(self) -> bool:
        return self._stop.is_set()

    def _wait_if_paused(self) -> bool:
        """暂停期间挂等待;返回 False 表示被中止。"""
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(1)
        return not self._stop.is_set()

    # ================= 自动阶段 =================
    def _auto_phase(self) -> None:
        run = db.get_run(self.run_id)
        db.update_run(self.run_id, phase="auto")
        evidence.save_run_meta(self.run_id, run)
        db.ensure_results(self.run_id, [c.id for c in CASES])

        for cid in ordered_auto_ids():
            if not self._wait_if_paused():
                return
            res = db.get_result(self.run_id, cid)
            if res and res["status"] in ("pass", "fail", "skipped"):
                continue
            # 依赖检查
            case = BY_ID[cid]
            dep_bad = [d for d in case.deps
                       if (db.get_result(self.run_id, d) or {}).get("status") != "pass"]
            if dep_bad:
                db.set_result(self.run_id, cid, status="blocked",
                              actual=f"依赖未完成: {', '.join(dep_bad)}")
                continue
            self.run_case_sync(cid)

        # 进入人工阶段
        if not self._interrupted():
            for cid in manual_ids():
                res = db.get_result(self.run_id, cid)
                if res and res["status"] == "pending":
                    db.set_result(self.run_id, cid, status="manual_pending")
            db.update_run(self.run_id, phase="manual")

    # ================= 用例执行 =================
    def run_case(self, case_id: str, in_thread: bool = True) -> None:
        """供 API 调用:执行单条用例(自动校验)。"""
        if in_thread:
            threading.Thread(target=self.run_case_sync, args=(case_id,),
                             daemon=True, name=f"case-{self.run_id}-{case_id}").start()
        else:
            self.run_case_sync(case_id)

    # ================= 域/子组顺序运行(每域「运行」按钮;S3 按子组) =================
    def domain_plan(self, domain: str, sub: str | None = None
                    ) -> tuple[list[str], list[dict]]:
        """规划某域的顺序运行。返回 (queued_ids, skipped[{id,title,reason}])。

        sub 用于 S3 子组(basic/RAID0/1/5/6/10),只排该子组的用例。
        跳过:正在运行的用例、hazard=critical(必须单独输入编号确认)、
        纯人工用例(无 impl,按指导提交判定)。
        """
        queued: list[str] = []
        skipped: list[dict] = []
        for c in CASES:
            if c.domain != domain:
                continue
            if sub is not None and c.sub != sub:
                continue
            res = db.get_result(self.run_id, c.id)
            if res and res["status"] == "running":
                skipped.append({"id": c.id, "title": c.title, "reason": "正在运行"})
            elif c.hazard == "critical":
                skipped.append({"id": c.id, "title": c.title,
                                "reason": "高危用例,需单独运行并输入编号确认"})
            elif c.impl is None:
                skipped.append({"id": c.id, "title": c.title,
                                "reason": "纯人工项,请按操作指导完成后提交判定"})
            else:
                queued.append(c.id)
        return queued, skipped

    def run_domain(self, domain: str, sub: str | None = None
                   ) -> tuple[list[str], list[dict]]:
        """后台线程依次执行域(或 S3 子组)内所有可执行子测试。返回 (queued, skipped)。"""
        queued, skipped = self.domain_plan(domain, sub)
        if not queued:
            return queued, skipped
        run = db.get_run(self.run_id)
        if run and run["status"] in ("pending", "paused"):
            db.update_run(self.run_id, status="running")
        self._stop.clear()
        self._pause.clear()
        threading.Thread(target=self._run_domain_sync, args=(queued,),
                         daemon=True, name=f"domain-{self.run_id}-{domain}").start()
        return queued, skipped

    def _run_domain_sync(self, ids: list[str]) -> None:
        db.ensure_results(self.run_id, ids)
        for cid in ids:
            if self._interrupted():
                break
            if not self._wait_if_paused():
                break
            res = db.get_result(self.run_id, cid)
            if res and res["status"] == "running":
                continue
            case = BY_ID[cid]
            dep_bad = [d for d in case.deps
                       if (db.get_result(self.run_id, d) or {}).get("status") != "pass"]
            if dep_bad:
                db.set_result(self.run_id, cid, status="blocked",
                              actual=f"依赖未完成: {', '.join(dep_bad)}")
                continue
            self.run_case_sync(cid)

    def run_case_sync(self, case_id: str) -> tuple[str, str]:
        """执行一条用例并落库。返回 (status, message)。"""
        case = BY_ID[case_id]
        run = db.get_run(self.run_id)
        cfg = run["config"]

        with self._case_lock:
            db.ensure_results(self.run_id, [case_id])
            if case.impl is None:
                msg = "纯人工用例:请按操作指导完成,并上传证据后提交判定"
                db.set_result(self.run_id, case_id, status="manual_pending", actual=msg)
                return "manual_pending", msg

            db.case_started(self.run_id, case_id,
                            str(evidence.case_dir(self.run_id, case_id)))
            started = time.time()

            def log_fn(text: str) -> None:
                evidence.append_text(self.run_id, case_id, "stdout.log", text)

            log_fn(f"========== {case_id} {case.title} 开始 {time.strftime('%F %T')} ==========\n")
            status, msg = "fail", ""
            metrics: dict = {}
            ssh: SSH | None = None
            try:
                ssh = SSH(cfg["device_ip"], cfg.get("ssh_user", "seeed"),
                          cfg.get("ssh_pass", ""), int(cfg.get("ssh_port", 22)))
                ssh.connect(retries=2)
                ctx = Ctx(self.run_id, cfg, ssh, log_fn, case_id=case_id,
                          stop_fn=self._interrupted)
                resolve_storage_cfg(ctx)   # volume_path 留空时自动探测并持久化
                try:
                    ret = IMPL[case.impl](ctx)
                    status, msg = "pass", ret or "通过"
                except CaseAbort as a:
                    status, msg = a.status, a.msg
                except Exception:
                    tb = traceback.format_exc()
                    log_fn(tb)
                    status, msg = "fail", f"用例执行异常: {tb.splitlines()[-1] if tb else '未知'}"
                metrics = ctx.metrics
            except SSHError as e:
                status, msg = "fail", f"无法连接设备: {e}"
                log_fn(msg + "\n")
            except Exception:
                tb = traceback.format_exc()
                log_fn(tb)
                status, msg = "fail", f"用例初始化异常: {tb.splitlines()[-1]}"
            finally:
                if ssh is not None:
                    ssh.close()

            evidence.save_json(self.run_id, case_id, "metrics.json", metrics)
            log_fn(f"========== 结果: {status} | {msg}\n")
            verdict_by = "manual" if case.mode == "manual" else "auto"
            db.case_finished(self.run_id, case_id, status, msg, metrics,
                             error="" if status in ("pass", "skipped") else msg[:2000],
                             verdict_by=verdict_by, started_at=started)
            return status, msg

    # ================= 人工判定 =================
    def submit_verdict(self, case_id: str, verdict: str, note: str = "") -> None:
        if verdict not in ("pass", "fail", "skipped"):
            raise ValueError(f"非法判定: {verdict}")
        res = db.get_result(self.run_id, case_id) or {}
        prev_actual = res.get("actual", "")
        final = (note or "").strip()
        db.set_result(
            self.run_id, case_id,
            status=verdict,
            actual=(prev_actual + ("\n" if prev_actual and final else "") +
                    f"[人工判定 {time.strftime('%F %T')}] {final}").strip(),
            verdict_by="manual",
            completed_at=time.time(),
        )

    def check_manual_done(self) -> bool:
        """人工用例是否全部有终态判定。"""
        for cid in manual_ids():
            res = db.get_result(self.run_id, cid)
            if not res or res["status"] not in ("pass", "fail", "skipped"):
                return False
        return True
