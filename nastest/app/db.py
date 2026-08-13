"""SQLite 数据层:runs / results 两张表。线程安全(每次调用独立连接)。"""
import json
import os
import sqlite3
import time
from pathlib import Path

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  config_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',   -- pending/running/paused/completed/aborted
  phase TEXT NOT NULL DEFAULT 'created',    -- created/preflight/auto/manual/report
  preflight_json TEXT DEFAULT '{}',
  note TEXT DEFAULT '',
  created_at REAL, completed_at REAL
);
CREATE TABLE IF NOT EXISTS results(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  case_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  actual TEXT DEFAULT '',
  metrics_json TEXT DEFAULT '{}',
  evidence_dir TEXT DEFAULT '',
  started_at REAL, completed_at REAL, duration_ms INTEGER,
  verdict_by TEXT DEFAULT '',               -- auto / manual
  error TEXT DEFAULT '',
  UNIQUE(run_id, case_id)
);
"""


def _conn() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
    try:
        os.chmod(config.DB_PATH, 0o600)  # 配置内含密码,限制读权限
    except OSError:
        pass


# ---------- runs ----------
def create_run(name: str, cfg: dict) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO runs(name,config_json,status,phase,created_at) VALUES(?,?,?,?,?)",
            (name, json.dumps(cfg, ensure_ascii=False), "pending", "created", time.time()),
        )
        return cur.lastrowid


def get_run(run_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    return _run_dict(row) if row else None


def list_runs() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
    return [_run_dict(r) for r in rows]


def _run_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["config"] = json.loads(d.pop("config_json"))
    d["preflight"] = json.loads(d.pop("preflight_json") or "{}")
    return d


def update_run(run_id: int, **fields) -> None:
    if not fields:
        return
    keys, vals = [], []
    for k, v in fields.items():
        if k in ("config", "preflight"):
            k, v = k + "_json", json.dumps(v, ensure_ascii=False)
        keys.append(f"{k}=?")
        vals.append(v)
    vals.append(run_id)
    with _conn() as c:
        c.execute(f"UPDATE runs SET {','.join(keys)} WHERE id=?", vals)


def delete_run(run_id: int) -> None:
    with _conn() as c:
        c.execute("DELETE FROM results WHERE run_id=?", (run_id,))
        c.execute("DELETE FROM runs WHERE id=?", (run_id,))


# ---------- results ----------
def ensure_results(run_id: int, case_ids: list[str]) -> None:
    """为 run 初始化全部用例行(幂等)。"""
    with _conn() as c:
        for cid in case_ids:
            c.execute(
                "INSERT OR IGNORE INTO results(run_id,case_id,status) VALUES(?,?, 'pending')",
                (run_id, cid),
            )


def list_results(run_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM results WHERE run_id=? ORDER BY id", (run_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
        out.append(d)
    return out


def delete_result(run_id: int, case_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM results WHERE run_id=? AND case_id=?", (run_id, case_id))


def get_result(run_id: int, case_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM results WHERE run_id=? AND case_id=?", (run_id, case_id)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["metrics"] = json.loads(d.pop("metrics_json") or "{}")
    return d


def set_result(run_id: int, case_id: str, **fields) -> None:
    """更新用例结果;metrics 字段自动 JSON 序列化。"""
    if not fields:
        return
    keys, vals = [], []
    for k, v in fields.items():
        if k == "metrics":
            k, v = "metrics_json", json.dumps(v, ensure_ascii=False)
        keys.append(f"{k}=?")
        vals.append(v)
    vals += [run_id, case_id]
    with _conn() as c:
        c.execute(
            f"UPDATE results SET {','.join(keys)} WHERE run_id=? AND case_id=?", vals
        )


def case_started(run_id: int, case_id: str, evidence_dir: str) -> None:
    set_result(
        run_id, case_id,
        status="running", started_at=time.time(), completed_at=None,
        duration_ms=None, error="", evidence_dir=evidence_dir,
    )


def case_finished(run_id: int, case_id: str, status: str, actual: str,
                  metrics: dict | None = None, error: str = "",
                  verdict_by: str = "auto", started_at: float | None = None) -> None:
    now = time.time()
    dur = int((now - started_at) * 1000) if started_at else None
    set_result(
        run_id, case_id,
        status=status, actual=actual, metrics=metrics or {},
        completed_at=now, duration_ms=dur, error=error, verdict_by=verdict_by,
    )
