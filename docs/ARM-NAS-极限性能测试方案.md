# ARM NAS 极限性能测试方案

> 版本：v1.0 ｜ 日期：2026-08 ｜ 适用对象：ARM（aarch64）架构 NAS 设备的极限性能测评
>
> 本方案定义测试维度、测试内容与具体测试方法，目标是产出**可对比、可复现**的量化数据。

---

## 1. 测试目标与定义

"极限性能"不是单项跑分的峰值，本方案将其拆为三个层次，全部都要覆盖：

| 层次 | 定义 | 对应测试 |
|---|---|---|
| **峰值性能（Peak）** | 最优配置下单项能达到的最大吞吐 / 最低延迟 / 最高 IOPS | 各单项测试（5.1 ~ 5.7） |
| **持续性能（Sustained）** | 长时间（≥4h，建议 24h）满载下能稳定保持的性能 | 稳定性与持续性能测试（5.8） |
| **极限容量（Breaking Point）** | 并发持续加大、容量写满、重建中运行等临界状态下的表现与拐点 | 并发扩展、满容量、重建叠加测试 |

测试结论应至少回答四个问题：

1. 这台设备的性能上限在哪里，瓶颈是 CPU、内存、存储还是网络？
2. 长时间满载后性能是否衰减？衰减多少？是否因过热降频？
3. 多用户并发时性能如何扩展？拐点在几路并发？
4. 开启加密 / 压缩 / 快照等特性后性能损失多少？

---

## 2. ARM 平台关键特性及对测试的影响

ARM NAS 与 x86 NAS 的测试重点不同，以下特性必须在测试设计中显式考虑：

| ARM 平台特性 | 对性能的影响 | 测试应对 |
|---|---|---|
| 单核性能普遍弱于桌面 x86 | 小文件、元数据操作、协议栈处理易成瓶颈 | 必须单列小文件 / 元数据 / 高 PPS 测试项 |
| 常为被动散热、功耗墙低 | 长时间满载会触发热降频 | 必测温度-频率-性能曲线与 24h 持续负载 |
| ARMv8 Crypto Extensions（AES 指令） | 决定 SMB 加密、VPN、HTTPS、ZFS 加密的性能 | `openssl speed` 验证；确认内核启用 `aes-ce` 驱动 |
| NEON SIMD | 影响 RAID 奇偶校验、压缩、校验和计算 | 关注 RAID5/6 写入与 btrfs/ZFS 场景 |
| 内存带宽受限（常为单通道 DDR4） | 大文件拷贝、转码、多盘并发可能受带宽限制 | STREAM 带宽测试；多盘并发与带宽对照 |
| 部分 SoC 为大小核（big.LITTLE） | 负载调度到大核/小核结果差异明显 | 记录测试时各核频率与占用；必要时绑核复测 |
| 内核页大小可能为 16K/64K | 影响 4K IO 与内存映射行为 | 记录 `getconf PAGESIZE` |
| 弱核上 iperf3 单实例即是瓶颈 | 单进程 iperf3 在弱 ARM 核上可能只能打满 2~4 Gbps | 网络测试用多实例多端口，避免误判网卡上限 |

---

## 3. 测试环境准备

### 3.1 测试拓扑与客户端要求

```
[客户端 1..N] ──── 2.5GbE / 10GbE（直连或交换机）──── [被测 ARM NAS (DUT)]
```

客户端是"打流方"，其性能必须高于 DUT，否则测出的是客户端上限：

- **CPU**：x86-64，≥ 8 核（并发测试时每个客户端独立一台或确认本机资源充足）
- **内存**：≥ 16 GB
- **本地磁盘**：NVMe SSD（并发多路打流建议 RAID0 或多块 NVMe），本地顺序读写 ≥ 2 GB/s
- **网卡**：与 DUT 同档或更高（2.5G / 10G），驱动开启多队列
- **系统**：Linux（内核 ≥ 5.15），测试前关闭客户端侧防火墙限速、节能策略

**客户端自检（先测客户端自己，确认不是瓶颈）：**

```bash
# 客户端本地盘 fio 自检（应远高于 NAS 预期性能）
fio --name=local --directory=/data/test --size=8G --rw=randrw --rwmixread=70 \
    --bs=4k --ioengine=libaio --iodepth=64 --numjobs=4 --direct=1 \
    --runtime=60 --time_based --group_reporting
```

### 3.2 被测设备基线信息记录（测试前必填）

| 项目 | 记录内容 |
|---|---|
| 硬件 | SoC 型号、CPU 核心/频率、内存容量/频率/通道数、页大小 |
| 软件 | 固件版本、内核版本（`uname -a`）、发行版、文件系统类型 |
| 磁盘 | 型号 × 数量、CMR/SMR、RAID 模式（Basic/RAID0/1/5/6/10）、容量、SSD 缓存配置 |
| 网络 | 网口速率、MTU、是否 LACP 聚合、网卡驱动与队列数 |
| 特性开关 | 共享加密、卷加密、快照、压缩、去重、QoS——每一项开/关都要注明 |
| 环境 | 室温、摆放方式（影响散热结论） |

### 3.3 变量控制与测试纪律

1. **CPU 调频**：固定为性能模式（如 `performance` governor），并记录；同时记录满载实际频率（用于判断降频）。
2. **清理后台**：测试前停止 Docker 容器、下载任务、相册索引、备份计划等；若测试目标包含它们，需显式说明。
3. **重复次数**：每个测试项 ≥ 3 轮，取中位数；记录各轮数值，波动 > 10% 需查明原因。
4. **避开缓存假象**：测真实磁盘性能必须 `direct=1`（O_DIRECT），且测试文件大小 **大于物理内存**；测"缓存加速上限"时再单独测小文件。
5. **热身**：每轮正式测试前空跑 30~60s 预热（预读缓存、TCP 窗口、SSD 预热）。
6. **SSD 写放大**：顺序写测试文件要大于 SSD 的 SLC 缓存区，记录"前段高速"与"缓外稳态"两个值。
7. **单一变量**：对比加密/压缩/快照影响时，只改变该开关，其余配置完全一致。

### 3.4 工具清单

| 工具 | 用途 | 备注 |
|---|---|---|
| fio | 存储 IO 基准 | NAS 与客户端都装 |
| iperf3 / nuttcp | 网络吞吐 | 弱 ARM 核建议多实例 |
| netperf / sockperf | 网络延迟、请求-响应 | TCP_RR |
| sysbench | CPU / 内存快速基准 | |
| CoreMark | ARM 单核/多核微基准 | 统一编译器与参数才可横评 |
| STREAM | 内存带宽 | 源码编译 |
| lmbench | 内存/系统调用延迟 | 可选 |
| openssl | 加密指令性能 | `speed -evp` |
| stress-ng | 压力与烤机 | |
| mdtest / filebench | 元数据 / 文件服务器模型 | |
| htop / sensors / smartmontools | 监控温度、频率、磁盘 | |
| 功率计（计量插座） | 整机功耗 | 能效测试必需 |

---

## 4. 测试维度总览

| # | 维度 | 关键指标 | 主要工具 | 章节 |
|---|---|---|---|---|
| 1 | CPU 计算 | 整数/加密/压缩性能、多核扩展比、满载频率 | sysbench / CoreMark / openssl / 7zip | 5.1 |
| 2 | 内存 | 带宽（GB/s）、随机访问延迟 | STREAM / sysbench / lmbench | 5.2 |
| 3 | 存储子系统 | 顺序/随机吞吐、IOPS、延迟（P99）、RAID 效率、重建速度 | fio / mdtest | 5.3 |
| 4 | 网络层 | TCP/UDP 吞吐、PPS、延迟、聚合效率 | iperf3 / netperf | 5.4 |
| 5 | 文件协议 | SMB/NFS/FTP/WebDAV/iSCSI 吞吐与延迟 | fio over 挂载点 | 5.5 |
| 6 | 并发与混合负载 | 并发扩展曲线、拐点、混合负载下 P99 | filebench / 多客户端 | 5.6 |
| 7 | 应用场景 | 转码 fps、Docker、相册索引、备份、下载 | ffmpeg / 实际套件 | 5.7 |
| 8 | 稳定性与持续性能 | 24h 性能曲线、温度、降频、重建叠加、满容量衰减 | stress-ng + fio + 监控 | 5.8 |
| 9 | 功耗与能效 | 空闲/满载功耗、MB/s 每瓦 | 功率计 | 5.9 |

---

## 5. 详细测试项与方法

### 5.1 CPU 计算性能

**测试项：**

1. **整数计算（单核/多核）**：sysbench cpu，记录 events/s 与多核扩展比（多核分数 ÷ 单核分数 ÷ 核数，理想为 1）。
2. **ARM 微基准**：CoreMark 单核分与多核分（统一工具链编译才可横向对比）。
3. **加密性能（ARM 关键点）**：AES-GCM / ChaCha20-Poly1305 吞吐，直接决定 SMB 加密、VPN、HTTPS 能力。
4. **压缩性能**：7-Zip 压缩/解压缩，考验整数 + 内存子系统。
5. **满载频率与降频观察**：负载期间记录各核实际频率，判断是否触发功耗墙/温度墙。

**方法：**

```bash
# 整数：单核
sysbench cpu --threads=1 --cpu-max-prime=20000 run
# 整数：全核
sysbench cpu --threads=$(nproc) --cpu-max-prime=20000 run

# 加密：确认用了 ARMv8 Crypto Extensions（看 /proc/crypto 中是否有 aes-ce / gcm-ce）
cat /proc/crypto | grep -E 'name|module|driver' | head -40
openssl speed -seconds 5 -evp aes-128-gcm
openssl speed -seconds 5 -evp aes-256-gcm
openssl speed -seconds 5 -evp chacha20-poly1305

# 压缩
7zz b -mmt=$(nproc)

# 频率监控（另开终端，贯穿所有负载测试）
watch -n 1 'grep MHz /proc/cpuinfo; cat /sys/class/thermal/thermal_zone*/temp'

# 综合可选：Geekbench 6（有 aarch64 Linux 版）/ Phoronix Test Suite
```

**记录**：单核/多核 events/s、CoreMark/MHz 或总分、AES-256-GCM 吞吐（MB/s）、7z MIPS、满载频率曲线。

---

### 5.2 内存性能

**测试项：** 内存带宽（Copy/Scale/Add/Triad）、大块顺序读写带宽、随机访问延迟。

```bash
# STREAM（数组大小 ≥ 4 倍缓存总和，80M 个 double ≈ 每数组 610MB）
gcc -O3 -fopenmp -DSTREAM_ARRAY_SIZE=80000000 -DNTIMES=10 stream.c -o stream
OMP_NUM_THREADS=$(nproc) ./stream

# sysbench 内存（顺序读写带宽）
sysbench memory --threads=$(nproc) --memory-block-size=1M --memory-total-size=200G run
# 小延迟
sysbench memory --threads=1 --memory-block-size=4K --memory-access-mode=rnd run

# lmbench 随机访问延迟（可选）
lat_mem_rd -P $(nproc) 512 64
```

**记录**：Triad 带宽（GB/s）、sysbench 带宽（MiB/s）、随机延迟（ns）。
**判读**：单通道 DDR4 的 Triad 通常 < 20 GB/s；若多盘并发总吞吐接近该值，说明内存带宽已成系统瓶颈。

---

### 5.3 存储子系统性能

按"裸盘 → RAID → 文件系统 → 缓存层"逐层测试，便于定位损耗发生在哪一层。

#### 5.3.1 单盘裸性能（绕过文件系统，可选但推荐）

```bash
fio --name=disk_seq_w --filename=/dev/sdX --rw=write  --bs=128k --ioengine=libaio --iodepth=32 --direct=1 --size=100%
fio --name=disk_seq_r --filename=/dev/sdX --rw=read   --bs=128k --ioengine=libaio --iodepth=32 --direct=1 --size=100%
fio --name=disk_rnd_r --filename=/dev/sdX --rw=randread --bs=4k --ioengine=libaio --iodepth=32 --runtime=120 --time_based --direct=1
```

#### 5.3.2 RAID / 卷性能（fio 标准矩阵）

测试文件大小 > 物理内存，`direct=1`：

| 用例 | rw | bs | iodepth | numjobs | 考察点 |
|---|---|---|---|---|---|
| 大文件顺序读 | read | 1M | 32 | 4 | 峰值读吞吐 |
| 大文件顺序写 | write | 1M | 32 | 4 | 峰值写吞吐（注意 SSD 缓外速度） |
| 4K 随机读 | randread | 4k | 32 | 4 | IOPS |
| 4K 随机写 | randwrite | 4k | 32 | 4 | IOPS |
| 4K 混合读写 | randrw (rwmixread=70) | 4k | 64 | 4 | 典型负载 IOPS |
| 4K 随机读延迟 | randread | 4k | 1 | 1 | 最低延迟（QD1） |
| 提交延迟 | write + fsync=1 | 4k | 1 | 1 | 数据库类负载 |

> ⚠️ **iodepth > 1 必须配异步引擎**：fio 默认 `sync` 引擎会把 iodepth 静默压回 1，测出的是单路延迟而非峰值。所有 iodepth≥32/64 用例须加 `--ioengine=libaio`（或 io_uring）。QD1 用例无需。

```bash
# 示例：混合读写，输出 P50/P99/P99.9 延迟
fio --name=mix --directory=/volume1/fio --size=16G \
    --rw=randrw --rwmixread=70 --bs=4k --ioengine=libaio --iodepth=64 --numjobs=4 \
    --direct=1 --runtime=300 --time_based --group_reporting \
    --percentile_list=50:90:99:99.9

# 示例：fsync 提交延迟
fio --name=fsync_lat --directory=/volume1/fio --size=4G \
    --rw=write --bs=4k --iodepth=1 --fsync=1 --direct=1 \
    --runtime=60 --time_based
```

RAID 对比：同一矩阵分别在 RAID0/1/5/6/10（或 Basic）上执行，记录 **RAID 效率 = RAID 吞吐 ÷ 单盘吞吐 ÷ 盘数**（顺序写场景）。
若为 md 软阵列，确认奇偶校验计算未成为瓶颈：RAID5/6 写入时观察 CPU 占用。

#### 5.3.3 文件系统与元数据

```bash
# 元数据（创建/stat/删除），btrfs/ext4 对比
mdtest -d /volume1/fio -n 10000 -i 3 -F -C -T -r

# 小文件场景：1 万个 4K~64K 文件写入 + 遍历（脚本计时）
# filebench 文件服务器模型（多线程混合元数据+小 IO）
filebench -f fileserver.f   # 工作负载见附录
```

#### 5.3.4 SSD 缓存层（如有）

- 缓存命中：测试集 < 缓存容量，重复第 2 轮测加速上限；
- 缓存失效：测试集 ≫ 缓存容量，测穿透到机械盘的真实性能；
- write-back 与 write-through（如有选项）分别测写性能；
- **必测**：缓存盘失效/移除后卷是否可正常读写（可用性验证）。

#### 5.3.5 RAID 重建

```bash
# 记录重建全程耗时与 IO 性能衰减
cat /proc/mdstat                      # 重建进度与速度
echo check > /sys/block/md0/md/sync_action   # 校验
# 重建进行中，同时在卷上跑 fio 混合负载，对比正常时 IOPS 衰减比例
```

**记录**：各 RAID 级别吞吐/IOPS/延迟、RAID 效率、重建时长与带宽、重建期间性能衰减 %、SSD 缓存内外两段速度。

---

### 5.4 网络层性能

> 注意：弱 ARM 核上 iperf3 单实例可能先于网卡饱和。10GbE 测试建议起多个 iperf3 实例（不同端口）或用 nuttcp。

**测试项：**

1. TCP 单流 / 多流吞吐（双向）；
2. UDP 64 字节小包 PPS（考验 ARM 网络栈与中断处理）；
3. 请求-响应延迟（netperf TCP_RR）；
4. LACP 聚合的多流叠加效率；
5. MTU 9000 巨型帧收益；
6. 加密隧道吞吐：WireGuard / OpenVPN / IPsec（ARM 加密指令的实战体现）。

```bash
# NAS 侧多实例服务端
iperf3 -s -p 5201 & iperf3 -s -p 5202 & iperf3 -s -p 5203 & iperf3 -s -p 5204 &

# 客户端：单流
iperf3 -c <NAS_IP> -p 5201 -t 60 -i 5
# 客户端：多实例聚合（4 × 单流）
for p in 5201 5202 5203 5204; do iperf3 -c <NAS_IP> -p $p -t 60 & done; wait
# 反向（NAS → 客户端）
iperf3 -c <NAS_IP> -p 5201 -R -t 60
# UDP 小包 PPS
iperf3 -c <NAS_IP> -u -l 64 -b 0 -t 30

# 请求-响应延迟
netperf -H <NAS_IP> -t TCP_RR -l 30 -- -r 1,1

# WireGuard/OpenVPN 加密吞吐（建好隧道后在隧道 IP 上重跑 iperf3）
```

**线速参考（TCP 理想值）**：1GbE ≈ 118 MB/s，2.5GbE ≈ 295 MB/s，5GbE ≈ 590 MB/s，10GbE ≈ 1180 MB/s。

**记录**：各速率/流数/方向吞吐（Gbps 与 MB/s）、UDP PPS、TCP_RR transactions/s、聚合叠加比、巨帧收益 %、隧道吞吐。

---

### 5.5 文件服务协议性能（NAS 核心项）

对每种协议执行统一负载矩阵：

| 负载 | 说明 |
|---|---|
| 大文件顺序读 | 4 × 4GB 文件，bs=1M |
| 大文件顺序写 | 同上 |
| 4K 随机读写 | 模拟数据库/虚拟机盘 |
| 小文件批量 | 1 万个 4K~64K 文件写入/读取/删除 |

**SMB：**

```bash
# 挂载（关闭客户端缓存，测真实服务端性能；rsize/wsize 1MB）
mount -t cifs //<NAS_IP>/share /mnt/smb \
  -o username=admin,vers=3.1.1,cache=none,rsize=1048576,wsize=1048576,hard
# 开启加密对比（ARM 关键点：加密开销通常显著大于 x86 AES-NI 平台）
mount ... -o ...,seal
# 挂载后跑 5.3.2 的 fio 矩阵（--directory=/mnt/smb，注意 direct=1 在 cifs 上不生效时改用大文件+多次测试并说明）
```

**NFS：**

```bash
mount -t nfs -o vers=4.1,rsize=1048576,wsize=1048576,nconnect=8 <NAS_IP>:/share /mnt/nfs
# 对比 vers=3 / vers=4.1；nconnect=8 前后
```

**FTP / WebDAV**：用脚本批量上传下载计时，或 lftp / cadaver 配合大文件计时。

**iSCSI**（块级，最能反映虚拟化场景）：

```bash
iscsiadm -m discovery -t st -p <NAS_IP>
iscsiadm -m node --login
mkfs.ext4 /dev/sdX && mount /dev/sdX /mnt/iscsi   # 或直接对 /dev/sdX 裸测
# 裸设备顺序/随机矩阵同 5.3.1；文件系统层矩阵同 5.3.2
```

**rsync / 备份类**：

```bash
# 增量与全量备份吞吐
time rsync -a --info=progress2 /data/src/ admin@<NAS_IP>:/volume1/backup/
```

**macOS Time Machine / Windows robocopy（真实客户端视角）**：

```bash
# Windows 客户端
robocopy C:\src \\NAS\share /E /MT:16 /R:0 /W:0 /NFL /NDL /NP
```

**记录**：每协议 × 每负载的吞吐/IOPS/延迟；SMB 加密开关对比；NFS v3/v4 对比；协议效率 = 协议吞吐 ÷ 同负载 iSCSI 或本地文件系统吞吐。

---

### 5.6 多用户并发与混合负载

**测试项：**

1. **并发扩展曲线**：1 / 2 / 4 / 8 客户端（或等效多进程）同时执行大文件顺序读写与 4K 随机混合负载，绘制聚合吞吐与 P99 延迟曲线，找拐点；
2. **文件服务器模型**：filebench fileserver（多线程元数据+小文件混合）；
3. **读写混合压力**：一半客户端读、一半写，观察相互干扰；
4. **用户数上限**：SMB/NFS 并发会话逐步增加到服务端拒绝或响应超时，记录最大稳定会话数。

```bash
# fileserver.f 示例（filebench）
set $dir=/volume1/fio
set $nfiles=10000
set $meandirwidth=20
set $filesize=cvar(type=cvar-gamma,parameters=mean:16384;gamma:1.5)
set $nthreads=32
set $iosize=1m
set $meanappendsize=16k
define fileset name=bigfileset,path=$dir
define process name=filereader,instances=1 {
  thread name=fileuser,memsize=10m,instances=$nthreads {
    fscreate; fsdelete; create; closefile; statfile; readfile; writefile;
  }
}
run 300
```

**记录**：并发数 × 聚合吞吐 × P99 延迟表；拐点位置；满载并发时 CPU/内存/网络占用（定位瓶颈）。

---

### 5.7 典型应用场景

| 场景 | 方法 | 记录 |
|---|---|---|
| 媒体转码 | `ffmpeg -i 4K样片.mkv -c:v libx264 -preset veryfast -c:a copy out.mkv -f null -`；检查 SoC 是否有可用硬件编解码（多数 ARM NAS SoC 仅硬解、无硬编） | 转码 fps / 是否实时（1080p≥25fps）、CPU 占用 |
| Docker | 容器启动耗时、`redis-benchmark`、容器内编译耗时 | 启动秒数、ops/s |
| 相册/媒体库索引 | 导入 1000 张照片，测缩略图/AI 识别生成速度 | 张/分钟、CPU 占用 |
| Time Machine / 备份 | TM 首次全量备份计时 | MB/s、总耗时 |
| 下载 | BT 多任务满速下载 + 校验 | 下载速度、磁盘写入是否瓶颈 |
| 数据库（可选） | iSCSI 上跑 sysbench mysql | TPS、P95 延迟 |

---

### 5.8 稳定性与持续性能（极限压测核心）

**测试项：**

1. **24h 综合满载**：同时运行 `stress-ng`（CPU）+ fio 持续写（存储）+ iperf3 多实例（网络），每小时采样一次吞吐/IOPS/温度/频率，绘制时间曲线。

```bash
# NAS 侧组合压力（示例）
stress-ng --cpu $(nproc) --cpu-method all --timeout 24h &
fio --name=soak --directory=/volume1/fio --size=64G --rw=randrw --rwmixread=70 \
    --bs=4k --ioengine=libaio --iodepth=32 --numjobs=4 --direct=1 \
    --runtime=86400 --time_based &
# 客户端对 NAS 持续打流 24h（iperf3 -t 86400，多实例）
```

2. **温度与降频**：全程记录 CPU 温度、频率、吞吐，判定：是否降频、降频后性能衰减比例、是否出现热振荡（反复升降）。
3. **重建叠加压力**：在 24h 压力中触发拔盘重建，记录重建时长与业务 IO 衰减（见 5.3.5）。
4. **满容量衰减**：卷填充至 ≥ 90% 后重测顺序写与 4K 随机写，对比空卷性能（机械盘内圈衰减、SSD GC 压力、btrfs 空间分配都会体现）。
5. **内存泄漏 / 资源爬升**：长跑期间定时记录 `free -m`、进程 RSS、连接数、fd 数，观察是否单调爬升。
6. **异常恢复（可选）**：压测中直接断电，重启后检查文件系统一致性、RAID 状态、数据完整性（md5 校验样本）。

**记录**：每小时采样表、温度-频率-性能三联曲线、重建叠加曲线、满容量前后对比、异常事件清单。

**通过标准建议**：24h 内吞吐波动 < 10%；无因过热导致的持续降频（或降频后性能衰减 < 15%）；无服务崩溃/重启；断电后可正常恢复。

---

### 5.9 功耗与能效

用计量插座测整机功耗：

| 状态 | 说明 |
|---|---|
| 空闲 | 硬盘休眠后 10 分钟读数 |
| 硬盘工作空闲 | 盘转、无 IO |
| 单维满载 | 仅 CPU / 仅 IO / 仅网络打满 |
| 综合满载 | 24h 压测状态 |

**记录**：各状态瓦数；能效 = 吞吐（MB/s）÷ 整机功耗（W），用于与 x86 方案对比——这通常是 ARM NAS 的核心卖点，必须量化。

---

## 6. 测试执行 Checklist

**测试前**
- [ ] 基线信息表（3.2）填写完整，固件为发布版本
- [ ] 客户端自检通过，确认非瓶颈
- [ ] CPU governor 设为 performance，后台任务清理
- [ ] 工具版本记录（fio/iperf3 版本影响结果可比性）
- [ ] 磁盘 SMART 初始状态记录

**测试中**
- [ ] 每项热身 30~60s，正式 ≥ 3 轮取中位数
- [ ] direct IO + 测试文件 > 内存（除缓存专项外）
- [ ] 温度/频率/资源占用全程监控并记录
- [ ] 特性开关（加密/压缩/快照）对比遵循单一变量

**测试后**
- [ ] 测试数据清理，配置恢复
- [ ] SMART 复查（长跑后有无新增坏块/告警）
- [ ] 原始输出归档（每项保留 fio/iperf3 原始输出文件）

---

## 7. 测试记录模板

### 存储（每行一个用例 × RAID 级别）

| RAID | 用例 | bs/QD/jobs | 吞吐 (MB/s) | IOPS | P50 延迟 | P99 延迟 | CPU % | 备注 |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |

### 协议（每行一个协议 × 负载）

| 协议 | 负载 | 吞吐 (MB/s) | IOPS | P99 延迟 | CPU % | 加密 | 备注 |
|---|---|---|---|---|---|---|---|
| | | | | | | | |

### 并发扩展

| 客户端数 | 聚合吞吐 | P99 延迟 | CPU % | 内存 | 网络 % | 备注 |
|---|---|---|---|---|---|---|
| | | | | | | |

### 24h 稳定性（每小时一行）

| 时间 | 吞吐 | IOPS | CPU 温度 | CPU 频率 | 功耗 | 异常事件 |
|---|---|---|---|---|---|---|
| | | | | | | |

---

## 8. 结果评判参考基线

> 以下为建议判读标准，具体阈值应结合设备定位（家用/SOHO/企业级）与网卡档位调整。

| 指标 | 优秀 | 良好 | 及格 | 差 |
|---|---|---|---|---|
| 网络利用率（协议吞吐 ÷ 线速） | ≥ 95% | 85~95% | 70~85% | < 70% |
| SMB 加密性能损失 | < 20% | 20~40% | 40~60% | > 60% |
| RAID 顺序写效率（÷盘数） | ≥ 90% | 75~90% | 60~75% | < 60% |
| 文件系统层损耗（vs 裸盘） | < 15% | 15~30% | 30~50% | > 50% |
| 24h 吞吐波动 | < 10% | 10~20% | 20~30% | > 30% |
| 热降频 | 无降频 | 降频但衰减 < 15% | 衰减 15~30% | 持续降频/热振荡 |
| 重建期间业务 IO 衰减 | < 30% | 30~50% | 50~70% | > 70% |
| 并发拐点 | ≥ 8 客户端 | 4~8 | 2~4 | < 2 |
| 满容量性能衰减 | < 10% | 10~25% | 25~40% | > 40% |

ARM 平台特别关注的"红旗"信号：

- SMB 开启加密后吞吐腰斩以上 → AES 硬件指令未启用，检查 `/proc/crypto`；
- 多流打不满网卡但 CPU 已 100% → 协议栈/中断处理瓶颈，确认网卡多队列与中断亲和性；
- 长跑 1~2 小时后性能阶梯式下降 → 热降频或 SSD 缓外/GC，对照温度曲线定位；
- 小文件 IOPS 远低于裸盘 → 文件系统/RAID 元数据开销异常，检查 btrfs 配置或 md 参数。

---

## 9. 附录：常用命令速查

```bash
# ── 系统信息 ──
uname -a; lscpu; free -h; getconf PAGESIZE
cat /proc/crypto | grep -E 'name|driver|module' | head -30
cat /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq | sort -u   # 判断大小核
cat /sys/class/thermal/thermal_zone*/temp; sensors

# ── CPU ──
sysbench cpu --threads=1 run
sysbench cpu --threads=$(nproc) run
openssl speed -seconds 5 -evp aes-256-gcm
7zz b -mmt=$(nproc)

# ── 内存 ──
sysbench memory --threads=$(nproc) --memory-block-size=1M --memory-total-size=200G run
OMP_NUM_THREADS=$(nproc) ./stream

# ── 存储 ──
fio --name=seq_w --directory=/volume1/fio --size=16G --rw=write --bs=1M \
    --ioengine=libaio --iodepth=32 --numjobs=4 --direct=1 --group_reporting
fio --name=randmix --directory=/volume1/fio --size=16G --rw=randrw --rwmixread=70 \
    --bs=4k --ioengine=libaio --iodepth=64 --numjobs=4 --direct=1 \
    --runtime=300 --time_based --group_reporting --percentile_list=50:90:99:99.9
mdtest -d /volume1/fio -n 10000 -i 3 -F -C -T -r

# ── 网络 ──
iperf3 -s -p 5201
iperf3 -c <NAS_IP> -p 5201 -t 60            # 单流
iperf3 -c <NAS_IP> -p 5201 -R -t 60         # 反向
iperf3 -c <NAS_IP> -u -l 64 -b 0 -t 30      # UDP 小包
netperf -H <NAS_IP> -t TCP_RR -l 30 -- -r 1,1

# ── 协议挂载 ──
mount -t cifs //<NAS_IP>/share /mnt/smb -o username=admin,vers=3.1.1,cache=none,rsize=1048576,wsize=1048576
mount -t nfs -o vers=4.1,rsize=1048576,wsize=1048576,nconnect=8 <NAS_IP>:/share /mnt/nfs
iscsiadm -m discovery -t st -p <NAS_IP> && iscsiadm -m node --login

# ── 压测与监控 ──
stress-ng --cpu $(nproc) --vm 2 --vm-bytes 512M --timeout 24h
watch -n 1 'grep MHz /proc/cpuinfo'
cat /proc/mdstat                             # RAID 状态/重建进度
```

---

*完 —— 执行时请按 §6 Checklist 逐项核对，所有原始输出归档，保证结果可复现。*
