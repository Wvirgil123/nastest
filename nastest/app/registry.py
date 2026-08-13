"""极限性能测试用例注册表(依据 docs/ARM-NAS-极限性能测试方案.md)。

域 S0~S9 对应方案 §3 环境准备与 §5.1~5.9 测试维度。

mode:
  auto   — 引擎自动执行(SSH 设备端 / 本机客户端)
  manual — 需人工操作;部分带 impl 表示人工操作后可点击"运行自动校验/录入"
hazard: critical — 破坏性/数据风险操作,需输入用例编号确认
tier:   peak 峰值 / sustained 持续 / breaking 极限容量(方案 §1)
"""
from dataclasses import dataclass, field


@dataclass
class Case:
    id: str
    domain: str
    title: str
    mode: str                # auto / manual
    steps: str               # 测试方法(来自方案)
    expected: str            # 记录指标 / 判读标准
    guide: str = ""          # 人工操作指导(mode=manual 时显示)
    impl: str | None = None  # 实现函数键(caseimpl.IMPL)
    hazard: str = "none"     # none / warning / critical
    deps: list[str] = field(default_factory=list)
    timeout: int = 600
    tier: str = "peak"       # peak / sustained / breaking


def _c(**kw) -> Case:
    return Case(**kw)


# S3 各用例按检测到的 RAID 级别累计结果;人工重建阵列后重跑即可对比多种 RAID
_RAID_NOTE = (";自动识别当前 RAID 级别(RAID0/1/5/6/10)并按级别累计,"
              "人工重建阵列后重跑本项即可加入新 RAID 的对比数据")


CASES: list[Case] = [
    # ================= S0 环境与基线(方案 §3) =================
    _c(id="S0-01", domain="S0", title="基线信息采集", mode="auto", impl="s0_baseline",
       steps="采集 SoC/核数/频率(大小核)/内存/页大小/内核/文件系统/RAID/网口速率/governor/"
             "/proc/crypto 加密驱动/SMART/工具版本,生成 baseline.json + baseline.md",
       expected="§3.2 基线信息表完整;记录 aes-ce 驱动存在性、大小核频率、页大小"),
    _c(id="S0-02", domain="S0", title="CPU 调频锁定 performance", mode="auto", impl="s0_governor",
       steps="记录当前 governor → sudo 写入 performance → 复核;失败仅告警不判失败",
       expected="governor=performance(方案 §3.3);若被固件锁定则记录实际值并给出人工指引"),
    _c(id="S0-03", domain="S0", title="后台任务审计", mode="auto", impl="s0_background",
       steps="列出 docker/容器、cron 计划、相册索引、下载等后台进程与磁盘 IO 占用",
       expected="无干扰性后台负载;若有则输出清单与停止指引(方案 §3.3)"),
    _c(id="S0-04", domain="S0", title="客户端自检(本机非瓶颈)", mode="auto", impl="s0_client_selfcheck",
       steps="本机 NVMe 上跑 fio randrw 70/30 4k QD64×4(方案 §3.1)",
       expected="本机磁盘 IOPS/带宽远高于 NAS 预期性能;否则并发类结果不可信"),

    # ================= S1 CPU 计算性能(方案 §5.1) =================
    _c(id="S1-01", domain="S1", title="sysbench 整数(单核/多核)", mode="auto", impl="s1_sysbench_cpu",
       steps="sysbench cpu --cpu-max-prime=20000,threads=1 与 threads=$(nproc) 各 rounds 轮",
       expected="记录单核/多核 events/s 与多核扩展比(多核÷单核÷核数,理想=1)"),
    _c(id="S1-02", domain="S1", title="openssl 加密性能(ARM 关键)", mode="auto", impl="s1_openssl",
       steps="openssl speed -seconds 5 -evp aes-128-gcm / aes-256-gcm / chacha20-poly1305",
       expected="记录各算法大 block 吞吐 MB/s;确认走 aes-ce 硬件指令(§2),加密差→SMB加密/VPN 受限"),
    _c(id="S1-03", domain="S1", title="压缩性能 pigz", mode="auto", impl="s1_pigz",
       steps="pigz 多线程压缩/解压固定数据(替代 7zz)",
       expected="记录压缩 MB/s;考验整数+内存子系统"),
    _c(id="S1-04", domain="S1", title="CoreMark(单核/多核)", mode="auto", impl="s1_coremark",
       steps="git clone eembc/coremark → make 编译 → 单核与全核运行",
       expected="记录 CoreMark 总分与 iterations/s;无外网时给出离线指引", timeout=1800),

    # ================= S2 内存性能(方案 §5.2) =================
    _c(id="S2-01", domain="S2", title="STREAM 内存带宽", mode="auto", impl="s2_stream",
       steps="官方 stream.c(在线下载,失败用内置等价实现)gcc -O3 -fopenmp 编译,"
             "数组 ≥4 倍缓存,OMP 全核运行",
       expected="记录 Copy/Scale/Add/Triad GB/s;单通道 DDR4 Triad 通常 <20GB/s(§5.2 判读)", timeout=1200),
    _c(id="S2-02", domain="S2", title="sysbench 内存带宽/延迟", mode="auto", impl="s2_sysbench_mem",
       steps="sysbench memory 1M 块全核带宽 + 4K 随机访问延迟",
       expected="记录 MiB/s 与随机延迟;与 STREAM 交叉验证"),

    # ================= S3 存储子系统(方案 §5.3) =================
    _c(id="S3-01", domain="S3", title="顺序读 1M QD32×4(峰值读)", mode="auto", impl="s3_seq_read",
       steps="fio rw=read bs=1M iodepth=32 numjobs=4 direct=1,热身+rounds 轮取中位数" + _RAID_NOTE,
       expected="记录 MB/s、CPU%;文件>内存且 direct=1(§3.3)", timeout=3600),
    _c(id="S3-02", domain="S3", title="顺序写 1M QD32×4(峰值写)", mode="auto", impl="s3_seq_write",
       steps="fio rw=write bs=1M iodepth=32 numjobs=4 direct=1" + _RAID_NOTE,
       expected="记录 MB/s、CPU%;RAID 写含奇偶校验开销,观察 CPU 是否瓶颈", timeout=3600),
    _c(id="S3-03", domain="S3", title="4K 随机读 QD32×4(IOPS)", mode="auto", impl="s3_rand_read",
       steps="fio rw=randread bs=4k iodepth=32 numjobs=4 direct=1" + _RAID_NOTE,
       expected="记录 IOPS、平均/P99 延迟", timeout=3600),
    _c(id="S3-04", domain="S3", title="4K 随机写 QD32×4(IOPS)", mode="auto", impl="s3_rand_write",
       steps="fio rw=randwrite bs=4k iodepth=32 numjobs=4 direct=1" + _RAID_NOTE,
       expected="记录 IOPS、平均/P99 延迟", timeout=3600),
    _c(id="S3-05", domain="S3", title="4K 混合读写 70/30 QD64×4", mode="auto", impl="s3_randrw",
       steps="fio rw=randrw rwmixread=70 bs=4k iodepth=64 numjobs=4 direct=1,"
             "percentile_list=50:90:99:99.9" + _RAID_NOTE,
       expected="记录混合 IOPS 与 P50/P90/P99/P99.9 延迟(典型负载)", timeout=3600),
    _c(id="S3-06", domain="S3", title="4K 随机读 QD1 最低延迟", mode="auto", impl="s3_qd1_lat",
       steps="fio rw=randread bs=4k iodepth=1 numjobs=1 direct=1" + _RAID_NOTE,
       expected="记录 QD1 延迟(μs),单路径极限", timeout=3600),
    _c(id="S3-07", domain="S3", title="fsync 提交延迟(数据库类)", mode="auto", impl="s3_fsync_lat",
       steps="fio rw=write bs=4k iodepth=1 fsync=1 direct=1" + _RAID_NOTE,
       expected="记录 fsync 延迟(μs),数据库类负载关键指标", timeout=3600),
    _c(id="S3-08", domain="S3", title="元数据与小文件", mode="auto", impl="s3_metadata",
       steps="脚本计时:1 万文件 create/stat/read/delete(mdtest 无包,自研等价脚本)" + _RAID_NOTE,
       expected="记录各阶段 files/s;小文件 IOPS 远低裸盘→FS/RAID 元数据开销(§8 红旗)", timeout=1800),
    _c(id="S3-09", domain="S3", title="裸盘读基线(定位层损耗)", mode="auto", impl="s3_raw_read",
       hazard="warning",
       steps="自动识别 md 成员盘,fio 只读测试(read 不写盘,安全)" + _RAID_NOTE,
       expected="记录单裸盘顺序读 MB/s;用于计算 FS 层损耗与 RAID 效率(§5.3)", timeout=3600),
    _c(id="S3-10", domain="S3", title="RAID 重建速度与 IO 衰减", mode="manual", impl="s3_rebuild_monitor",
       hazard="critical", timeout=14400,
       steps="拔盘触发降级→(可选插回/换盘)触发重建;框架轮询 /proc/mdstat 记录速度曲线,"
             "重建中叠加 fio 测业务 IO 衰减",
       expected="记录重建时长/速度(MB/s)、重建期间业务 IO 衰减%(§8:<30% 优)",
       guide="1. 确认 RAID 为 RAID1/5/6(可降级);准备承受降级风险;\n"
             "2. 点击\"运行\"前从设备拔出一块成员盘(或 sudo mdadm --fail + --remove);\n"
             "3. 点击\"运行\":框架确认降级后,插回/更换硬盘触发重建并全程监控;\n"
             "4. 重建期间框架自动叠加 fio 负载测衰减;重建完成后自动结束。"),

    # ================= S4 网络层(方案 §5.4) =================
    _c(id="S4-01", domain="S4", title="TCP 单流上行(客户端→NAS)", mode="auto", impl="s4_tcp_tx",
       steps="设备 iperf3 -s;本机 iperf3 -c -t 60 -J,rounds 轮取中位数",
       expected="记录 Gbps/MB/s、重传数;1GbE 线速参考 ≈118MB/s", timeout=1800),
    _c(id="S4-02", domain="S4", title="TCP 单流下行(NAS→客户端)", mode="auto", impl="s4_tcp_rx",
       steps="iperf3 -R 反向",
       expected="记录 Gbps/MB/s;对比上行判断方向性瓶颈", timeout=1800),
    _c(id="S4-03", domain="S4", title="TCP 多实例聚合(4 端口)", mode="auto", impl="s4_tcp_multi",
       steps="设备起 4 个 iperf3 -s(5201-5204);本机 4 客户端并发 -J 汇总",
       expected="记录聚合 Gbps 与叠加比;弱 ARM 核单实例可能先饱和(§2),多实例排除误判", timeout=1800),
    _c(id="S4-04", domain="S4", title="UDP 64B 小包 PPS", mode="auto", impl="s4_udp_pps",
       steps="iperf3 -u -l 64 -b 0 -t 30",
       expected="记录 PPS、丢包率、jitter;考验 ARM 网络栈与中断处理", timeout=1800),
    _c(id="S4-05", domain="S4", title="巨帧 MTU9000 收益", mode="auto", impl="s4_mtu9000",
       steps="两端 sudo 改 MTU 9000 → iperf3 复测 → 恢复 1500(需本机 sudo 密码)",
       expected="记录巨帧前后吞吐与收益%;交换机不支持巨帧时会降速,需结合环境判读", timeout=1800),
    _c(id="S4-06", domain="S4", title="WireGuard 加密隧道吞吐", mode="manual",
       steps="建立 WireGuard 隧道后在隧道 IP 上跑 iperf3",
       expected="记录隧道吞吐与明文吞吐比(ARM 加密指令实战体现)",
       guide="1. 设备与本机安装 wireguard-tools 并配置一对密钥(可参考 wg-quick 示例);\n"
             "2. 隧道建立后:iperf3 -s 在设备隧道 IP 上监听,本机 iperf3 -c <隧道IP> -t 60;\n"
             "3. 将结果(Mbps)填入判定备注或上传截图后提交判定。"),

    # ================= S5 文件协议性能(方案 §5.5) =================
    _c(id="S5-01", domain="S5", title="SMB 大文件读写(纯软件)", mode="auto", impl="s5_smb_soft",
       steps="smbprotocol 直连,大文件写+读计时(无需 root 的兜底路径)",
       expected="记录写/读 MB/s;与挂载版(S5-03)交叉验证", timeout=3600),
    _c(id="S5-02", domain="S5", title="SMB 加密(seal)性能对比", mode="auto", impl="s5_smb_seal",
       steps="同一大文件,分别在 seal 开/关下传输",
       expected="记录加密损失%(§8:<20% 优);腰斩以上→AES 硬件指令未启用(红旗)", timeout=3600),
    _c(id="S5-03", domain="S5", title="SMB 挂载 + fio", mode="auto", impl="s5_smb_mount",
       steps="mount -t cifs vers=3.1.1,cache=none,rsize/wsize=1M → 本机 fio 顺序读/写/混合 → umount"
             "(需本机 sudo)",
       expected="记录各负载 MB/s/IOPS/P99;协议效率=协议吞吐÷本地卷吞吐", timeout=5400),
    _c(id="S5-04", domain="S5", title="NFS 导出+挂载+fio", mode="auto", impl="s5_nfs_mount",
       steps="设备自动配置 /etc/exports(v4.1)→ 本机 mount nfs nconnect=8 → fio → umount(需本机 sudo)",
       expected="记录 MB/s/IOPS/P99;对比 SMB 协议开销", timeout=5400),
    _c(id="S5-05", domain="S5", title="FTP 大文件计时", mode="auto", impl="s5_ftp",
       steps="ftplib 上传+下载大文件计时",
       expected="记录上传/下载 MB/s", timeout=3600),
    _c(id="S5-06", domain="S5", title="WebDAV 大文件计时", mode="auto", impl="s5_webdav",
       steps="httpx PUT+GET 大文件计时",
       expected="记录上传/下载 MB/s", timeout=3600),
    _c(id="S5-07", domain="S5", title="iSCSI 块存储", mode="manual",
       steps="配置 iSCSI target → 本机 iscsiadm 登录 → 裸盘 fio 矩阵",
       expected="记录顺序/随机 MB/s 与 IOPS(虚拟化场景关键)",
       guide="1. 若 fnOS 提供 iSCSI 功能:在控制台创建 target 与 LUN,记录 IQN;\n"
             "2. 本机:iscsiadm -m discovery -t st -p <NAS_IP> && iscsiadm -m node --login;\n"
             "3. 对 /dev/sdX 裸跑:fio --filename=/dev/sdX --rw=randread --bs=4k --iodepth=32 --direct=1 --runtime=60 --time_based;\n"
             "4. 将结果填入备注或上传截图后提交判定。若设备不支持 iSCSI,判为跳过并注明。"),
    _c(id="S5-08", domain="S5", title="rsync 备份吞吐", mode="auto", impl="s5_rsync",
       steps="本机 rsync -a 大目录 → 设备(sshpass 认证)计时",
       expected="记录 MB/s;增量/全量备份场景", timeout=3600),
    _c(id="S5-09", domain="S5", title="SMB 小文件批量(1万)", mode="auto", impl="s5_smb_small",
       steps="smbprotocol 写/读/删 1 万个 4K~64K 文件计时",
       expected="记录各阶段 files/s;ARM 小核元数据瓶颈专项(§2)", timeout=3600),

    # ================= S6 并发与混合负载(方案 §5.6) =================
    _c(id="S6-01", domain="S6", title="并发扩展-顺序写 1/2/4/8 路", mode="auto", impl="s6_scale_write",
       steps="N 线程并发 SMB 顺序写,同时采样设备 CPU/内存/网络",
       expected="记录 N×聚合 MB/s 曲线与拐点(§8:≥8 路优);拐点时资源占用定位瓶颈", timeout=3600),
    _c(id="S6-02", domain="S6", title="并发扩展-顺序读 1/2/4/8 路", mode="auto", impl="s6_scale_read",
       steps="先写入 N 个测试文件,再 N 线程并发读",
       expected="记录 N×聚合 MB/s 曲线与拐点", timeout=3600),
    _c(id="S6-03", domain="S6", title="读写混合并发(半读半写)", mode="auto", impl="s6_mixed",
       steps="4 路读 + 4 路写同时进行",
       expected="记录读/写各自 MB/s 与相互干扰比例", timeout=3600),

    # ================= S7 应用场景(方案 §5.7) =================
    _c(id="S7-01", domain="S7", title="ffmpeg 转码 fps", mode="auto", impl="s7_ffmpeg",
       steps="testsrc2 生成 1080p 样片 → libx264 veryfast 转码测 fps;探测 rkmpp 硬编",
       expected="记录 fps 与 CPU 占用;1080p≥25fps 为实时;多数 ARM SoC 仅硬解无硬编", timeout=1800),
    _c(id="S7-02", domain="S7", title="Docker 容器", mode="auto", impl="s7_docker",
       steps="检测 dockerd;可用则容器启动耗时+基础负载,不可用则采集诊断",
       expected="记录容器启动秒数;daemon 异常时输出 journalctl 诊断与修复指引", timeout=1200),
    _c(id="S7-03", domain="S7", title="相册索引/下载等 fnOS 场景", mode="manual",
       steps="相册导入 1000 张测索引速度;BT 多任务满速下载",
       expected="记录 张/分钟、下载速度、磁盘写入是否瓶颈",
       guide="1. 相册:导入约 1000 张照片,计时缩略图/AI 识别生成(张/分钟);\n"
             "2. 下载:BT 客户端添加 3+ 任务满速下载,记录总速度与磁盘 IO 占用;\n"
             "3. 将结果填入备注或上传截图后提交判定;不适用可判跳过。"),

    # ================= S8 稳定性与持续性能(方案 §5.8) =================
    _c(id="S8-01", domain="S8", title="综合满载浸泡(CPU+IO+网络)", mode="auto", impl="s8_soak",
       tier="sustained", timeout=90000,
       steps="stress-ng CPU + fio 持续混合写 +(有 iperf3 时)网络打流,同跑 soak_minutes;"
             "每 soak_sample_interval 采样温度/频率/IOPS/带宽/内存/连接数 → soak.csv",
       expected="采样曲线完整;方案建议 ≥4h(默认 240min),完整验收 24h=1440min"),
    _c(id="S8-02", domain="S8", title="衰减与降频分析", mode="auto", impl="s8_analyze",
       deps=["S8-01"], timeout=600,
       steps="解析 soak.csv:首末段吞吐波动%、温度-频率-性能相关性、资源爬升检查",
       expected="§5.8 通过标准:波动<10%;无持续热降频(或衰减<15%);无服务崩溃;无资源单调爬升"),
    _c(id="S8-03", domain="S8", title="满容量性能衰减", mode="manual", impl="s8_full_capacity",
       tier="breaking", hazard="critical", timeout=7200,
       steps="卷填充至 ≥90% 后重测顺序写与 4K 随机写,对比空卷",
       expected="记录衰减%(§8:<10% 优);机械盘内圈/btrfs 分配/SSD GC 都会体现",
       guide="1. 点击\"运行\"后框架会用 dd/fallocate 将卷填充到目标占比(默认 90%),耗时较长请确认磁盘空间无他用;\n"
             "2. 填充完成后自动复测 S3-02/S3-04 同参数并对比;\n"
             "3. 测试结束后框架清理填充文件;若不想自动填充,可手动填满后在备注录入前后数据提交判定。"),
    _c(id="S8-04", domain="S8", title="异常断电恢复", mode="manual",
       tier="breaking", hazard="critical",
       steps="满载压测中直接断电,重启后检查 FS 一致性/RAID 状态/数据完整性(md5 样本)",
       expected="重启正常、RAID 无异常、抽样数据 md5 一致",
       guide="1. 先在设备上写入校验样本:dd if=/dev/urandom of=/vol2/.nastest_perf/md5sample bs=1M count=512 && md5sum 记录;\n"
             "2. 在持续负载(可先跑 S8-01)期间直接拔掉设备电源;\n"
             "3. 重新上电开机后:检查 dmesg/RAID 状态(cat /proc/mdstat)、复算 md5sum;\n"
             "4. 将结果(重启耗时/RAID 状态/md5 是否一致)填入备注或上传截图后提交判定。"),

    # ================= S9 功耗与能效(方案 §5.9) =================
    _c(id="S9-01", domain="S9", title="功率计读数与能效", mode="manual", impl="s9_power",
       steps="计量插座分别测:空闲(盘休眠后)/盘转空闲/单维满载/综合满载 功耗",
       expected="记录各状态瓦数;能效=吞吐(MB/s)÷整机功耗(W)——ARM NAS 核心卖点,必须量化",
       guide="1. 将设备接入计量插座(功率计);\n"
             "2. 空闲:等待硬盘休眠后 10 分钟读数;盘转空闲:盘转无 IO;\n"
             "3. 单维满载:可分别点击运行 S1-01(CPU)/S3-02(IO)/S4-01(网络)时读数;\n"
             "4. 综合满载:S8-01 浸泡期间读数;\n"
             "5. 在下方\"录入读数\"表单填入各状态瓦数,框架自动计算 MB/s/W。"),
]

BY_ID = {c.id: c for c in CASES}
ALL_IDS = [c.id for c in CASES]
MANUAL_IDS = [c.id for c in CASES if c.mode == "manual"]
AUTO_IDS = [c.id for c in CASES if c.mode == "auto"]
