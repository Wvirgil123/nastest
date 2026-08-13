"""全局配置:路径、域定义、默认任务配置。

重构版 —— ARM NAS 极限性能测试框架(docs/ARM-NAS-极限性能测试方案.md)。
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # nastest/
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "nastest.db"
EVIDENCE_DIR = DATA_DIR / "evidence"
VENDOR_DIR = BASE_DIR / "vendor"

WEB_HOST = "0.0.0.0"
WEB_PORT = 8080

# ---- 域定义(对应方案 §3 环境准备 / §5.1~5.9) ----
DOMAIN_ORDER = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9"]

DOMAIN_NAMES = {
    "S0": "环境与基线", "S1": "CPU 计算性能", "S2": "内存性能",
    "S3": "存储子系统", "S4": "网络层性能", "S5": "文件协议性能",
    "S6": "并发与混合负载", "S7": "应用场景", "S8": "稳定性与持续性能",
    "S9": "功耗与能效",
}

# 性能层次(方案 §1),用于报告分组
TIER_NAMES = {
    "peak": "峰值性能",
    "sustained": "持续性能",
    "breaking": "极限容量",
}

# ---- 设备端工具(缺失时 preflight 一键 apt 安装) ----
DEVICE_PACKAGES = {
    "fio": "fio",
    "iperf3": "iperf3",
    "stress-ng": "stress-ng",
    "sysbench": "sysbench",
    "mpstat": "sysstat",
    "smartctl": "smartmontools",
    "rsync": "rsync",
    "pigz": "pigz",
}

# ---- 本机(测试客户端)工具 ----
LOCAL_TOOLS = ["iperf3", "fio", "rsync", "sshpass"]
# 需要 root 才能检测/使用的(挂载类)
LOCAL_ROOT_TOOLS = ["mount.cifs", "mount.nfs"]
# 本机缺失工具的 apt 包映射(local_sudo_pass 存在时自动安装)
LOCAL_PACKAGES = {
    "iperf3": "iperf3",
    "fio": "fio",
    "mount.cifs": "cifs-utils",
    "mount.nfs": "nfs-common",
    "netperf": "netperf",
}

# ---- 默认任务配置(RK3576 实测环境,可在新建表单修改) ----
DEFAULT_RUN_CONFIG = {
    # 设备
    "device_model": "RK3576 开发板(fnOS)",
    "device_ip": "192.168.8.133",
    "web_url": "http://192.168.8.133:5666/",
    "ssh_user": "seeed",
    "ssh_pass": "seeed",
    "ssh_port": 22,
    # 存储
    "disk_count": 4,
    "disk_desc": "4×500GB HDD",
    "raid_level": "RAID6",
    "volume_path": "",            # 留空=自动探测(优先 /vol* 数据卷)
    "device_test_dir": "",        # 留空=跟随卷自动推导(<卷>/.nastest_perf)
    # 协议
    "protocols": ["SMB", "NFS", "FTP", "WebDAV"],
    "smb_user": "seeed",
    "smb_pass": "seeed",
    "webdav_url": "http://192.168.8.133:5005",
    "ftp_port": 21,
    # 测试纪律(方案 §3.3)
    "rounds": 3,               # 每项 ≥3 轮取中位数
    "warmup_seconds": 30,      # 每轮前热身
    "fio_runtime": 60,         # fio time_based 单轮时长(秒)
    "fio_size": "4G",          # fio 单 job 文件大小(完整验收建议 16G,>物理内存)
    # 网络
    "iperf_ports": [5201, 5202, 5203, 5204],
    "nic_link_mbps": 1000,     # 网卡线速(Mbps),用于利用率评级
    # 浸泡(方案 §5.8)
    "soak_minutes": 240,       # 综合满载时长;方案建议 ≥4h,完整 24h=1440
    "soak_sample_interval": 60,
    # 客户端
    "local_sudo_pass": "",     # 本机 sudo 密码:自动装包/挂载/改 MTU(可空=人工指导)
    "local_test_dir": "/tmp/nastest_local",    # 本机测试文件目录
    "local_fio_dir": "",       # 客户端自检用本机 NVMe 目录(空=local_test_dir)
    # 协议测试文件
    "smb_bigfile_mb": 512,     # 纯软件协议测试大文件(完整验收建议 ≥4096)
}

RESULT_STATUS = ["pending", "running", "pass", "fail", "skipped", "blocked", "manual_pending"]
RUN_STATUS = ["pending", "running", "paused", "completed", "aborted"]

# §8 评级
GRADES = ["优", "良", "及格", "差"]
