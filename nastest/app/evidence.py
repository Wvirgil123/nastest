"""证据目录管理:每个 run 一个目录,每条用例一个子目录。"""
import json
import re
import time
from pathlib import Path

from . import config


def _safe(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name)


def run_dir(run_id: int) -> Path:
    p = config.EVIDENCE_DIR / f"run_{run_id}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def case_dir(run_id: int, case_id: str) -> Path:
    p = run_dir(run_id) / "cases" / _safe(case_id)
    (p / "attachments").mkdir(parents=True, exist_ok=True)
    return p


def save_text(run_id: int, case_id: str, name: str, text: str) -> Path:
    p = case_dir(run_id, case_id) / _safe(name)
    p.write_text(text, encoding="utf-8")
    return p


def append_text(run_id: int, case_id: str, name: str, text: str) -> None:
    p = case_dir(run_id, case_id) / _safe(name)
    with p.open("a", encoding="utf-8") as f:
        f.write(text)


def save_json(run_id: int, case_id: str, name: str, obj) -> Path:
    p = case_dir(run_id, case_id) / _safe(name)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def save_attachment(run_id: int, case_id: str, filename: str, data: bytes) -> Path:
    p = case_dir(run_id, case_id) / "attachments" / _safe(filename)
    p.write_bytes(data)
    return p


def list_attachments(run_id: int, case_id: str) -> list[str]:
    p = case_dir(run_id, case_id) / "attachments"
    return sorted(f.name for f in p.iterdir() if f.is_file())


def read_case_file(run_id: int, case_id: str, name: str) -> str:
    p = case_dir(run_id, case_id) / _safe(name)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def save_run_meta(run_id: int, run: dict) -> None:
    p = run_dir(run_id) / "run.json"
    cfg = dict(run["config"])
    for secret in ("ssh_pass", "smb_pass", "local_sudo_pass"):
        if cfg.get(secret):
            cfg[secret] = "***"
    p.write_text(json.dumps(
        {"run_id": run["id"], "name": run["name"], "config": cfg,
         "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")},
        ensure_ascii=False, indent=2), encoding="utf-8")
