"""报告图表生成 —— 可选增强(缺 matplotlib 时自动跳过,不影响报告生成)。

数据取自与 reporter 表格相同的 results 指标,输出到
data/evidence/run_<id>/charts/*.png;report.md 以相对路径引用,
HTML 报告经 /runs/<id>/charts/<fname> 路由提供。

图表清单(与 docs 分析图表一致):
  01_raid_seq    各 RAID 级别顺序吞吐对比        (§5.3)
  02_raid_iops   各 RAID 级别 4K 随机 IOPS 对比  (§5.3)
  03_bottleneck  瓶颈逐层衰减:线速→应用可用带宽 (§5.5 对比链)
  04_scale       并发扩展曲线(读/写聚合吞吐)    (§5.6)
  05_protocols   文件协议读写吞吐横向对比        (§5.5)
  06_crypto      加密算法性能对比(openssl speed)(§5.1 / §8 AES 红旗)
  07_raw_vs_raid 裸盘并发与各 RAID 级别顺序读写对比 (§5.3)
  08_raid_seq_1job 各 RAID 级别单流顺序吞吐对比     (§5.3)
"""
import json

from . import evidence, raid

RAID_ORDER = ["RAID0", "RAID1", "RAID5", "RAID6", "RAID10"]

CHART_FILES = {
    "raid_seq": "01_raid_seq.png",
    "raid_iops": "02_raid_iops.png",
    "bottleneck": "03_bottleneck.png",
    "scale": "04_scale.png",
    "protocols": "05_protocols.png",
    "crypto": "06_crypto.png",
    "raw_vs_raid": "07_raw_vs_raid.png",
    "raid_seq_1job": "08_raid_seq_1job.png",
}

# 调色板(色盲安全、专业克制)
C_BLUE, C_ORANGE, C_GREEN, C_RED, C_PURPLE = "#2E6FB5", "#E07B39", "#3A9D5D", "#D64545", "#7B5EA7"
C_GRAY, C_BG, C_GRID, C_TEXT, C_MUTED = "#9AA3AF", "#FFFFFF", "#E8ECEF", "#2B3038", "#6B7480"


def available() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except ImportError:
        return False


def _m(results: dict, case_id: str, key: str):
    r = results.get(case_id) or {}
    v = (r.get("metrics") or {}).get(key)
    return v if isinstance(v, (int, float)) else None


def _setup_pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.font_manager as fm
    fm.fontManager.addfont = getattr(fm.fontManager, "addfont", lambda *a: None)
    plt.rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "Noto Sans CJK SC",
                                       "WenQuanYi Micro Hei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def _style_ax(ax):
    ax.set_facecolor(C_BG)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(C_GRID)
    ax.spines["bottom"].set_color(C_GRID)
    ax.tick_params(colors=C_MUTED, labelsize=10)
    ax.yaxis.grid(True, color=C_GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)


def _bar_labels(ax, bars, fmt="{:.1f}", offset=2, fontsize=9):
    for b in bars:
        h = b.get_height()
        ax.text(b.get_x() + b.get_width() / 2, h + offset, fmt.format(h),
                ha="center", va="bottom", fontsize=fontsize, color=C_TEXT, fontweight="bold")


def _title(ax, text):
    ax.set_title(text, fontsize=13, color=C_TEXT, fontweight="bold", pad=12)


# ---------------- 数据抽取 ----------------
def _raid_series(results: dict) -> dict:
    """{RAID级别: {seq_read, seq_write, rand_read, rand_write}},未测为 None。"""
    out = {}
    for lv in RAID_ORDER:
        vals = {
            "seq_read": _m(results, raid.level_case(lv, "01"), "seq_read_r_mbps"),
            "seq_write": _m(results, raid.level_case(lv, "02"), "seq_write_w_mbps"),
            "rand_read": _m(results, raid.level_case(lv, "03"), "rand_read_r_iops"),
            "rand_write": _m(results, raid.level_case(lv, "04"), "rand_write_w_iops"),
        }
        if any(v is not None for v in vals.values()):
            out[lv] = vals
    return out


def _raid_seq_1job_series(results: dict) -> dict:
    """{RAID级别: {seq_read, seq_write}} 单流 1M / QD32 × 1job(S3-<级别>-09/10)。

    与 _raid_series(01/02 多流)对照:机械盘多 job 顺序读磁头交插吞吐腰斩,
    单流反映 RAID 真实顺序带宽。未测为 None。
    """
    out = {}
    for lv in RAID_ORDER:
        vals = {
            "seq_read": _m(results, raid.level_case(lv, "09"), "seq_read_1job_r_mbps"),
            "seq_write": _m(results, raid.level_case(lv, "10"), "seq_write_1job_w_mbps"),
        }
        if any(v is not None for v in vals.values()):
            out[lv] = vals
    return out


def _bottleneck_layers(results: dict, cfg: dict):
    """瓶颈逐层衰减:[(层名, MB/s)] 降序 + 线速 MB/s。"""
    line_mb = int(cfg.get("nic_link_mbps", 1000)) / 8
    # RAID 本地顺序写:优先运行配置级别,回退任一有数据的级别
    lv = str(cfg.get("raid_level") or "")
    raid_w = (_m(results, raid.level_case(lv, "02"), "seq_write_w_mbps")
              if lv in RAID_ORDER else None)
    if raid_w is None:
        for lv in RAID_ORDER:
            raid_w = _m(results, raid.level_case(lv, "02"), "seq_write_w_mbps")
            if raid_w is not None:
                break
    ftp = [v for v in (_m(results, "S5-05", "upload_mbps"),
                       _m(results, "S5-05", "download_mbps")) if v is not None]
    layers = [
        ("裸 TCP (iperf3)", _m(results, "S4-01", "tcp_tx_mb_s")),
        ("FTP 协议", max(ftp) if ftp else None),
        ("WebDAV GET", _m(results, "S5-06", "download_mbps")),
        ("SMB 明文读", _m(results, "S5-01", "read_mbps")),
        ("SMB 明文写", _m(results, "S5-01", "write_mbps")),
        (f"{lv} 本地顺序写", raid_w),
        ("NFS 写", _m(results, "S5-04", "seq_write_w_mbps")),
    ]
    layers = sorted([(n, v) for n, v in layers if v is not None],
                    key=lambda x: -x[1])
    return layers, line_mb


def _scale_curves(results: dict):
    wcurve = (_m_dict(results, "S6-01", "scale_write_curve") or [])
    rcurve = (_m_dict(results, "S6-02", "scale_read_curve") or [])

    def pts(curve):
        return [(c.get("n"), c.get("mbps")) for c in curve
                if isinstance(c, dict) and isinstance(c.get("n"), (int, float))
                and isinstance(c.get("mbps"), (int, float))]
    return (pts(wcurve), pts(rcurve),
            _m(results, "S6-01", "write_inflection_n"),
            _m(results, "S6-02", "read_inflection_n"))


def _m_dict(results: dict, case_id: str, key: str):
    r = results.get(case_id) or {}
    v = (r.get("metrics") or {}).get(key)
    return v if isinstance(v, list) else None


def _protocol_series(results: dict):
    """[(协议, 写/上传, 读/下载)];None=未测。"""
    return [
        ("FTP", _m(results, "S5-05", "upload_mbps"), _m(results, "S5-05", "download_mbps")),
        ("WebDAV", _m(results, "S5-06", "upload_mbps"), _m(results, "S5-06", "download_mbps")),
        ("SMB", _m(results, "S5-01", "write_mbps"), _m(results, "S5-01", "read_mbps")),
        ("NFS", _m(results, "S5-04", "seq_write_w_mbps"), _m(results, "S5-04", "seq_read_r_mbps")),
        ("rsync/SSH", _m(results, "S5-08", "rsync_mbps"), None),
    ]


def _crypto_series(results: dict):
    return [
        ("AES-128-GCM\n(aes-ce 硬件)", _m(results, "S1-02", "openssl_aes-128-gcm_mb_s")),
        ("AES-256-GCM\n(aes-ce 硬件)", _m(results, "S1-02", "openssl_aes-256-gcm_mb_s")),
        ("chacha20-poly1305\n(纯软件)", _m(results, "S1-02", "openssl_chacha20-poly1305_mb_s")),
    ]


# ---------------- 绘图 ----------------
def _draw_raid_bars(path, plt, raids, a_vals, b_vals, a_label, b_label,
                    a_color, b_color, ylabel, title, fmt="{:.1f}"):
    """两张 RAID 对比柱状图(顺序吞吐 / 随机 IOPS)的公共绘制。数据不足返回 False。"""
    import numpy as np
    pairs = [(r, a, b) for r, a, b in zip(raids, a_vals, b_vals)
             if a is not None and b is not None]
    if len(pairs) < 2:
        return False
    raids = [p[0] for p in pairs]
    a_vals = [p[1] for p in pairs]
    b_vals = [p[2] for p in pairs]
    x = np.arange(len(raids))
    w = 0.36
    fig, ax = plt.subplots(figsize=(8.4, 4.4), dpi=160)
    b1 = ax.bar(x - w / 2, a_vals, w, label=a_label, color=a_color, zorder=3)
    b2 = ax.bar(x + w / 2, b_vals, w, label=b_label, color=b_color, zorder=3)
    _bar_labels(ax, b1, fmt=fmt)
    _bar_labels(ax, b2, fmt=fmt)
    ax.set_xticks(x)
    ax.set_xticklabels(raids, fontsize=11, color=C_TEXT)
    ax.set_ylabel(ylabel, color=C_MUTED, fontsize=11)
    _title(ax, title)
    ax.legend(frameon=False, fontsize=10, loc="upper right")
    ax.set_ylim(0, max(a_vals + b_vals) * 1.18)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(path, facecolor=C_BG)
    plt.close(fig)
    return True


def _draw_bottleneck(path, plt, layers, line_mb):
    import numpy as np
    if len(layers) < 3:
        return False
    names = [n for n, _ in layers]
    vals = [v for _, v in layers]

    def color(pct):
        return C_BLUE if pct >= 80 else C_GREEN if pct >= 60 else C_ORANGE if pct >= 45 else C_RED
    colors = [color(v / line_mb * 100) for v in vals]
    y = np.arange(len(layers))
    fig, ax = plt.subplots(figsize=(8.6, 4.6), dpi=160)
    bars = ax.barh(y, vals, color=colors, height=0.6, zorder=3)
    for b, v in zip(bars, vals):
        ax.text(v + line_mb * 0.012, b.get_y() + b.get_height() / 2,
                f"{v:.1f} MB/s  ({v / line_mb * 100:.0f}%)",
                va="center", fontsize=9, color=C_TEXT, fontweight="bold")
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=10, color=C_TEXT)
    ax.invert_yaxis()
    ax.set_xlabel(f"吞吐 (MB/s)  —  线速 {line_mb:.0f} MB/s", color=C_MUTED, fontsize=11)
    _title(ax, "瓶颈逐层衰减：从线速到应用可用带宽")
    ax.axvline(line_mb, color=C_GRAY, linewidth=1.2, linestyle="--", zorder=2)
    ax.text(line_mb, -0.6, f" 线速 {line_mb:.0f}", color=C_MUTED, fontsize=9, va="bottom")
    ax.set_xlim(0, max(line_mb, max(vals)) * 1.12)
    _style_ax(ax)
    ax.xaxis.grid(True, color=C_GRID, linewidth=0.8, zorder=0)
    ax.yaxis.grid(False)
    fig.tight_layout()
    fig.savefig(path, facecolor=C_BG)
    plt.close(fig)
    return True


def _draw_scale(path, plt, wpts, rpts, infl_w, infl_r):
    if sum(len(p) >= 2 for p in (wpts, rpts)) < 1:
        return False
    fig, ax = plt.subplots(figsize=(8.0, 4.4), dpi=160)
    all_n, all_v = [], []
    if rpts:
        ns, vs = zip(*rpts)
        ax.plot(ns, vs, marker="o", markersize=8, linewidth=2.2, color=C_BLUE,
                label="并发读", zorder=3)
        for xi, yi in zip(ns, vs):
            ax.text(xi, yi + 2.5, f"{yi:.1f}", ha="center", fontsize=9,
                    color=C_BLUE, fontweight="bold")
        all_n += list(ns)
        all_v += list(vs)
    if wpts:
        ns, vs = zip(*wpts)
        ax.plot(ns, vs, marker="s", markersize=8, linewidth=2.2, color=C_ORANGE,
                label="并发写", zorder=3)
        for xi, yi in zip(ns, vs):
            ax.text(xi, yi - 4, f"{yi:.1f}", ha="center", fontsize=9,
                    color=C_ORANGE, fontweight="bold")
        all_n += list(ns)
        all_v += list(vs)
    # 拐点标注(取曲线上对应点;上半区的点往下标、下半区的点往上标,避免出界)
    lo_all, hi_all = min(all_v), max(all_v)
    for infl, pts, color in ((infl_r, rpts, C_BLUE), (infl_w, wpts, C_ORANGE)):
        if infl is None:
            continue
        hit = next(((n, v) for n, v in pts if n == infl), None)
        if hit:
            below = hit[1] > lo_all + (hi_all - lo_all) * 0.5
            ax.annotate(f"拐点 N={int(infl)}", xy=hit,
                        xytext=(-14, -52 if below else 26), textcoords="offset points",
                        fontsize=9, color=color,
                        arrowprops=dict(arrowstyle="->", color=color, lw=1))
    ax.set_xlabel("并发客户端数 N", color=C_MUTED, fontsize=11)
    ax.set_ylabel("聚合吞吐 (MB/s)", color=C_MUTED, fontsize=11)
    _title(ax, "并发扩展曲线：聚合吞吐随客户端数变化")
    ax.set_xticks(sorted(set(all_n)))
    ax.legend(frameon=False, fontsize=10, loc="lower right")
    lo, hi = min(all_v), max(all_v)
    ax.set_ylim(max(0, lo - (hi - lo) * 0.25), hi + (hi - lo) * 0.25)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(path, facecolor=C_BG)
    plt.close(fig)
    return True


def _draw_protocols(path, plt, series):
    import numpy as np
    rows = [(p, w, r) for p, w, r in series if w is not None or r is not None]
    if len(rows) < 2:
        return False
    protos = [p for p, _, _ in rows]
    wr = [w or 0 for _, w, _ in rows]
    rd = [r or 0 for _, _, r in rows]
    x = np.arange(len(protos))
    w = 0.36
    fig, ax = plt.subplots(figsize=(8.4, 4.4), dpi=160)
    b1 = ax.bar(x - w / 2, wr, w, label="写/上传", color=C_ORANGE, zorder=3)
    b2 = ax.bar(x + w / 2, rd, w, label="读/下载", color=C_BLUE, zorder=3)
    for bars, raw in ((b1, [w for _, w, _ in rows]), (b2, [r for _, _, r in rows])):
        for b, v in zip(bars, raw):
            if v is not None:
                ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 2,
                        f"{v:.1f}", ha="center", va="bottom", fontsize=9,
                        color=C_TEXT, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(protos, fontsize=11, color=C_TEXT)
    ax.set_ylabel("吞吐 (MB/s)", color=C_MUTED, fontsize=11)
    _title(ax, "文件协议读写吞吐横向对比")
    ax.legend(frameon=False, fontsize=10, loc="upper right")
    ax.set_ylim(0, max(wr + rd) * 1.18)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(path, facecolor=C_BG)
    plt.close(fig)
    return True


def _raw_vs_raid_series(results: dict):
    """[(类别名, 顺序读 MB/s, 顺序写 MB/s)]:裸盘并发 + 各 RAID 级别,未测为 None。

    裸盘并发读/写取 S3-BASE-03/04 合计值;阵列在役时裸盘写会被拒跑,
    此时写为 None,绘图端按单侧数据处理。
    """
    rows = []
    m3 = (results.get("S3-BASE-03") or {}).get("metrics") or {}
    m4 = (results.get("S3-BASE-04") or {}).get("metrics") or {}
    disks = m3.get("raw_read_disks") or m4.get("raw_write_disks") or []
    raw_r = _m(results, "S3-BASE-03", "raw_read_all_mb_s")
    raw_w = _m(results, "S3-BASE-04", "raw_write_all_mb_s")
    if raw_r is not None or raw_w is not None:
        rows.append((f"裸盘并发({len(disks)} 盘)" if disks else "裸盘并发",
                     raw_r, raw_w))
    for lv in RAID_ORDER:
        r = _m(results, raid.level_case(lv, "01"), "seq_read_r_mbps")
        w = _m(results, raid.level_case(lv, "02"), "seq_write_w_mbps")
        if r is not None or w is not None:
            rows.append((lv, r, w))
    return rows


def _draw_raw_vs_raid(path, plt, series):
    import numpy as np
    rows = [(n, r, w) for n, r, w in series if r is not None or w is not None]
    if not rows:
        return False
    cats = [n for n, _, _ in rows]
    rd = [r or 0 for _, r, _ in rows]
    wr = [w or 0 for _, _, w in rows]
    x = np.arange(len(cats))
    bw = 0.36
    fig, ax = plt.subplots(figsize=(max(8.4, 1.4 * len(cats) + 3.0), 4.6), dpi=160)
    b1 = ax.bar(x - bw / 2, rd, bw, label="顺序读", color=C_BLUE, zorder=3)
    b2 = ax.bar(x + bw / 2, wr, bw, label="顺序写", color=C_ORANGE, zorder=3)
    for bars, raw in ((b1, [r for _, r, _ in rows]), (b2, [w for _, _, w in rows])):
        for b, v in zip(bars, raw):
            if v is not None:
                ax.text(b.get_x() + b.get_width() / 2, b.get_height() + 2,
                        f"{v:.1f}", ha="center", va="bottom", fontsize=9,
                        color=C_TEXT, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(cats, fontsize=10.5, color=C_TEXT)
    ax.set_ylabel("吞吐 (MB/s)", color=C_MUTED, fontsize=11)
    _title(ax, "裸盘并发 vs 各 RAID 级别:顺序读/写吞吐 (1M / direct)")
    ax.legend(frameon=False, fontsize=10, loc="upper right")
    ax.set_ylim(0, max(rd + wr) * 1.18)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(path, facecolor=C_BG)
    plt.close(fig)
    return True


def _draw_crypto(path, plt, series, line_mb):
    import numpy as np
    rows = [(n, v) for n, v in series if v is not None]
    if len(rows) < 2:
        return False
    names = [n for n, _ in rows]
    vals = [v for _, v in rows]
    colors = [C_BLUE, C_GREEN, C_RED][:len(rows)]
    x = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(7.6, 4.4), dpi=160)
    bars = ax.bar(x, vals, width=0.5, color=colors, zorder=3)
    _bar_labels(ax, bars, fmt="{:.1f}", offset=max(vals) * 0.015)
    if line_mb:
        ax.axhline(line_mb, color=C_GRAY, linewidth=1.2, linestyle="--", zorder=2)
        ax.text(len(rows) - 0.6, line_mb * 1.04, f"千兆线速 {line_mb:.0f}",
                color=C_MUTED, fontsize=9, ha="right")
    ax.set_xticks(x)
    ax.set_xticklabels(names, fontsize=9.5, color=C_TEXT)
    ax.set_ylabel("吞吐 (MB/s)", color=C_MUTED, fontsize=11)
    _title(ax, "加密算法性能对比 (openssl speed)")
    ax.set_ylim(0, max(vals) * 1.15)
    _style_ax(ax)
    fig.tight_layout()
    fig.savefig(path, facecolor=C_BG)
    plt.close(fig)
    return True


# ---------------- 入口 ----------------
def generate_charts(run_id: int, results: dict, cfg: dict, force: bool = False) -> dict:
    """生成图表,返回 {图表键: 文件名}(供报告引用)。

    force=False 时复用已生成的图表(HTML 页面浏览不重绘);报告重新生成时
    force=True 强制重绘。缺 matplotlib / 数据不足时对应图表自动跳过。
    """
    out = evidence.run_dir(run_id) / "charts"
    manifest = out / "manifest.json"
    if not force and manifest.exists():
        try:
            cached = json.loads(manifest.read_text(encoding="utf-8"))
            cached = {k: v for k, v in cached.items() if (out / v).exists()}
            if cached:
                return cached
        except (ValueError, OSError):
            pass
    if not available():
        return {}
    out.mkdir(exist_ok=True)
    for f in out.glob("*.png"):  # 清旧图,避免残留
        f.unlink()
    plt = _setup_pyplot()
    import numpy  # noqa: F401  确保 numpy 可用(matplotlib 绘图依赖)
    generated: dict = {}
    line_mb = int(cfg.get("nic_link_mbps", 1000)) / 8

    def try_draw(key, fn):
        try:
            if fn():
                generated[key] = CHART_FILES[key]
        except Exception:
            pass  # 单图失败不影响其余图表与报告生成

    rs = _raid_series(results)
    raids = list(rs.keys())
    try_draw("raid_seq", lambda: _draw_raid_bars(
        out / CHART_FILES["raid_seq"], plt, raids,
        [rs[r]["seq_read"] for r in raids], [rs[r]["seq_write"] for r in raids],
        "顺序读", "顺序写", C_BLUE, C_ORANGE, "吞吐 (MB/s)",
        "各 RAID 级别顺序吞吐对比 (1M / QD32 × 4jobs)"))
    rs1 = _raid_seq_1job_series(results)
    raids1 = list(rs1.keys())
    try_draw("raid_seq_1job", lambda: _draw_raid_bars(
        out / CHART_FILES["raid_seq_1job"], plt, raids1,
        [rs1[r]["seq_read"] for r in raids1], [rs1[r]["seq_write"] for r in raids1],
        "顺序读(单流)", "顺序写(单流)", C_BLUE, C_ORANGE, "吞吐 (MB/s)",
        "各 RAID 级别顺序吞吐对比 (单流 1M / QD32 × 1job)"))
    try_draw("raid_iops", lambda: _draw_raid_bars(
        out / CHART_FILES["raid_iops"], plt, raids,
        [rs[r]["rand_read"] for r in raids], [rs[r]["rand_write"] for r in raids],
        "4K 随机读", "4K 随机写", C_GREEN, C_RED, "IOPS",
        "各 RAID 级别 4K 随机 IOPS 对比 (QD32 × 4jobs)", fmt="{:.0f}"))
    layers, line = _bottleneck_layers(results, cfg)
    try_draw("bottleneck", lambda: _draw_bottleneck(
        out / CHART_FILES["bottleneck"], plt, layers, line))
    wpts, rpts, infl_w, infl_r = _scale_curves(results)
    try_draw("scale", lambda: _draw_scale(
        out / CHART_FILES["scale"], plt, wpts, rpts, infl_w, infl_r))
    try_draw("protocols", lambda: _draw_protocols(
        out / CHART_FILES["protocols"], plt, _protocol_series(results)))
    try_draw("raw_vs_raid", lambda: _draw_raw_vs_raid(
        out / CHART_FILES["raw_vs_raid"], plt, _raw_vs_raid_series(results)))
    try_draw("crypto", lambda: _draw_crypto(
        out / CHART_FILES["crypto"], plt, _crypto_series(results), line_mb))

    if generated:
        manifest.write_text(json.dumps(generated, ensure_ascii=False), encoding="utf-8")
    return generated
