# nastest — ARM-NAS 极限性能测试框架

依据 `docs/ARM-NAS-极限性能测试方案.md` 实现的 NAS 极限性能基准测试框架:
覆盖 **峰值 / 持续(浸泡)/ 极限容量** 三层次,回答方案的四个核心问题(瓶颈在哪一层、
长跑是否衰减、并发拐点、加密开销),并按方案 §8 输出 优/良/及格/差 评级与 ARM 红旗检测。

## 特性

- **自动化优先**:89 条用例中 83 条可自动执行;设备端(SSH)与本机客户端双端驱动,
  每条命令的完整日志落盘(`stdout.log`),指标存 SQLite,原始输出(fio JSON 等)存证据目录。
- **单用例可点击执行**:任意用例可随时单独(重)跑;自动阶段之后人工用例统一归集,
  附操作指导;人工可录入数值(S9 功耗)与判定结论。
- **域顺序运行**:每个小结(S0~S9)头部有「▶ 顺序运行本域」按钮,一键依次执行
  该域所有可执行子测试(自动跳过运行中/高危/纯人工项),适合补测或重测某一层。
- **S3 按 RAID 子组运行**:存储子系统分为 **basic / RAID0 / RAID1 / RAID5 / RAID6 / RAID10**
  六个子组,每组一个独立「▶ 顺序运行本组」按钮。把设备阵列切换为对应 RAID 级别后
  运行相应组即可;执行前框架自动校验 `/proc/mdstat` 阵列级别,**与组不符会拒跑**
  (防止数据串组)。basic 组(裸盘读基线 + 全盘并发裸盘读/写)与级别无关。
  报告为每个已测 RAID 组各出一张子表对比。
- **存储卷免填写**:新建任务的「存储卷路径」可留空。环境检查或首次执行时会自动探测
  数据卷(优先 `/vol1`、`/vol2` 等 fnOS 卷,取剩余空间最大者;无则回退最大的非系统
  btrfs/ext4/xfs 挂载点),测试目录自动取 `<卷>/.nastest_perf`,并持久化到任务配置。
  若已填的卷**已不是挂载点**(如重建阵列后卷路径变化),会自动切换到探测到的数据卷并
  在日志显著提示,避免把测试误做到系统盘上。
- **任务管理**:任务列表与任务详情页均有「删除」按钮,确认后连同测试结果与证据目录
  一并删除;仍有运行中用例的任务不允许直接删除(需先中止并等待当前用例退出)。
- **测试纪律(方案 §3.3)**:每用例 ≥N 轮取中位数、方差 >10% 标记、热身、
  测试文件大于内存、direct=1、governor 锁定 performance 检查、aes-ce 检查。
- **浸泡实时监控**:S8-01 后台压测 + 每 X 秒采样(温度/频率/CPU/IOPS/带宽/内存)
  写 `soak.csv`,界面实时表格轮询。
- **报告(方案 §7/§8)**:基线卡 → 四问答 → 存储表 → 协议表 → 并发表 → 浸泡小时表
  → §8 评级表 → 红旗清单 → 人工项指引 → 证据位置;Markdown 下载 + HTML 在线查看。
  存储/协议/并发/浸泡各表后附「测试方法 · 底层原理 · 使用的命令」说明块,
  附录用例明细表也带测试方法列。

## 运行

```bash
cd nastest
pip install -r requirements.txt   # fastapi uvicorn jinja2 paramiko smbprotocol httpx
python -m app.main                # 打开 http://127.0.0.1:8080
```

## 用例矩阵(S0~S9,共 89 条)

| 域 | 内容 |
|---|---|
| S0 环境与基线 | 基线采集 / governor 锁定 / 后台负载审计 / 客户端自检 |
| S1 CPU | sysbench / openssl 加密 / pigz 压缩 / CoreMark |
| S2 内存 | STREAM(官方或内置等价实现)/ sysbench memory |
| S3 存储 | **basic**(裸盘读基线 + 全盘并发裸盘读/写,写为破坏性操作)+ **RAID0/1/5/6/10 五组**,每组:fio 矩阵 9 条(多流顺序读写/4K 随机/混合/QD1 延迟/fsync/单流顺序读写)+ 元数据,共 53 条 |
| S4 网络 | iperf3 TCP 上/下行、多流聚合、UDP PPS、MTU9000 对比、WireGuard(人工) |
| S5 协议 | SMB 纯软件 / SMB 加密对比 / SMB 挂载 fio / NFS 挂载 fio / FTP / WebDAV / iSCSI(人工)/ rsync / SMB 小文件 |
| S6 并发 | 并发扩展写/读(找拐点)/ 读写混合 |
| S7 应用 | ffmpeg 转码 / Docker / 应用层场景(人工) |
| S8 稳定性 | 综合满载浸泡(实时采样)/ 衰减与降频分析 / 满容量衰减(高危)/ 断电恢复(人工) |
| S9 功耗 | 功率计读数人工录入 → 自动计算 MB/s/W |

高危用例(S3-BASE-04 / S8-03 / S8-04)需输入用例编号确认后才执行,自动阶段会跳过。
S3 的 RAID 子组不进入自动阶段(防止阵列级别与用例组不符),由人工切换阵列后按组手动运行。

## 快速验证配置(冒烟,约 20 分钟)

新建任务时:

```
rounds=1  fio_runtime=10  fio_size=512M  warmup_seconds=3
soak_minutes=3  soak_sample_interval=15  smb_bigfile_mb=256
```

## 完整验收配置

```
rounds=3  fio_runtime=60  fio_size=16G(> 设备内存,排除缓存)  warmup_seconds=30
soak_minutes=1440(24h)+  soak_sample_interval=60  smb_bigfile_mb=4096
```

## 本机客户端全自动(可选)

运行配置中填写 **本机 sudo 密码**(`local_sudo_pass`)后,框架自动:
apt 安装本机 iperf3 / fio / cifs-utils / nfs-common → SMB/NFS 挂载跑 fio → 改 MTU 测巨帧。
不填则相关用例(S0-04 / S4-01~05 / S5-03/04)以 `blocked` 状态附人工指引,不影响其余用例。

## 数据与安全

- 数据库 `data/nastest.db`(chmod 600,内含明文密码 —— 仅本地测试台使用,**勿外传**);
  API 回显配置时 `ssh_pass / smb_pass / local_sudo_pass` 均掩码为 `***`。
- 证据目录 `data/evidence/run_<id>/cases/<用例编号>/`:`stdout.log`(全部命令与输出)、
  `metrics.json`(指标)、`fio_*_raw.txt`、`soak.csv`、`baseline.md` 等。

## 目录结构

```
app/
  main.py       FastAPI 入口与 API
  config.py     域序/依赖包清单/默认运行配置
  registry.py   89 条用例定义(steps/expected/guide/impl/hazard/tier/sub)
  raid.py       S3 子组公共工具(组识别/编号规则/评级级别挑选)
  migrate.py    启动时一次性数据迁移(旧 S3 编号 → 子组编号,幂等)
  executor.py   调度:域序自动阶段 → 人工阶段归集;单用例/域/子组执行;依赖检查
  caseimpl.py   实现聚合(ctx + impl_env/impl_stor/impl_proto/impl_app)
  ctx.py        Ctx:sh/sh_local/put_file/fio profile/iperf3/SMB/采样助手
  preflight.py  设备+本机环境检查、双端 apt 自动安装
  parse.py      fio --output=json / iperf3 -J 解析
  sampler.py    周期采样器(soak.csv + 实时样本)
  scorer.py     §8 评级 + 四问答 + 红旗检测
  reporter.py   §7 报告(report.md + HTML 上下文)
  db.py sshc.py evidence.py
templates/ static/   Web 界面(轮询、单用例点击、实时采样表、人工录入表单)
vendor/triad.c       官方 stream.c 下载失败时的内存带宽等价实现
```
