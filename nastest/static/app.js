/* ARM-NAS 极限性能测试前端:状态轮询、执行控制、用例详情、人工录入、soak 实时样本 */
const STATUS_TXT = {
  pass: '✅ 完成', fail: '❌ 失败', skipped: '⏭ 跳过', blocked: '🚫 受阻',
  pending: '⬜ 未测', running: '🔄 运行中', manual_pending: '🙋 待人工',
};
const TIER_TXT = {peak: '峰', sustained: '持', breaking: '破'};
const CASE_BY_ID = Object.fromEntries(CASES.map(c => [c.id, c]));
// 人工数值录入表单(POST /cases/{id}/metrics)
const METRIC_FORMS = {
  'S9-01': [['watts_idle', '空闲功率(W)'], ['watts_disk_idle', '盘转空闲功率(W)'],
            ['watts_single', '单维满载功率(W)'], ['watts_full', '综合满载功率(W)']],
  'S3-BASE-02': [['rebuild_io_degrade_pct', '重建期间业务 IO 衰减(%)']],
};
// S3 六个子组:basic + 五种 RAID 级别(人工切换阵列后运行对应组)
const S3_SUBS = ['basic', 'RAID0', 'RAID1', 'RAID5', 'RAID6', 'RAID10'];
const S3_SUB_DESC = {
  basic: '与 RAID 级别无关:裸盘读基线 + RAID 重建监控',
  RAID0: '条带 · 无冗余,峰值读写',
  RAID1: '镜像 · 读可并行,写=单盘',
  RAID5: '分布式奇偶校验 · ≥3 盘,允许坏 1 盘',
  RAID6: '双奇偶校验 · ≥4 盘,允许坏 2 盘',
  RAID10: '镜像+条带 · ≥4 盘,高 IOPS',
};
let STATE = null;
let OPEN_CASE = null;
let pollTimer = null;
let soakTimer = null;

// ---------- 基础 ----------
async function api(url, method = 'POST', body) {
  const opt = {method};
  if (body !== undefined) {
    opt.headers = {'Content-Type': 'application/json'};
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(url, opt);
  let data = null;
  try { data = await r.json(); } catch (e) {}
  if (!r.ok) {
    const msg = (data && (data.detail || data.message)) || r.statusText;
    throw new Error(msg);
  }
  return data;
}

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
}
const fmt = (v, u = '') => (v === undefined || v === null) ? '—' : v + u;

function keyMetric(c, m) {
  if (!m) return '';
  // S3 RAID 子组:同一套 fio 矩阵,指标键相同,按组内序号区分
  if (c.id.startsWith('S3-RAID')) {
    switch (c.id.slice(-2)) {
      case '01': return fmt(m.seq_read_r_mbps, 'MB/s');
      case '02': return fmt(m.seq_write_w_mbps, 'MB/s');
      case '03': return fmt(m.rand_read_r_iops, ' IOPS');
      case '04': return fmt(m.rand_write_w_iops, ' IOPS');
      case '05': return `混合 ${fmt(m.randrw_mixed_iops, ' IOPS')}`;
      case '06': return fmt(m.qd1_lat_r_lat_us, 'μs');
      case '07': return `fsync ${fmt(m.fsync_lat_fsync_lat_us, 'μs')}`;
      case '08': return `create ${fmt(m.files_s_create)} files/s`;
    }
    return '';
  }
  switch (c.id) {
    case 'S1-01': return `单核 ${fmt(m.cpu_events_s_single)} / 多核 ${fmt(m.cpu_events_s_multi)} ev/s`;
    case 'S1-02': return `AES-128-GCM ${fmt(m['openssl_aes-128-gcm_mb_s'], 'MB/s')}`;
    case 'S1-03': return `压缩 ${fmt(m.compress_mb_s, 'MB/s')} 解压 ${fmt(m.decompress_mb_s, 'MB/s')}`;
    case 'S1-04': return `单核 ${fmt(m.coremark_single)} / 多核 ${fmt(m.coremark_multi)}`;
    case 'S2-01': return `Triad ${fmt(m.stream_triad_gb_s, 'GB/s')}`;
    case 'S2-02': return fmt(m.mem_bw_mib_s, 'MiB/s');
    case 'S3-BASE-01': return fmt(m.raw_read_mb_s, 'MB/s');
    case 'S3-BASE-02': return `${fmt(m.rebuild_minutes, 'min')} ${fmt(m.rebuild_avg_speed_mb_s, 'MB/s')}`;
    case 'S4-01': return `${fmt(m.tcp_tx_mbps, 'Mbps')} 利用率 ${fmt(m.link_util_pct, '%')}`;
    case 'S4-02': return fmt(m.tcp_rx_mbps, 'Mbps');
    case 'S4-03': return fmt(m.tcp_multi_mbps, 'Mbps');
    case 'S4-04': return `${fmt(m.udp_pps, 'pps')} 丢包 ${fmt(m.udp_lost_pct, '%')}`;
    case 'S4-05': return `${fmt(m.mtu1500_mbps)}→${fmt(m.mtu9000_mbps)}Mbps(${fmt(m.mtu_gain_pct, '%')})`;
    case 'S5-01': return `写 ${fmt(m.write_mbps)} / 读 ${fmt(m.read_mbps)} MB/s`;
    case 'S5-02': return `损失 写${fmt(m.encrypt_loss_write_pct, '%')} 读${fmt(m.encrypt_loss_read_pct, '%')}`;
    case 'S5-03': return `写 ${fmt(m.seq_write_w_mbps)} / 读 ${fmt(m.seq_read_r_mbps)} MB/s`;
    case 'S5-04': return `写 ${fmt(m.seq_write_w_mbps)} / 读 ${fmt(m.seq_read_r_mbps)} MB/s`;
    case 'S5-05': return `上 ${fmt(m.upload_mbps)} / 下 ${fmt(m.download_mbps)} MB/s`;
    case 'S5-06': return `PUT ${fmt(m.upload_mbps)} / GET ${fmt(m.download_mbps)} MB/s`;
    case 'S5-08': return fmt(m.rsync_mbps, 'MB/s');
    case 'S5-09': return `create ${fmt(m.small_create_files_s)} files/s`;
    case 'S6-01': return `拐点 N=${fmt(m.write_inflection_n)}`;
    case 'S6-02': return `拐点 N=${fmt(m.read_inflection_n)}`;
    case 'S6-03': return `读/写各 ${fmt(m.mixed_read_mbps, 'MB/s')}`;
    case 'S7-01': return `${fmt(m.transcode_fps, ' fps')} (${fmt(m.encoder, '')})`;
    case 'S7-02': return fmt(m.container_start_s, 's');
    case 'S8-01': return `${fmt(m.soak_minutes_actual, 'min')} ${fmt(m.samples, '样本')} 最高 ${fmt(m.max_temp_c, '℃')}`;
    case 'S8-02': return `波动 ${fmt(m.soak_variance_pct, '%')}${m.throttled ? ' ⚠降频' : ''}`;
    case 'S8-03': return `衰减 ${fmt(m.seq_write_degrade_pct, '%')}(容量 ${fmt(m.used_pct_full, '%')})`;
    case 'S9-01': return m.watts_full !== undefined ? `满载 ${m.watts_full}W ${fmt(m.efficiency_mb_s_per_w, 'MB/s/W')}` : '';
    default: return '';
  }
}

// ---------- 初始化 ----------
function caseRow(c) {
  const tr = el('tr');
  tr.id = 'row-' + c.id;
  tr.innerHTML = `
    <td class="mono">${c.id}</td>
    <td>${c.hazard === 'critical' ? '<span class="hazard" title="高危:需输入编号确认">⚠</span>' : ''}${esc(c.title)}</td>
    <td class="small" title="${c.tier}">${TIER_TXT[c.tier] || ''}</td>
    <td class="small">${c.mode === 'auto' ? '自动' : (c.has_impl ? '人工+校验' : '纯人工')}</td>
    <td id="st-${c.id}" class="small">⬜ 未测</td>
    <td id="km-${c.id}" class="small mono"></td>
    <td id="op-${c.id}"></td>`;
  tr.addEventListener('click', (ev) => { if (!ev.target.closest('button')) openCase(c.id); });
  return tr;
}

const CASE_THEAD = '<thead><tr><th style="width:110px">编号</th><th>测试项</th><th style="width:40px">层次</th><th style="width:80px">方式</th><th style="width:100px">状态</th><th style="width:230px">关键指标</th><th style="width:150px">操作</th></tr></thead>';

function buildDomains() {
  const wrap = document.getElementById('domains');
  wrap.innerHTML = '';
  for (const dom of DOMAIN_ORDER) {
    const sec = el('div', 'domain');
    if (dom === 'S3') { buildS3(sec); wrap.appendChild(sec); continue; }
    sec.innerHTML = `<h3 class="domain-title" id="dt-${dom}">${dom} ${esc(DOMAIN_NAMES[dom])} <span class="muted small" id="ds-${dom}"></span></h3>`;
    const dbtn = el('button', 'btn btn-sm domain-run', '▶ 顺序运行本域');
    dbtn.title = '依次运行本域所有子测试(自动跳过:高危项/纯人工项/运行中的项)';
    dbtn.onclick = () => runDomain(dom);
    sec.querySelector('.domain-title').appendChild(dbtn);
    const tbl = el('table', 'table case-table');
    tbl.innerHTML = CASE_THEAD;
    const tb = el('tbody');
    tb.id = 'tb-' + dom;
    for (const c of CASES.filter(x => x.domain === dom)) tb.appendChild(caseRow(c));
    tbl.appendChild(tb);
    sec.appendChild(tbl);
    wrap.appendChild(sec);
  }
}

// S3 按子组渲染:basic + RAID0/1/5/6/10,每组一个独立「顺序运行」按钮
function buildS3(sec) {
  sec.innerHTML = `<h3 class="domain-title" id="dt-S3">S3 ${esc(DOMAIN_NAMES['S3'])} <span class="muted small" id="ds-S3"></span></h3>
    <div class="banner-info small">S3 按 RAID 子组运行:把设备阵列切换为对应 RAID 级别后,点该组的「▶ 顺序运行本组」。
    执行前框架自动校验 <span class="mono">/proc/mdstat</span> 阵列级别,与组不符会拒跑(防止数据串组);basic 组与 RAID 级别无关。</div>`;
  for (const sub of S3_SUBS) {
    const box = el('div', 'subgroup');
    const label = sub === 'basic' ? 'basic(公共基线)' : `${sub} 组`;
    box.innerHTML = `<h4 class="subgroup-title">${label}
      <span class="muted small">${esc(S3_SUB_DESC[sub] || '')}</span>
      <span class="muted small" id="ss-${sub}"></span></h4>`;
    const btn = el('button', 'btn btn-sm domain-run', '▶ 顺序运行本组');
    btn.title = sub === 'basic'
      ? '运行 basic 组可自动执行的用例(裸盘读基线)'
      : `确认阵列已切换为 ${sub} 后点击,依次运行本组全部用例(执行前自动校验阵列级别)`;
    btn.onclick = () => runSub(sub);
    box.querySelector('.subgroup-title').appendChild(btn);
    const tbl = el('table', 'table case-table');
    tbl.innerHTML = CASE_THEAD;
    const tb = el('tbody');
    tb.id = 'tb-S3-' + sub;
    for (const c of CASES.filter(x => x.domain === 'S3' && x.sub === sub)) tb.appendChild(caseRow(c));
    tbl.appendChild(tb);
    box.appendChild(tbl);
    sec.appendChild(box);
  }
}

// ---------- 渲染 ----------
function renderControls() {
  const run = STATE.run, st = run.status, ph = run.phase;
  document.getElementById('run-title').textContent = `任务 #${run.id} · ${run.name}`;
  const statusEl = document.getElementById('run-status');
  statusEl.textContent = {pending: '待开始', running: '运行中', paused: '已暂停', completed: '已完成', aborted: '已中止'}[st] || st;
  statusEl.className = 'badge st-run-' + st;
  document.getElementById('run-phase').textContent =
    {created: '已创建', preflight: '环境检查', auto: '自动测试阶段', manual: '人工测试阶段', report: '报告'}[ph] || ph;
  const cfg = run.config;
  document.getElementById('cfg-summary').textContent =
    `${cfg.device_model} · ${cfg.device_ip} · ${cfg.disk_desc || ''} ${cfg.raid_level} · 卷 ${cfg.volume_path} · ` +
    `协议 ${(cfg.protocols || []).join('/')} · rounds=${cfg.rounds} fio=${cfg.fio_size}/${cfg.fio_runtime}s · ` +
    `浸泡 ${cfg.soak_minutes}min · 线速 ${cfg.nic_link_mbps}Mbps`;

  const running = st === 'running';
  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-start').textContent = (st === 'paused' || ph === 'manual' || ph === 'auto') ? '▶ 继续执行' : '▶ 开始自动测试';
  document.getElementById('btn-pause').style.display = running && ph === 'auto' ? '' : 'none';
  document.getElementById('btn-abort').style.display = (running || st === 'paused') ? '' : 'none';

  const c = STATE.counts || {};
  const total = STATE.total;
  const done = (c.pass || 0) + (c.fail || 0) + (c.skipped || 0);
  const seg = (n, cls) => n ? `<span class="seg ${cls}" style="flex:${n}" title="${n}"></span>` : '';
  document.getElementById('progress').innerHTML = `
    <div class="progressbar">
      ${seg(c.pass || 0, 'p-pass')}${seg(c.fail || 0, 'p-fail')}${seg(c.skipped || 0, 'p-skip')}
      ${seg(c.blocked || 0, 'p-block')}${seg(c.running || 0, 'p-running')}${seg((c.manual_pending || 0) + (c.pending || 0), 'p-rest')}
    </div>
    <div class="small muted">完成 ${done}/${total}(${STATE.score.completion_pct}%) ·
    ✅${c.pass || 0} ❌${c.fail || 0} ⏭${c.skipped || 0} 🚫${c.blocked || 0} 🙋${c.manual_pending || 0} ⬜${c.pending || 0} 🔄${c.running || 0}</div>`;

  // §8 评级概览
  const gc = {优: 0, 良: 0, 及格: 0, 差: 0};
  let graded = 0;
  for (const g of STATE.score.grades || []) {
    if (gc[g.grade] !== undefined) { gc[g.grade]++; graded++; }
  }
  document.getElementById('grade-line').innerHTML = graded
    ? `§8 评级(已评 ${graded}/${STATE.score.grades.length}):` +
      `<span class="grade grade-优">优 ${gc['优']}</span> <span class="grade grade-良">良 ${gc['良']}</span> ` +
      `<span class="grade grade-及格">及格 ${gc['及格']}</span> <span class="grade grade-差">差 ${gc['差']}</span>` +
      `<a href="/runs/${RUN_ID}/report" style="margin-left:8px">评级明细 →</a>`
    : '§8 评级:暂无可评指标(完成用例后自动生成)';

  const mb = document.getElementById('manual-banner');
  if (ph === 'manual') {
    mb.style.display = '';
    mb.innerHTML = STATE.manual_done
      ? '🎉 人工用例已全部判定,可生成测试报告。'
      : `🙋 自动阶段结束。剩余人工用例 ${c.manual_pending || 0} 条:点击用例行查看操作指导,完成后提交判定` +
        `(S9-01 功率、S3-BASE-02 重建衰减可在详情中录入数值;无法完成的项可标记"跳过"并说明)。`;
  } else mb.style.display = 'none';
}

function renderPreflight() {
  const pf = STATE.run.preflight || {};
  const out = document.getElementById('preflight-out');
  if (!pf.time) {
    out.innerHTML = '<span class="muted">尚未检查。开测前请先运行环境检查(设备连通 / sudo / 依赖 / 磁盘空间 / aes-ce / 本机工具)。</span>';
    document.getElementById('btn-install').style.display = 'none';
    document.getElementById('btn-install-local').style.display = 'none';
    document.getElementById('install-log').style.display = 'none';
    document.getElementById('install-local-log').style.display = 'none';
    return;
  }
  const d = pf.device || {};
  let html = `检查时间 ${pf.time}<br>`;
  html += d.reachable
    ? `✅ 设备连通 (${esc(d.uname || '')}) · sudo: ${d.sudo ? '✅ 可用' : '❌ 不可用' + (d.sudo_error ? '(' + esc(d.sudo_error) + ')' : '')} · governor: ${esc(d.governor || '?')} · aes-ce: ${d.aes_ce ? '✅' : '❌红旗'}<br>`
    : `❌ 设备不可达: ${esc(d.error || '')}<br>`;
  if (d.reachable && d.volume_path) {
    html += `存储卷: <span class="mono">${esc(d.volume_path)}</span>` +
      (d.volume_detected ? '(自动探测,如需更改请在任务配置中填写)' : '') +
      (d.volume_warning ? ` <span class="warn">⚠️ ${esc(d.volume_warning)}</span>` : '') +
      (d.volume_error ? ` <span class="warn">⚠️ ${esc(d.volume_error)}</span>` : '') + '<br>';
  }
  if (d.packages) {
    html += '设备端工具: ' + Object.entries(d.packages).map(([k, v]) => `${k} ${v ? '✅' : '❌缺失'}`).join(' · ');
    if (d.free_gb !== undefined) {
      html += `<br>磁盘空间: ${d.free_gb}GB 可用 / 需 ≈${d.need_gb}GB ${d.disk_free_ok ? '✅' : '❌ ' + esc(d.disk_error || '')}`;
    }
    html += '<br>';
  }
  if (pf.local) {
    html += '本机工具: ' + Object.entries(pf.local).filter(([k]) => k !== 'sudo_ok' && k !== 'sudo_error')
      .map(([k, v]) => `${k} ${v ? '✅' : '⚠️缺失'}`).join(' · ');
    html += pf.local.sudo_ok ? ' · 本机 sudo ✅' : ` · 本机 sudo ❌(${esc(pf.local.sudo_error || '')})`;
    if ((pf.missing_local || []).length) {
      html += `<br><span class="warn">本机缺失 ${pf.missing_local.join(', ')}:填了本机 sudo 密码可一键安装,` +
        `否则挂载/MTU 用例转人工指引</span>`;
    }
    html += '<br>';
  }
  out.innerHTML = html;
  document.getElementById('btn-install').style.display = (pf.missing_device || []).length ? '' : 'none';
  document.getElementById('btn-install-local').style.display =
    ((pf.missing_local || []).length && (pf.local || {}).sudo_ok) ? '' : 'none';
  for (const [key, elId] of [['install', 'install-log'], ['install_local', 'install-local-log']]) {
    const inst = pf[key] || {};
    const logEl = document.getElementById(elId);
    if (inst.log) {
      logEl.style.display = '';
      logEl.textContent = `【${key === 'install' ? '设备端' : '本机'}安装 ${inst.status}】\n` + inst.log;
      logEl.scrollTop = logEl.scrollHeight;
    }
  }
}

function renderCases() {
  const byId = Object.fromEntries(STATE.results.map(r => [r.case_id, r]));
  const domStats = {};
  const subStats = {};
  for (const c of CASES) {
    const r = byId[c.id] || {status: 'pending'};
    document.getElementById('st-' + c.id).innerHTML = STATUS_TXT[r.status] || r.status;
    document.getElementById('km-' + c.id).textContent = keyMetric(c, r.metrics) || '';
    document.getElementById('row-' + c.id).className = 'st-row-' + r.status + (OPEN_CASE === c.id ? ' row-open' : '');
    const op = document.getElementById('op-' + c.id);
    op.innerHTML = '';
    if (r.status === 'running') {
      op.appendChild(el('span', 'small muted', '运行中…'));
    } else if (c.mode === 'auto' || c.has_impl) {
      const label = c.mode === 'auto' ? '运行' : '运行校验';
      const b = el('button', 'btn btn-sm', label);
      b.onclick = (ev) => { ev.stopPropagation(); runCase(c.id); };
      op.appendChild(b);
    }
    if (c.mode === 'manual') {
      const b2 = el('button', 'btn btn-sm', '详情/判定');
      b2.onclick = (ev) => { ev.stopPropagation(); openCase(c.id); };
      op.appendChild(b2);
    }
    domStats[c.domain] = domStats[c.domain] || {pass: 0, fail: 0, total: 0};
    domStats[c.domain].total++;
    if (r.status === 'pass') domStats[c.domain].pass++;
    if (r.status === 'fail') domStats[c.domain].fail++;
    if (c.domain === 'S3' && c.sub) {
      subStats[c.sub] = subStats[c.sub] || {pass: 0, fail: 0, total: 0};
      subStats[c.sub].total++;
      if (r.status === 'pass') subStats[c.sub].pass++;
      if (r.status === 'fail') subStats[c.sub].fail++;
    }
  }
  for (const dom of DOMAIN_ORDER) {
    const s = domStats[dom];
    if (s) document.getElementById('ds-' + dom).textContent = `(${s.pass}完成 / ${s.fail}失败 / ${s.total}项)`;
  }
  // S3:各子组进度 + 已有数据的组(报告中按组出子表)
  const tested = [];
  for (const sub of S3_SUBS) {
    const s = subStats[sub];
    const tag = document.getElementById('ss-' + sub);
    if (s && tag) tag.textContent = `(${s.pass}完成 / ${s.fail}失败 / ${s.total}项)`;
    if (s && s.pass > 0) tested.push(sub === 'basic' ? 'basic' : sub);
  }
  const ds3 = document.getElementById('ds-S3');
  if (ds3 && tested.length) ds3.textContent += ` · 已测组: ${tested.join(' / ')}`;
}

async function refresh() {
  try {
    STATE = await api('/api/runs/' + RUN_ID, 'GET');
  } catch (e) { return; }
  renderControls();
  renderPreflight();
  renderCases();
  if (OPEN_CASE) renderModal();
  const active = STATE.run.status === 'running' ||
    (STATE.run.preflight?.install?.status === 'running') ||
    (STATE.run.preflight?.install_local?.status === 'running') ||
    (STATE.counts.running || 0) > 0;
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refresh, active ? 3000 : 8000);
}

// ---------- 控制动作 ----------
async function doPreflightCheck() {
  try { await api(`/api/runs/${RUN_ID}/preflight/check`); await refresh(); }
  catch (e) { alert('环境检查失败: ' + e.message); }
}
async function doPreflightInstall() {
  if (!confirm('将在设备上执行 sudo apt-get install 安装缺失依赖,确认继续?')) return;
  await api(`/api/runs/${RUN_ID}/preflight/install`, 'POST', {});
  refresh();
}
async function doPreflightInstallLocal() {
  if (!confirm('将在本机执行 sudo apt-get install 安装 iperf3/fio/cifs-utils/nfs-common 等,确认继续?')) return;
  await api(`/api/runs/${RUN_ID}/preflight/install-local`, 'POST', {});
  refresh();
}
async function doStart() {
  try { await api(`/api/runs/${RUN_ID}/start`); refresh(); }
  catch (e) { alert('启动失败: ' + e.message); }
}
async function doPause() { await api(`/api/runs/${RUN_ID}/pause`); refresh(); }
async function doAbort() {
  if (!confirm('确认中止整个任务?')) return;
  await api(`/api/runs/${RUN_ID}/abort`); refresh();
}
async function doDelete() {
  const name = STATE?.run?.name || '';
  if (!confirm(`确认删除任务 #${RUN_ID}「${name}」?\n将同时删除其全部测试结果与证据目录(日志/fio 原始输出等),不可恢复。`)) return;
  try {
    await api(`/api/runs/${RUN_ID}`, 'DELETE');
    clearTimeout(pollTimer);
    if (soakTimer) clearInterval(soakTimer);
    location.href = '/';
  } catch (e) { alert('删除失败: ' + e.message); refresh(); }
}
async function genReport() {
  await api(`/api/runs/${RUN_ID}/report`);
  location.href = `/runs/${RUN_ID}/report`;
}

async function runCase(cid) {
  const c = CASE_BY_ID[cid];
  let body = {};
  if (c.hazard === 'critical') {
    const v = prompt(`⚠️ 高危操作:${c.title}\n\n${(c.guide || '').split('\n')[0]}\n\n请输入用例编号 ${cid} 确认执行:`);
    if (v !== cid) return;
    body.confirm = cid;
  }
  try {
    await api(`/api/runs/${RUN_ID}/cases/${cid}/run`, 'POST', body);
    refresh();
  } catch (e) { alert(e.message); }
}

async function runDomain(dom) {
  try {
    const r = await api(`/api/runs/${RUN_ID}/domains/${dom}/run`);
    const parts = [`▶ ${dom} 顺序运行已启动:共 ${r.queued.length} 项(${r.queued.join('、') || '无可执行项'})`];
    if (r.skipped && r.skipped.length) {
      parts.push(`跳过 ${r.skipped.length} 项:` + r.skipped.map(s => `${s.id}(${s.reason})`).join(';'));
    }
    alert(parts.join('\n'));
    refresh();
  } catch (e) { alert(e.message); }
}

// S3 子组顺序运行(basic / RAID0/1/5/6/10),执行前逐用例自动校验阵列级别
async function runSub(sub) {
  const tip = sub === 'basic' ? '' : `\n\n请确认设备阵列当前已是 ${sub}(不符的用例将被拒跑)。`;
  if (!confirm(`将顺序运行 S3 ${sub} 组的全部可执行用例。${tip}`)) return;
  try {
    const r = await api(`/api/runs/${RUN_ID}/domains/S3/run`, 'POST', {sub});
    const parts = [`▶ S3 ${sub} 组顺序运行已启动:共 ${r.queued.length} 项(${r.queued.join('、') || '无可执行项'})`];
    if (r.skipped && r.skipped.length) {
      parts.push(`跳过 ${r.skipped.length} 项:` + r.skipped.map(s => `${s.id}(${s.reason})`).join(';'));
    }
    alert(parts.join('\n'));
    refresh();
  } catch (e) { alert(e.message); }
}

// ---------- 用例详情弹窗 ----------
function openCase(cid) { OPEN_CASE = cid; renderModal(); document.getElementById('modal-mask').style.display = 'flex'; }
function closeModal() {
  OPEN_CASE = null;
  document.getElementById('modal-mask').style.display = 'none';
  if (soakTimer) { clearInterval(soakTimer); soakTimer = null; }
}

function renderModal() {
  const c = CASE_BY_ID[OPEN_CASE];
  const r = (STATE.results || []).find(x => x.case_id === OPEN_CASE) || {status: 'pending'};
  document.getElementById('m-title').innerHTML =
    `${c.id} ${esc(c.title)} <span class="badge st-${r.status}">${STATUS_TXT[r.status] || r.status}</span>` +
    (c.hazard === 'critical' ? ' <span class="hazard">⚠高危</span>' : '');
  const b = document.getElementById('m-body');
  let html = '';
  if (c.hazard === 'critical') html += '<div class="banner-danger">⚠️ 高危用例,执行前请阅读指导并确认风险(需输入编号)</div>';
  html += `<div class="kv"><b>测试方法</b><div class="small">${esc(c.steps).replace(/\n/g, '<br>')}</div></div>`;
  html += `<div class="kv"><b>记录指标 / 判读</b><div class="small">${esc(c.expected).replace(/\n/g, '<br>')}</div></div>`;
  if (c.guide) html += `<div class="kv guide"><b>📋 人工操作指导</b><div class="small">${esc(c.guide).replace(/\n/g, '<br>')}</div></div>`;
  if (r.actual) html += `<div class="kv"><b>实测/记录</b><div class="small mono">${esc(r.actual).replace(/\n/g, '<br>')}</div></div>`;
  if (r.error) html += `<div class="kv"><b>错误</b><div class="small mono err">${esc(r.error)}</div></div>`;
  if (r.metrics && Object.keys(r.metrics).length) {
    html += '<div class="kv"><b>指标</b><table class="table small">' +
      Object.entries(r.metrics).map(([k, v]) =>
        `<tr><td class="mono">${esc(k)}</td><td class="mono">${esc(typeof v === 'object' ? JSON.stringify(v) : v)}</td></tr>`).join('') +
      '</table></div>';
  }
  // 人工数值录入(S9-01 功耗 / S3-BASE-02 重建衰减)
  if (METRIC_FORMS[c.id]) {
    html += `<div class="kv"><b>🔢 数值录入(保存后可运行校验/参与评级)</b><div class="grid2">` +
      METRIC_FORMS[c.id].map(([k, label]) =>
        `<label>${label}<input type="number" step="any" id="mf-${k}" value="${r.metrics?.[k] ?? ''}"></label>`).join('') +
      `</div><button class="btn btn-sm btn-primary" style="margin-top:6px" onclick="submitMetrics('${c.id}')">保存数值</button>
      <span id="mf-msg" class="small muted"></span></div>`;
  }
  // soak 实时样本
  if (c.id === 'S8-01') {
    html += `<div class="kv"><b>🌡 浸泡实时采样(运行中自动刷新)</b>
      <div id="soak-live" class="small mono muted">加载中…</div></div>`;
  }
  if (r.duration_ms) html += `<div class="small muted">耗时 ${(r.duration_ms / 1000).toFixed(1)}s · 判定方:${r.verdict_by === 'manual' ? '人工' : (r.verdict_by || '—')}</div>`;
  html += `<div class="kv"><b>证据</b><div class="small">
      <a href="/runs/${RUN_ID}/cases/${c.id}/files/stdout.log" target="_blank">stdout.log</a> ·
      <a href="/runs/${RUN_ID}/cases/${c.id}/files/metrics.json" target="_blank">metrics.json</a> ·
      附件: <span id="m-atts">${(r.attachments || []).join(', ') || '无'}</span></div></div>`;
  html += `<div class="kv"><b>上传证据附件(截图等)</b>
      <input type="file" id="m-file"> <button class="btn btn-sm" onclick="uploadAtt('${c.id}')">上传</button></div>`;
  if (c.mode === 'manual') {
    html += `<div class="kv verdict-box"><b>人工判定</b>
      <div><input id="m-note" class="note-input" placeholder="备注(实测数据/现象说明)">
      <button class="btn btn-sm btn-pass" onclick="verdict('${c.id}','pass')">判为通过</button>
      <button class="btn btn-sm btn-fail" onclick="verdict('${c.id}','fail')">判为失败</button>
      <button class="btn btn-sm" onclick="verdict('${c.id}','skipped')">跳过(无法测试)</button></div></div>`;
  }
  b.innerHTML = html;
  if (c.id === 'S8-01') startSoakPolling();
}

async function submitMetrics(cid) {
  const fields = METRIC_FORMS[cid];
  const metrics = {};
  for (const [k] of fields) {
    const v = document.getElementById('mf-' + k).value;
    if (v !== '') metrics[k] = parseFloat(v);
  }
  if (!Object.keys(metrics).length) { document.getElementById('mf-msg').textContent = '请至少填写一项'; return; }
  try {
    await api(`/api/runs/${RUN_ID}/cases/${cid}/metrics`, 'POST', {metrics});
    document.getElementById('mf-msg').textContent = '✅ 已保存';
    refresh();
  } catch (e) { document.getElementById('mf-msg').textContent = '保存失败: ' + e.message; }
}

function startSoakPolling() {
  if (soakTimer) clearInterval(soakTimer);
  const tick = async () => {
    if (OPEN_CASE !== 'S8-01') { clearInterval(soakTimer); soakTimer = null; return; }
    try {
      const d = await api(`/api/runs/${RUN_ID}/cases/S8-01/samples?n=14`, 'GET');
      const box = document.getElementById('soak-live');
      if (!box) { clearInterval(soakTimer); soakTimer = null; return; }
      if (!d.samples.length) { box.innerHTML = '<span class="muted">暂无样本(S8-01 开始后每 ' + (d.interval || '?') + 's 采样一次)</span>'; return; }
      box.innerHTML = `<div class="small muted">${d.live ? '🔴 实时' : '历史(soak.csv)'} · 最近 ${d.samples.length} 条</div>
        <table class="table small"><thead><tr><th>时间</th><th>温度℃</th><th>频率MHz</th><th>CPU%</th>
        <th>磁盘IOPS</th><th>写MB/s</th><th>读MB/s</th><th>网络Mbps</th><th>内存MB</th></tr></thead><tbody>` +
        d.samples.map(s => `<tr><td class="mono">${esc(s.ts || '')}</td><td>${esc(s.temp_c ?? '')}</td>
          <td>${esc(s.freq_mhz ?? '')}</td><td>${esc(s.cpu_pct ?? '')}</td><td>${esc(s.disk_iops ?? '')}</td>
          <td>${esc(s.disk_write_mbps ?? '')}</td><td>${esc(s.disk_read_mbps ?? '')}</td>
          <td>${esc(s.net_mbps ?? '')}</td><td>${esc(s.mem_avail_mb ?? '')}</td></tr>`).join('') +
        '</tbody></table>';
    } catch (e) { /* 忽略瞬时错误 */ }
  };
  tick();
  soakTimer = setInterval(tick, 5000);
}

async function uploadAtt(cid) {
  const f = document.getElementById('m-file').files[0];
  if (!f) return alert('请选择文件');
  const fd = new FormData();
  fd.append('file', f);
  const r = await fetch(`/api/runs/${RUN_ID}/cases/${cid}/attachments`, {method: 'POST', body: fd});
  if (r.ok) { renderModal(); refresh(); } else alert('上传失败');
}

async function verdict(cid, v) {
  const note = document.getElementById('m-note').value;
  await api(`/api/runs/${RUN_ID}/cases/${cid}/verdict`, 'POST', {verdict: v, note});
  renderModal(); refresh();
}

// ---------- 启动 ----------
buildDomains();
refresh();
