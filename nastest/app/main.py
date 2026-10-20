"""FastAPI Web 后台入口 —— ARM-NAS 极限性能测试框架。启动: python -m app.main"""
import csv
import json
import re
import shutil
import threading
import time

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, evidence, migrate, preflight, reporter, sampler
from .executor import drop_executor, get_executor, has_active_threads, manual_ids
from .raid import S3_SUBS
from .registry import BY_ID, CASES
from .scorer import score_run

db.init_db()
migrate.s3_groups()   # 旧版 S3-01..10 → 子组编号(幂等,无旧数据时直接跳过)

app = FastAPI(title="ARM-NAS 极限性能测试后台")
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))


# ---------- 重启恢复:把残留 running 置为 paused ----------
for _r in db.list_runs():
    # 注册表新增用例后,为旧任务补齐结果行(幂等),新用例显示为未测
    db.ensure_results(_r["id"], [c.id for c in CASES])
    if _r["status"] == "running":
        db.update_run(_r["id"], status="paused",
                      note=(_r.get("note") or "") + "\n[服务重启,任务已暂停,可点击继续]")
    # 服务重启时卡在"运行中"的用例复位为未测,否则该用例永远显示运行中、任务也无法删除
    for _res in db.list_results(_r["id"]):
        if _res["status"] == "running":
            db.set_result(_r["id"], _res["case_id"], status="pending",
                          error="服务重启导致该用例执行被中断,可重新运行")


def _case_meta() -> list[dict]:
    return [{
        "id": c.id, "domain": c.domain, "title": c.title, "mode": c.mode,
        "tier": c.tier, "steps": c.steps, "expected": c.expected,
        "guide": c.guide, "hazard": c.hazard, "deps": c.deps,
        "has_impl": c.impl is not None, "sub": c.sub,
    } for c in CASES]


def _run_state(run_id: int) -> dict:
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "任务不存在")
    run = dict(run)
    safe_cfg = dict(run["config"])
    for k in ("ssh_pass", "smb_pass", "local_sudo_pass"):
        if safe_cfg.get(k):
            safe_cfg[k] = "***"
    run["config"] = safe_cfg
    results = db.list_results(run_id)
    for r in results:
        try:
            r["attachments"] = evidence.list_attachments(run_id, r["case_id"])
        except OSError:
            r["attachments"] = []
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "run": {k: v for k, v in run.items()},
        "results": results,
        "counts": counts,
        "total": len(CASES),
        "manual_done": all(
            (next((r for r in results if r["case_id"] == cid), {}) or {}).get("status")
            in ("pass", "fail", "skipped") for cid in manual_ids()),
        "score": score_run(run_id),
    }


# ================= 页面 =================
@app.get("/", response_class=HTMLResponse)
def page_index(request: Request):
    runs = db.list_runs()
    for r in runs:
        rs = db.list_results(r["id"])
        r["done"] = sum(1 for x in rs if x["status"] in ("pass", "fail", "skipped"))
        r["total"] = len(CASES)
        r["created_str"] = time.strftime("%m-%d %H:%M", time.localtime(r["created_at"])) if r.get("created_at") else ""
    return templates.TemplateResponse("index.html", {"request": request, "runs": runs})


@app.get("/runs/new", response_class=HTMLResponse)
def page_new(request: Request):
    return templates.TemplateResponse(
        "run_new.html", {"request": request, "defaults": config.DEFAULT_RUN_CONFIG})


@app.get("/runs/{run_id}/config", response_class=HTMLResponse)
def page_edit_config(request: Request, run_id: int):
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "任务不存在")
    return templates.TemplateResponse(
        "run_config.html",
        {"request": request, "run_id": run_id, "run_name": run["name"],
         # 旧任务可能缺新增的默认键,用默认值兜底
         "defaults": {**config.DEFAULT_RUN_CONFIG, **run["config"]}})


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def page_run(request: Request, run_id: int):
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "任务不存在")
    return templates.TemplateResponse(
        "run_detail.html",
        {"request": request, "run_id": run_id,
         "cases_json": json.dumps(_case_meta(), ensure_ascii=False),
         "domain_order": config.DOMAIN_ORDER, "domain_names": config.DOMAIN_NAMES})


@app.get("/runs/{run_id}/report", response_class=HTMLResponse)
def page_report(request: Request, run_id: int):
    if db.get_run(run_id) is None:
        raise HTTPException(404, "任务不存在")
    return templates.TemplateResponse("report.html", {"request": request, **reporter.report_context(run_id)})


@app.get("/runs/{run_id}/report.md")
def download_report_md(run_id: int):
    if db.get_run(run_id) is None:
        raise HTTPException(404, "任务不存在")
    p = reporter.generate_markdown(run_id)
    return FileResponse(p, media_type="text/markdown", filename=f"report_run{run_id}.md")


@app.get("/runs/{run_id}/cases/{case_id}/files/{name}")
def evidence_file(run_id: int, case_id: str, name: str):
    text = evidence.read_case_file(run_id, case_id, name)
    if not text:
        raise HTTPException(404, "文件不存在")
    return PlainTextResponse(text)


@app.get("/runs/{run_id}/cases/{case_id}/attachments/{fname}")
def attachment_file(run_id: int, case_id: str, fname: str):
    p = evidence.case_dir(run_id, case_id) / "attachments" / fname
    if not p.exists():
        raise HTTPException(404, "附件不存在")
    return FileResponse(p, filename=fname)


@app.get("/runs/{run_id}/charts/{fname}")
def chart_file(run_id: int, fname: str):
    if not re.fullmatch(r"[\w.-]+\.png", fname):
        raise HTTPException(404, "图表不存在")
    p = evidence.run_dir(run_id) / "charts" / fname
    if not p.exists():
        raise HTTPException(404, "图表不存在")
    return FileResponse(p, media_type="image/png")


# ================= API =================
_INT_CFG_KEYS = ("disk_count", "ssh_port", "ftp_port", "rounds", "fio_runtime",
                 "warmup_seconds", "soak_minutes", "soak_sample_interval",
                 "nic_link_mbps", "smb_bigfile_mb")


def _normalize_config(cfg: dict) -> dict:
    """配置类型与字符串规整,新建/修改任务共用。"""
    for k in ("volume_path", "device_test_dir"):
        cfg[k] = str(cfg.get(k) or "").strip()
    for k in _INT_CFG_KEYS:
        try:
            cfg[k] = int(cfg.get(k, 0))
        except (TypeError, ValueError):
            pass
    ports = cfg.get("iperf_ports", [5201])
    if isinstance(ports, str):
        ports = ports.split(",")
    try:
        cfg["iperf_ports"] = [int(p) for p in ports]
    except (TypeError, ValueError):
        cfg["iperf_ports"] = [5201, 5202, 5203, 5204]
    return cfg


@app.post("/api/runs")
async def api_create_run(request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip() or f"性能测试 {time.strftime('%m-%d %H:%M')}"
    cfg = dict(config.DEFAULT_RUN_CONFIG)
    cfg.update(body.get("config") or {})
    cfg["protocols"] = body.get("protocols") or cfg["protocols"]
    cfg = _normalize_config(cfg)
    # 存储卷:留空=运行/预检时自动探测;测试目录未填则跟随卷推导
    if cfg["volume_path"] and not cfg["device_test_dir"]:
        cfg["device_test_dir"] = cfg["volume_path"].rstrip("/") + "/.nastest_perf"
    run_id = db.create_run(name, cfg)
    db.ensure_results(run_id, [c.id for c in CASES])
    evidence.save_run_meta(run_id, db.get_run(run_id))
    return {"run_id": run_id}


@app.post("/api/runs/{run_id}/config")
async def api_update_config(run_id: int, request: Request):
    """修改任务配置(可连带改名),对之后执行的用例生效,不影响已完成结果。

    有用例正在执行或任务处于运行态时拒绝;保存后为注册表新增的用例补齐结果行,
    并刷新证据目录 run.json。测试目录若为空或仍是旧卷的派生值,则跟随新卷重新推导。
    """
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "任务不存在")
    if run["status"] == "running" or any(
            r["status"] == "running" for r in db.list_results(run_id)):
        raise HTTPException(409, "任务正在运行,请先暂停或中止后再修改配置")
    body = await request.json()
    old_cfg = run["config"]
    cfg = dict(old_cfg)
    cfg.update(body.get("config") or {})
    cfg["protocols"] = body.get("protocols") or cfg["protocols"]
    cfg = _normalize_config(cfg)
    old_vol = str(old_cfg.get("volume_path") or "").strip().rstrip("/")
    if cfg["volume_path"] and (
            not cfg["device_test_dir"]
            or (old_vol and cfg["device_test_dir"] == old_vol + "/.nastest_perf")):
        cfg["device_test_dir"] = cfg["volume_path"].rstrip("/") + "/.nastest_perf"
    fields = {"config": cfg}
    name = (body.get("name") or "").strip()
    if name:
        fields["name"] = name
    db.update_run(run_id, **fields)
    db.ensure_results(run_id, [c.id for c in CASES])   # 注册表新增用例补行(幂等)
    evidence.save_run_meta(run_id, db.get_run(run_id))
    return {"ok": True, "run_id": run_id}


@app.get("/api/runs")
def api_list_runs():
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def api_run_state(run_id: int):
    return _run_state(run_id)


@app.delete("/api/runs/{run_id}")
def api_delete_run(run_id: int):
    """删除任务:DB 记录 + 证据目录一并清理。

    运行中的任务(有用例在跑或调度线程存活)不允许直接删除,
    需先中止并等待当前用例退出。
    """
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "任务不存在")
    if has_active_threads(run_id):
        raise HTTPException(
            409, "任务仍在运行(有用例正在执行)。请先中止任务并等待当前用例退出后再删除")
    drop_executor(run_id)
    db.delete_run(run_id)
    ev_dir = config.EVIDENCE_DIR / f"run_{run_id}"
    if ev_dir.exists():
        shutil.rmtree(ev_dir, ignore_errors=True)
    return {"ok": True}


@app.post("/api/runs/{run_id}/preflight/check")
def api_preflight_check(run_id: int):
    return preflight.check(run_id)


@app.post("/api/runs/{run_id}/preflight/install")
async def api_preflight_install(run_id: int, request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    packages = body.get("packages")
    threading.Thread(target=preflight.install_device_packages,
                     args=(run_id, packages), daemon=True).start()
    return {"ok": True}


@app.post("/api/runs/{run_id}/preflight/install-local")
async def api_preflight_install_local(run_id: int, request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    packages = body.get("packages")
    threading.Thread(target=preflight.install_local_packages,
                     args=(run_id, packages), daemon=True).start()
    return {"ok": True}


@app.post("/api/runs/{run_id}/start")
def api_start(run_id: int):
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404)
    if run["status"] == "paused":
        get_executor(run_id).resume()
    else:
        get_executor(run_id).start()
    return {"ok": True}


@app.post("/api/runs/{run_id}/pause")
def api_pause(run_id: int):
    get_executor(run_id).pause()
    return {"ok": True}


@app.post("/api/runs/{run_id}/abort")
def api_abort(run_id: int):
    get_executor(run_id).abort()
    return {"ok": True}


@app.post("/api/runs/{run_id}/cases/{case_id}/run")
async def api_run_case(run_id: int, case_id: str, request: Request):
    case = BY_ID.get(case_id)
    if case is None:
        raise HTTPException(404, "用例不存在")
    if case.impl is None:
        raise HTTPException(400, "该用例为纯人工用例,请按指导操作后提交判定")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    if case.hazard == "critical" and body.get("confirm") != case.id:
        raise HTTPException(466, "高危用例:需要输入用例编号确认")
    res = db.get_result(run_id, case_id)
    if res and res["status"] == "running":
        raise HTTPException(409, "该用例正在运行")
    get_executor(run_id).run_case(case_id)
    return {"ok": True}


@app.post("/api/runs/{run_id}/domains/{domain}/run")
async def api_run_domain(run_id: int, domain: str, request: Request):
    """域顺序运行:依次执行该域所有可执行子测试(跳过运行中/高危/纯人工项)。

    S3 域按子组运行:body 传 {"sub": "basic"|"RAID0"|"RAID1"|"RAID5"|"RAID6"|"RAID10"},
    与人工切换后的阵列级别对应;执行前各用例自动校验阵列级别。
    """
    if domain not in config.DOMAIN_ORDER:
        raise HTTPException(404, "域不存在")
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "任务不存在")
    sub = None
    try:
        body = await request.json()
    except Exception:
        body = {}
    if body.get("sub"):
        if domain != "S3":
            raise HTTPException(400, "仅 S3 域支持子组运行")
        if body["sub"] not in S3_SUBS:
            raise HTTPException(400, f"未知子组: {body['sub']}")
        sub = body["sub"]
    ex = get_executor(run_id)
    if run["status"] == "paused":
        if ex.thread and ex.thread.is_alive():
            raise HTTPException(409, "任务处于暂停状态,请先恢复后再运行")
        ex._pause.clear()   # 仅有暂停标记、无运行中的自动阶段 → 直接继续
    queued, skipped = ex.run_domain(domain, sub)
    return {"ok": True, "queued": queued, "skipped": skipped}


@app.post("/api/runs/{run_id}/cases/{case_id}/verdict")
async def api_verdict(run_id: int, case_id: str, request: Request):
    body = await request.json()
    verdict, note = body.get("verdict"), (body.get("note") or "").strip()
    try:
        get_executor(run_id).submit_verdict(case_id, verdict, note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/runs/{run_id}/cases/{case_id}/metrics")
async def api_manual_metrics(run_id: int, case_id: str, request: Request):
    """人工录入数值指标(如 S9-01 功率计瓦数)。合并进该用例 metrics。"""
    if BY_ID.get(case_id) is None:
        raise HTTPException(404, "用例不存在")
    if db.get_run(run_id) is None:
        raise HTTPException(404, "任务不存在")
    body = await request.json()
    metrics = body.get("metrics") or {}
    if not isinstance(metrics, dict) or not metrics:
        raise HTTPException(400, "metrics 不能为空")
    for k, v in metrics.items():
        if not isinstance(v, (int, float, str)) or isinstance(v, bool):
            raise HTTPException(400, f"指标 {k} 类型不支持(仅数值/字符串)")
    res = db.get_result(run_id, case_id)
    merged = {**(res.get("metrics") or {} if res else {}), **metrics}
    db.set_result(run_id, case_id, metrics=merged)
    return {"ok": True, "metrics": merged}


@app.get("/api/runs/{run_id}/cases/{case_id}/samples")
def api_samples(run_id: int, case_id: str, n: int = 30):
    """实时采样:运行中的 Sampler 优先;否则回读 soak.csv 末尾。"""
    live = sampler.get_live(run_id, case_id)
    if live is not None:
        return {"live": True, "interval": live.interval, "samples": live.tail(n)}
    p = evidence.case_dir(run_id, case_id) / "soak.csv"
    if p.exists():
        rows = []
        with open(p, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)
        return {"live": False, "interval": None, "samples": rows[-n:]}
    return {"live": False, "interval": None, "samples": []}


@app.post("/api/runs/{run_id}/cases/{case_id}/attachments")
async def api_upload(run_id: int, case_id: str, file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "附件超过 20MB")
    p = evidence.save_attachment(run_id, case_id, file.filename or "attachment.bin", data)
    return {"ok": True, "name": p.name}


@app.post("/api/runs/{run_id}/report")
def api_gen_report(run_id: int):
    if db.get_run(run_id) is None:
        raise HTTPException(404)
    p = reporter.generate_markdown(run_id)
    db.update_run(run_id, phase="report", status="completed", completed_at=time.time())
    return {"ok": True, "path": str(p)}


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="info")


if __name__ == "__main__":
    main()
