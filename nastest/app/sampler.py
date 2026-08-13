"""后台采样器:周期性调用采样函数,结果写入 CSV + 内存,供 soak/并发用例使用。

LIVE_SAMPLERS 注册表供 API 查询运行中用例的实时样本。
"""
import csv
import threading
import time
from pathlib import Path
from typing import Callable

# (run_id, case_id) -> Sampler(运行中)
LIVE_SAMPLERS: dict[tuple[int, str], "Sampler"] = {}
_LOCK = threading.Lock()


def register(run_id: int, case_id: str, sampler: "Sampler") -> None:
    with _LOCK:
        LIVE_SAMPLERS[(run_id, case_id)] = sampler


def unregister(run_id: int, case_id: str) -> None:
    with _LOCK:
        LIVE_SAMPLERS.pop((run_id, case_id), None)


def get_live(run_id: int, case_id: str) -> "Sampler | None":
    with _LOCK:
        return LIVE_SAMPLERS.get((run_id, case_id))


class Sampler:
    """interval 秒调用一次 fn()->dict;写 CSV(追加)与内存列表。"""

    def __init__(self, interval: int, csv_path: Path | str | None,
                 fn: Callable[[], dict]):
        self.interval = max(1, int(interval))
        self.csv_path = Path(csv_path) if csv_path else None
        self.fn = fn
        self.samples: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._csv_fields: list[str] | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="sampler")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                row = {"ts": time.strftime("%F %T"), "t": round(time.time(), 1)}
                row.update(self.fn() or {})
                self.samples.append(row)
                self._append_csv(row)
            except Exception as e:  # 采样失败不中断
                self.samples.append({"ts": time.strftime("%F %T"),
                                     "error": str(e)[:200]})
            self._stop.wait(self.interval)

    def _append_csv(self, row: dict) -> None:
        if self.csv_path is None:
            return
        try:
            write_header = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
            if self._csv_fields is None:
                self._csv_fields = list(row.keys())
            with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=self._csv_fields,
                                   extrasaction="ignore")
                if write_header:
                    w.writeheader()
                w.writerow(row)
        except OSError:
            pass

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        return self.samples

    def tail(self, n: int = 20) -> list[dict]:
        return self.samples[-n:]
