'use strict';
const $ = selector => document.querySelector(selector);
const state = {items: [], selected: null, checked: new Set(), queue: null, blind: localStorage.getItem('audio-review-blind') === 'true', loading: false, signature: '', health: null, drafts: new Map(), imports: {entries: [], running: false, nextId: 1}};
const ratingFields = [
  ['human_likeness', '像真人播客吗'], ['naturalness', '语音自然度'], ['clarity', '音质清晰度'],
  ['engagement', '愿继续听'], ['voice_distinction', '声音区分度'], ['accent_emotion', '口音情绪合适'],
];
const statusNames = {uploaded: '等待检测', queued: '已加入队列', processing: '检测中', done: '检测完成', error: '检测失败'};
const storageLocation = () => state.health?.local_only === false ? '服务器' : '本机';
const scopeNames = {fast: '全量快速', sample: '快速抽样', full: '全量精细'};
const resultScope = quality => quality.scope === 'sample' && (quality.target_hop_seconds ?? quality.hop_seconds) === 1 ? '抽样精细' : scopeNames[quality.scope];
const scopeDescriptions = {
  fast: '约 9 秒一个音质窗口，覆盖全段。基础声学检测始终覆盖全量。',
  sample: '评测开头与中段共 2 分钟，约 9 秒一个音质窗口。基础声学检测仍覆盖全量。',
  full: '按 1 秒步长密集评分，耗时较长，适合复核。基础声学检测覆盖全量。',
};
const eligible = item => !['queued','processing'].includes(item.status);
const batchItems = () => {
  const checked = state.items.filter(item => state.checked.has(item.id) && eligible(item));
  return checked.length ? checked : state.items.filter(item => ['uploaded','error'].includes(item.status));
};
let queueSignature = '';
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const formatTime = seconds => {
  if (!Number.isFinite(seconds)) return '--:--';
  const total = Math.floor(seconds), hours = Math.floor(total / 3600), minutes = Math.floor(total / 60) % 60;
  return (hours ? hours + ':' + String(minutes).padStart(2, '0') : String(minutes).padStart(2, '0')) + ':' + String(total % 60).padStart(2, '0');
};
const scoreText = score => score == null ? '—' : Number(score).toFixed(2);
const fileSize = bytes => bytes >= 1024 * 1024 ? (bytes / 1024 / 1024).toFixed(1) + ' MB' : Math.ceil(bytes / 1024) + ' KB';
function notify(message) {
  const toast = $('#toast'); toast.textContent = message; toast.classList.add('visible');
  clearTimeout(notify.timer); notify.timer = setTimeout(() => toast.classList.remove('visible'), 4200);
}
async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = '操作未完成，请稍后再试。';
    try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}
function displayName(item) { return state.blind ? item.sample_id + ' · 盲听样本' : item.filename; }
function renderQueue() {
  const items = state.items;
  $('#total-count').textContent = items.length;
  $('#done-count').textContent = items.filter(item => item.status === 'done').length;
  $('#flagged-count').textContent = items.filter(item => item.status === 'done' && item.result?.metrics.findings.length).length;
  $('#reviewed-count').textContent = items.filter(item => item.review.complete).length;
  const pending = batchItems();
  $('#run-button').disabled = !pending.length || state.loading || !state.health?.ready;
  $('#pending-count').textContent = pending.length ? '（' + pending.length + ' 条）' : '';
  $('#export-button').disabled = !items.length;
  $('#queue-label').textContent = items.length ? '音频列表 · ' + items.length : '音频列表';
  const selectable = items.filter(eligible), checked = selectable.filter(item => state.checked.has(item.id));
  $('#selection-count').textContent = checked.length ? '已选 ' + checked.length + ' 条' : '未选时检测待处理音频';
  $('#select-all').checked = selectable.length > 0 && checked.length === selectable.length;
  $('#select-all').indeterminate = checked.length > 0 && checked.length < selectable.length;
  $('#select-all').disabled = !selectable.length;
  const queue = state.queue, workers = queue?.workers || state.health?.workers || 2;
  $('#queue-status').textContent = queue?.active_jobs || queue?.queued_jobs
    ? `评测任务 ${queue.active_jobs}/${workers} · 排队 ${queue.queued_jobs} 个任务 · 可批量提交多条`
    : `可批量提交多条 · 同时评测最多 ${workers} 条，其他自动排队`;
  const nextQueueSignature = JSON.stringify([items.map(item => [item.id,item.updated_at]),state.selected,state.blind,[...state.checked]]);
  if (nextQueueSignature === queueSignature) return;
  queueSignature = nextQueueSignature;
  $('#audio-list').innerHTML = items.length ? items.map(item => {
    const score = item.status === 'done' ? item.result?.quality.overall : null;
    const busy = ['queued','processing'].includes(item.status);
    return `<div class="audio-row"><label class="batch-check"><input type="checkbox" data-check="${item.id}" aria-label="选择 ${escapeHTML(displayName(item))}" ${state.checked.has(item.id) ? 'checked' : ''} ${busy ? 'disabled' : ''}></label><button class="audio-item ${item.id === state.selected ? 'selected' : ''}" data-select="${item.id}" aria-label="查看 ${escapeHTML(displayName(item))}"><span class="audio-index">${item.sample_id}</span><span class="audio-item-info"><span class="audio-name">${escapeHTML(displayName(item))}</span><span class="audio-meta"><span class="status ${escapeHTML(item.status)}">${busy ? escapeHTML(item.stage) : item.result?.processing?.cache_hit ? '已复用结果' : statusNames[item.status]}</span><span>·</span><span>${item.result ? formatTime(item.result.metrics.duration) : fileSize(item.size)}</span>${item.review.complete ? '<span>· 已评分</span>' : ''}</span>${busy ? `<span class="item-progress"><span style="width:${item.progress}%"></span></span>` : ''}</span>${score != null ? `<span class="audio-score">${scoreText(score)}</span>` : ''}</button></div>`;
  }).join('') : '<div class="empty-queue"><span class="mini-wave">▂ ▆ █ ▄ ▂</span><p>导入音频后，在这里查看进度。</p></div>';
}
function selectedItem() { return state.items.find(item => item.id === state.selected); }
function playerMarkup(item) {
  return `<div class="player-box"><div class="player-tools"><audio id="audio-player" controls preload="metadata" src="/api/audio/${item.id}"></audio><label class="speed-control">倍速<select id="playback-speed" aria-label="播放倍速"><option value="0.75">0.75×</option><option value="1" selected>1×</option><option value="1.25">1.25×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label></div><canvas id="waveform" class="waveform" aria-label="声音波形，点击可跳转播放位置" tabindex="0"></canvas><div class="wave-times"><span id="play-time">00:00</span><span>${formatTime(item.result?.metrics.duration)}</span></div></div>`;
}
function qualityMarkup(result) {
  const quality = result.quality, metrics = result.metrics;
  quality.windows ||= [];
  const chartWindows = quality.windows.length <= 120 ? quality.windows : Array.from({length:120}, (_, index) => {
    const chunk = quality.windows.slice(Math.floor(index / 120 * quality.windows.length), Math.floor((index + 1) / 120 * quality.windows.length));
    return chunk.reduce((lowest, window) => window.overall < lowest.overall ? window : lowest, chunk[0]);
  });
  const scope = resultScope(quality) || quality.scope;
  const noScore = quality.overall == null;
  return `<div class="section-heading"><h3>自动音质评分</h3><span>${scope} ${formatTime(quality.coverage_seconds)} · ${quality.window_count} 个有效窗口</span></div>
    <div class="quality-grid"><div class="quality-card featured"><span>整体音质</span><strong>${scoreText(quality.overall)}<small>/ 5</small></strong><p>${noScore ? '未发现可评分的声音窗口' : 'DNSMOS P.835 预测值'}</p></div><div class="quality-card"><span>人声音质</span><strong>${scoreText(quality.speech)}<small>/ 5</small></strong><p>人声清晰度与失真听感</p></div><div class="quality-card"><span>背景质量</span><strong>${scoreText(quality.background)}<small>/ 5</small></strong><p>背景噪声对听感的影响</p></div></div>
    ${quality.windows.length ? `<div class="window-chart" aria-label="音质片段评分，点击回听">${chartWindows.map(window => `<button class="window-bar ${window.overall < 2.5 ? 'low' : ''}" style="height:${Math.max(8, window.overall / 5 * 42)}px" data-seek="${window.start}" title="${formatTime(window.start)} · ${scoreText(window.overall)}/5" aria-label="回听 ${formatTime(window.start)}，音质 ${scoreText(window.overall)} 分"></button>`).join('')}</div>` : ''}
    <p class="quality-caption">${noScore ? '低能量或静音音频不输出虚构分数。' : `低分段分位值 P10：${scoreText(quality.p10)} · 最低片段：${scoreText(quality.minimum)}。`}${quality.short_audio_padded ? ' 音频不足 9.01 秒，使用重复填充，评分仅供参考。' : ''}${quality.windows.length > 120 ? ' 片段图按时间区间展示较低分。' : ''}<br>${(quality.target_hop_seconds ?? quality.hop_seconds) > 1 ? '约 9 秒一个窗口，短时问题可用全量精细模式复核。' : '1 秒滑窗精细评分。'}${result.processing ? result.processing.cache_hit ? ' 本次复用相同内容、相同模式的自动结果。' : ` 本次计算 ${result.processing.elapsed_seconds.toFixed(1)} 秒。` : ''}<br>模型适用于人声音质；配乐可能影响背景分。尚未按实际中文音频与人工评分校准。</p>
    <div class="technical-grid"><div class="technical-cell"><span>平均声音电平</span><strong>${metrics.rms_dbfs.toFixed(1)} dBFS</strong></div><div class="technical-cell"><span>近满幅样本</span><strong>${metrics.near_full_scale_percent.toFixed(2)}%</strong></div><div class="technical-cell"><span>低能量片段</span><strong>${metrics.low_energy_percent.toFixed(1)}%</strong></div><div class="technical-cell"><span>原始采样 / 声道</span><strong>${(metrics.sample_rate / 1000).toFixed(1)} kHz / ${metrics.channels}</strong></div></div>`;
}
function findingsMarkup(result) {
  const findings = result.metrics.findings;
  return `<div class="section-heading"><h3>问题片段与回听</h3><span>${findings.length ? findings.length + ' 处提示' : '全量声学检查完成'}</span></div><div class="findings">${findings.length ? findings.map(finding => `<div class="finding"><button class="finding-time" data-seek="${finding.start}" aria-label="回听 ${formatTime(finding.start)}">${formatTime(finding.start)}</button><div class="finding-content"><strong>${escapeHTML(finding.title)}</strong><p>${escapeHTML(finding.description)}</p></div><span class="finding-level">${finding.level}</span></div>`).join('') : '<div class="no-findings">未触发当前检测规则。自然度与内容仍需试听判断；短时音质问题可用全量精细模式复核。</div>'}</div>`;
}
function ratingOptions(value) {
  return `<option value="" ${value === undefined ? 'selected' : ''}>未评分</option>` + [1,2,3,4,5].map(score => `<option value="${score}" ${value === score ? 'selected' : ''}>${score} 分${score === 1 ? ' · 很差' : score === 3 ? ' · 一般' : score === 5 ? ' · 很好' : ''}</option>`).join('') + `<option value="na" ${value === null ? 'selected' : ''}>不适用</option>`;
}
function reviewMarkup(item) {
  const review = item.review, ratings = review.ratings || {};
  const difference = item.result?.quality.overall != null && ratings.clarity != null ? Math.abs(ratings.clarity - item.result.quality.overall) : null;
  return `<div class="section-heading"><h3>人工试听评分</h3><span>${review.complete ? '已完成' : '沿用现有 7 个维度'}</span></div><p class="ratings-note">1 分很差，3 分一般，5 分很好。自动音质分与人工分独立保存；自然度、拟真度等主观自动评审尚未接入。</p><form id="review-form"><div class="ratings-grid">${ratingFields.map(([key,label]) => `<div class="rating-field"><label for="rating-${key}">${label}</label><select id="rating-${key}" name="${key}">${ratingOptions(ratings[key])}</select></div>`).join('')}<div class="rating-field"><label for="ai-suspicion">是否怀疑 AI 合成</label><select id="ai-suspicion" name="ai_suspicion"><option value="">未判断</option>${['怀疑','不确定','不怀疑'].map(option => `<option ${review.ai_suspicion === option ? 'selected' : ''}>${option}</option>`).join('')}</select></div><div class="rating-field"><label for="reviewer-name">评分人（选填）</label><input id="reviewer-name" name="reviewer" value="${escapeHTML(review.reviewer || '')}" maxlength="80" placeholder="填写姓名" class="reviewer-input"></div></div><div class="review-notes"><label for="review-notes">判为 AI 的理由 / 试听备注</label><textarea id="review-notes" name="notes" maxlength="5000" placeholder="记录声音、停顿、对话或其他可回听依据">${escapeHTML(review.notes || '')}</textarea></div><div class="review-actions"><span id="save-status">${review.saved_at ? '已保存 · ' + new Date(review.saved_at).toLocaleString('zh-CN') : '评分保存在' + storageLocation() + '，刷新后仍可查看。'}</span><button type="submit" class="button primary" id="save-review">保存人工评分</button></div></form>${difference != null ? `<div class="comparison">人工音质清晰度 ${ratings.clarity} 分 · 自动整体音质 ${scoreText(item.result.quality.overall)} 分 · 相差 ${difference.toFixed(2)} 分。两者衡量范围不同，用于后续校准参考。</div>` : ''}`;
}
function renderDetail(force = false) {
  const item = selectedItem();
  if (!item) return;
  if (item.status === 'done' && item.result && !Array.isArray(item.result.quality.windows)) return;
  const signature = JSON.stringify([item.id, item.updated_at, state.blind]);
  if (!force && signature === state.signature) return;
  const previousId = $('#detail-panel').dataset.audioId;
  const previousForm = $('#review-form'), previousPlayer = $('#audio-player');
  if (previousId && previousForm) {
    if (previousForm.dataset.dirty === 'true') state.drafts.set(previousId, Object.fromEntries(new FormData(previousForm)));
    else state.drafts.delete(previousId);
  }
  const playback = previousId === item.id && previousPlayer ? {time:previousPlayer.currentTime, rate:previousPlayer.playbackRate, paused:previousPlayer.paused} : null;
  state.signature = signature;
  const busy = ['queued','processing'].includes(item.status);
  const meta = busy ? `${fileSize(item.size)} · ${scopeNames[item.scope]}` : item.result ? `${formatTime(item.result.metrics.duration)} · ${fileSize(item.size)} · ${resultScope(item.result.quality)}` : `${fileSize(item.size)} · ${statusNames[item.status]}`;
  const badgeClass = item.status === 'done' ? '' : item.status === 'error' ? 'amber' : 'neutral';
  let content;
  if (item.status === 'done' && item.result) {
    content = playerMarkup(item) + qualityMarkup(item.result) + findingsMarkup(item.result) + reviewMarkup(item);
  } else if (busy) {
    content = `<div class="notice">${escapeHTML(item.stage)}。批量任务会自动排队，在${storageLocation()}最多同时评测 ${state.health?.workers || 2} 条，可继续导入其他音频。</div><div class="job-progress"><span style="width:${item.progress}%"></span></div><p class="scope-pill">${item.progress}% · ${scopeNames[item.scope]} · 基础检测覆盖全量</p><div class="detail-placeholder"><div><span class="mini-wave">▂ ▆ █ ▄ ▂</span><h3>正在分析声音</h3><p>完成后会显示真实音质评分和问题片段。</p></div></div>`;
  } else {
    content = playerMarkup(item) + (item.status === 'error' ? `<div class="notice error">${escapeHTML(item.error || '检测失败，请重试。')}</div>` : '<div class="notice">音频已导入。勾选多条后点击“批量检测”，系统会自动处理全部所选音频。</div>') + `<div class="detail-placeholder"><div><span class="mini-wave">▂ ▆ █ ▄ ▂</span><h3>${item.status === 'error' ? '可以重新检测这条音频' : '准备开始检测'}</h3><button class="button secondary" id="run-selected">${item.status === 'error' ? '重试检测' : '检测这条音频'}</button></div></div>`;
  }
  $('#detail-panel').innerHTML = `<div class="detail-header"><div class="detail-title-row"><div><div class="eyebrow">02 / ${item.sample_id} · 结果与回听</div><h2>${escapeHTML(displayName(item))}</h2><div class="detail-sub">${escapeHTML(meta)}</div></div><span class="badge ${badgeClass}">${statusNames[item.status]}</span></div>${item.status === 'done' ? '<button class="text-button" id="run-selected">重新检测</button>' : ''}</div><div class="detail-body">${content}</div>`;
  $('#run-selected')?.addEventListener('click', () => runItems([item.id], item.status === 'done'));
  $('#review-form')?.addEventListener('submit', saveHumanReview);
  $('#detail-panel').dataset.audioId = item.id;
  const draft = state.drafts.get(item.id), form = $('#review-form');
  if (form) {
    for (const event of ['input','change']) form.addEventListener(event, () => { form.dataset.dirty = 'true'; });
    if (draft) {
      for (const [name,value] of Object.entries(draft)) { const field = form.elements.namedItem(name); if (field) field.value = value; }
      form.dataset.dirty = 'true';
    }
  }
  setupPlayer(item);
  const player = $('#audio-player');
  if (playback && player) {
    const restore = () => {
      player.currentTime = Math.min(playback.time, Number.isFinite(player.duration) ? player.duration : playback.time);
      player.playbackRate = playback.rate; $('#playback-speed').value = String(playback.rate);
      if (!playback.paused) player.play().catch(() => {});
    };
    if (player.readyState >= 1) restore(); else player.addEventListener('loadedmetadata', restore, {once:true});
  }
}
let resizeObserver;
function setupPlayer(item) {
  resizeObserver?.disconnect(); resizeObserver = null;
  const player = $('#audio-player'), canvas = $('#waveform');
  if (!player || !canvas) return;
  const waveform = item.result?.metrics.waveform || [], knownDuration = item.result?.metrics.duration;
  const draw = () => {
    if (!canvas.isConnected) return;
    const rect = canvas.getBoundingClientRect(), ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, rect.width * ratio); canvas.height = rect.height * ratio;
    const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
    const width = rect.width, height = rect.height, duration = knownDuration || player.duration || 1;
    ctx.fillStyle = '#f3f7fa'; ctx.fillRect(0, 0, width, height);
    for (const finding of item.result?.metrics.findings || []) {
      ctx.fillStyle = '#f1dfbb70';
      ctx.fillRect(finding.start / duration * width, 4, Math.max(3, (finding.end - finding.start) / duration * width), height - 8);
    }
    const count = Math.max(1, Math.floor(width / 4)), max = Math.max(...waveform, 0.02);
    for (let i = 0; i < count; i++) {
      const start = Math.floor(i / count * waveform.length), end = Math.max(start + 1, Math.floor((i + 1) / count * waveform.length));
      const value = waveform.length ? Math.max(...waveform.slice(start, end), 0) : .02;
      const barHeight = Math.max(2, Math.sqrt(value / max) * (height - 16));
      ctx.fillStyle = i / count < player.currentTime / duration ? '#156852' : '#a7bdc9';
      ctx.fillRect(i * width / count, (height - barHeight) / 2, 2, barHeight);
    }
    const position = player.currentTime / duration * width;
    ctx.fillStyle = '#183044'; ctx.fillRect(position, 3, 1.5, height - 6);
    $('#play-time').textContent = formatTime(player.currentTime);
  };
  player.addEventListener('timeupdate', draw);
  player.addEventListener('loadedmetadata', draw);
  player.addEventListener('error', () => notify('浏览器暂时无法播放这个格式，可换用 MP3 或 WAV 进行回听。'));
  $('#playback-speed').addEventListener('change', event => { player.playbackRate = Number(event.target.value); });
  canvas.addEventListener('click', event => {
    const duration = knownDuration || player.duration;
    if (!Number.isFinite(duration)) return;
    const rect = canvas.getBoundingClientRect(); player.currentTime = Math.max(0, Math.min(duration, (event.clientX - rect.left) / rect.width * duration));
    draw();
  });
  canvas.addEventListener('keydown', event => {
    if (['ArrowLeft','ArrowRight'].includes(event.key)) { event.preventDefault(); player.currentTime = Math.max(0, Math.min(player.duration || 0, player.currentTime + (event.key === 'ArrowRight' ? 5 : -5))); draw(); }
    if (event.key === ' ') { event.preventDefault(); player.paused ? player.play().catch(() => {}) : player.pause(); }
  });
  resizeObserver = new ResizeObserver(draw); resizeObserver.observe(canvas); draw();
}
let refreshVersion = 0;
async function refresh(force = false) {
  const version = ++refreshVersion, selection = state.selected;
  try {
    const response = await api('/api/reviews?compact=true');
    if (version !== refreshVersion) return;
    if (selection !== state.selected) return refresh(force);
    const previous = new Map(state.items.map(item => [item.id,item]));
    state.items = response.items.map(item => {
      const old = previous.get(item.id);
      if (old?.updated_at === item.updated_at && Array.isArray(old.result?.quality.windows)) item.result = old.result;
      return item;
    });
    state.queue = response.queue;
    if (!state.selected && state.items.length) state.selected = state.items[0].id;
    const selected = selectedItem();
    if (selected?.result && eligible(selected) && !Array.isArray(selected.result.quality.windows)) {
      const item = await api('/api/reviews/' + selected.id);
      if (version !== refreshVersion) return;
      state.items = state.items.map(row => row.id === item.id ? item : row);
    }
    renderQueue(); renderDetail(force);
  } catch (error) { if (force) notify('检测服务暂时不可用，请确认它正在运行。'); }
}
const importName = entry => state.blind ? '导入音频 #' + entry.number : entry.name;
let importRenderFrame;
function scheduleImportRender() {
  if (importRenderFrame) return;
  importRenderFrame = requestAnimationFrame(() => { importRenderFrame = null; renderImports(); });
}
function renderImports() {
  const imports = state.imports, entries = imports.entries;
  const panel = $('#upload-panel'); panel.hidden = !entries.length;
  $('#dropzone').classList.toggle('importing', imports.running);
  $('#dropzone strong').textContent = imports.running ? '继续添加音频，或拖入多条' : '拖入音频，或点击多选';
  if (!entries.length) return;
  const done = entries.filter(entry => entry.status === 'done').length;
  const failed = entries.filter(entry => entry.status === 'error').length;
  const waiting = entries.filter(entry => entry.status === 'queued').length;
  const active = entries.find(entry => ['uploading','confirming'].includes(entry.status));
  const transferable = entries.filter(entry => !entry.invalid);
  const totalBytes = transferable.reduce((sum, entry) => sum + entry.size, 0);
  const loadedBytes = transferable.reduce((sum, entry) => sum + entry.loaded, 0);
  const percentage = totalBytes ? Math.min(100, Math.floor(loadedBytes / totalBytes * 100)) : 0;
  $('#upload-title').textContent = imports.running ? '正在导入音频' : failed ? '导入完成，有未完成音频' : '导入完成';
  $('#upload-summary').textContent = `已导入 ${done}/${entries.length} 条${waiting ? ' · 待上传 ' + waiting + ' 条' : ''}${failed ? ' · 未完成 ' + failed + ' 条' : ''}`;
  $('#upload-percent').textContent = percentage + '%';
  const progress = $('#upload-progress');
  progress.firstElementChild.style.width = percentage + '%';
  progress.classList.toggle('indeterminate', active?.status === 'uploading' && active.progress === null);
  progress.setAttribute('aria-valuenow', String(percentage));
  progress.setAttribute('aria-valuetext', `已传输 ${percentage}%，已导入 ${done}/${entries.length} 条${active?.status === 'confirming' ? '，等待服务器确认' : ''}`);
  $('#upload-bytes').textContent = `已传输 ${fileSize(loadedBytes)} / ${fileSize(totalBytes)}`;
  $('#upload-current').textContent = active
    ? active.status === 'confirming'
      ? `已传输 100%，等待服务器保存并确认：${importName(active)}`
      : `正在上传第 ${entries.indexOf(active) + 1}/${entries.length} 条：${importName(active)}${active.progress === null ? '' : ' · ' + Math.floor(active.progress * 100) + '%'}`
    : failed ? '展开导入明细查看原因；其他音频已继续导入。' : '服务器已确认导入，可以勾选多条开始批量检测。';
  $('#upload-help').textContent = imports.running
    ? '可继续添加多条音频，已导入的音频可以先开始检测。'
    : '每条音频单独上传并确认，单条失败不会中断后续导入。';
  $('#clear-upload-history').disabled = imports.running;
  const retryable = entries.filter(entry => entry.status === 'error' && !entry.invalid);
  $('#retry-uploads').hidden = !retryable.length;
  $('#retry-uploads').textContent = `重试未完成的音频（${retryable.length} 条）`;
  $('#upload-record-list').innerHTML = entries.map(entry => {
    const status = entry.status === 'done' ? '已导入'
      : entry.status === 'confirming' ? '已传输 100% · 等待确认'
      : entry.status === 'uploading' ? entry.progress === null ? '正在上传' : '上传 ' + Math.floor(entry.progress * 100) + '%'
      : entry.status === 'error' ? '未完成' : '等待上传';
    const error = state.blind && entry.error ? entry.error.split(entry.name).join('该音频') : entry.error;
    return `<li class="upload-record ${entry.status === 'error' ? 'error' : ''}"><div><span class="upload-record-name" title="${escapeHTML(importName(entry))}">${escapeHTML(importName(entry))}</span><span class="upload-record-status">${status}</span></div>${error ? `<p>${escapeHTML(error)}</p>` : ''}</li>`;
  }).join('');
}
function sendUpload(entry) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest(), body = new FormData();
    body.append('files', entry.file);
    request.open('POST', '/api/upload');
    request.responseType = 'json';
    request.timeout = 30 * 60 * 1000;
    request.upload.addEventListener('progress', event => {
      if (event.lengthComputable && event.total > 0) {
        entry.progress = Math.min(1, event.loaded / event.total);
        entry.loaded = entry.size * entry.progress;
        if (entry.progress === 1) entry.status = 'confirming';
      } else entry.progress = null;
      scheduleImportRender();
    });
    request.upload.addEventListener('load', () => {
      entry.progress = 1; entry.loaded = entry.size; entry.status = 'confirming'; renderImports();
    });
    request.addEventListener('load', () => {
      const response = request.response;
      if (request.status < 200 || request.status >= 300) {
        reject(new Error(typeof response?.detail === 'string' ? response.detail : `导入请求未完成（HTTP ${request.status}），可稍后重试。`)); return;
      }
      if (!Array.isArray(response?.items) || !response.items.length) {
        reject(new Error('服务器未返回导入记录，请先刷新音频列表确认，再决定是否重试。')); return;
      }
      resolve(response);
    });
    request.addEventListener('error', () => reject(new Error('连接中断，未收到服务器确认。请先检查音频列表，再重试。')));
    request.addEventListener('timeout', () => reject(new Error('上传请求超时，未收到服务器确认。请先检查音频列表，再重试。')));
    request.addEventListener('abort', () => reject(new Error('上传已中断，请先检查音频列表，再重试。')));
    request.send(body);
  });
}
async function processUploadQueue() {
  const imports = state.imports;
  if (imports.running) return;
  imports.running = true; renderImports();
  try {
    let entry;
    // One request at a time bounds server upload pressure and isolates each file's failure.
    while ((entry = imports.entries.find(item => item.status === 'queued'))) {
      entry.status = 'uploading'; entry.progress = 0; entry.loaded = 0; entry.error = ''; renderImports();
      try {
        const response = await sendUpload(entry);
        entry.status = 'done'; entry.progress = 1; entry.loaded = entry.size; entry.file = null;
        // Discard a list request started before this upload was committed.
        refreshVersion += 1;
        for (const item of response.items) {
          const index = state.items.findIndex(existing => existing.id === item.id);
          if (index >= 0) state.items[index] = item; else state.items.push(item);
          state.checked.add(item.id);
        }
        if (!state.selected) state.selected = response.items[0].id;
        renderQueue(); renderDetail();
      } catch (error) {
        entry.status = 'error'; entry.error = error.message || '导入未完成，请稍后重试。';
      }
      renderImports();
    }
  } finally {
    imports.running = false; renderImports(); renderQueue();
    const done = imports.entries.filter(entry => entry.status === 'done').length;
    const failed = imports.entries.filter(entry => entry.status === 'error').length;
    notify(`已导入 ${done} 条音频${failed ? '，' + failed + ' 条未完成，请查看导入明细' : '，可以批量检测'}`);
  }
}
function uploadFiles(files) {
  const selected = Array.from(files); if (!selected.length) return;
  const imports = state.imports;
  if (!imports.running && imports.entries.every(entry => entry.status === 'done')) imports.entries = [];
  const allowed = $('#file-input').accept.toLowerCase().split(',');
  for (const file of selected) {
    const suffix = file.name.includes('.') ? '.' + file.name.split('.').pop().toLowerCase() : '';
    const error = !allowed.includes(suffix) ? '不支持此格式，请选择 MP3、WAV、M4A、FLAC 等音频文件。'
      : file.size > 200 * 1024 * 1024 ? '超过单条 200 MB 限制，请先拆分音频。'
      : file.size === 0 ? '文件为空，请重新选择有效音频。' : '';
    imports.entries.push({number:imports.nextId++, file, name:file.name, size:file.size, loaded:0, progress:0, status:error ? 'error' : 'queued', invalid:!!error, error});
  }
  $('#dropzone').classList.remove('dragging');
  renderImports(); processUploadQueue();
}
function retryUploads() {
  const imports = state.imports;
  const retries = imports.entries.filter(entry => entry.status === 'error' && !entry.invalid);
  if (!retries.length) return;
  for (const entry of retries) { entry.status = 'queued'; entry.error = ''; entry.loaded = 0; entry.progress = 0; }
  const retried = new Set(retries);
  imports.entries = imports.entries.filter(entry => !retried.has(entry)).concat(retries);
  renderImports(); processUploadQueue();
}
async function runItems(ids, force = false) {
  if (state.loading || !ids.length) return;
  state.loading = true; renderQueue();
  try {
    const submitted = [], reused = [];
    for (let index = 0; index < ids.length; index += 200) {
      const response = await api('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:ids.slice(index,index + 200), scope:$('#scope-select').value, force})});
      submitted.push(...response.submitted); reused.push(...response.reused);
      response.submitted.forEach(id => state.checked.delete(id));
    }
    notify(submitted.length ? `已提交 ${submitted.length} 条${reused.length ? '，其中 ' + reused.length + ' 条复用结果' : ''}` : '这些音频已经在检测队列中'); await refresh(true);
  } catch (error) { notify(error.message); }
  finally { state.loading = false; renderQueue(); }
}
async function saveHumanReview(event) {
  event.preventDefault(); const item = selectedItem(); if (!item) return;
  const form = new FormData(event.target), ratings = {};
  for (const [key] of ratingFields) { const value = form.get(key); if (value !== '') ratings[key] = value === 'na' ? null : Number(value); }
  const document = {ratings, ai_suspicion:form.get('ai_suspicion'), reviewer:form.get('reviewer'), notes:form.get('notes')};
  $('#save-review').disabled = true;
  try {
    const updated = await api('/api/reviews/' + item.id + '/human', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(document)});
    state.drafts.delete(item.id); event.target.dataset.dirty = 'false';
    state.items = state.items.map(row => row.id === updated.id ? updated : row); renderQueue(); renderDetail(true);
    notify(updated.review.complete ? '7 个维度的人工评分已保存' : '已保存当前评分，可以稍后补充');
  } catch (error) { notify(error.message); $('#save-review').disabled = false; }
}
$('#blind-toggle').checked = state.blind;
$('#blind-toggle').addEventListener('change', event => { state.blind = event.target.checked; localStorage.setItem('audio-review-blind', String(state.blind)); renderQueue(); renderDetail(true); renderImports(); });
$('#audio-list').addEventListener('click', event => { const button = event.target.closest('[data-select]'); if (button) { state.selected = button.dataset.select; renderQueue(); refresh(true); } });
$('#audio-list').addEventListener('change', event => { const input = event.target.closest('[data-check]'); if (input) { input.checked ? state.checked.add(input.dataset.check) : state.checked.delete(input.dataset.check); renderQueue(); } });
$('#select-all').addEventListener('change', event => { state.items.filter(eligible).forEach(item => event.target.checked ? state.checked.add(item.id) : state.checked.delete(item.id)); renderQueue(); });
$('#select-pending').addEventListener('click', () => { state.checked = new Set(state.items.filter(item => ['uploaded','error'].includes(item.status)).map(item => item.id)); renderQueue(); });
$('#clear-selection').addEventListener('click', () => { state.checked.clear(); renderQueue(); });
$('#scope-select').addEventListener('change', () => { $('#scope-description').textContent = scopeDescriptions[$('#scope-select').value]; });
$('#file-input').addEventListener('change', event => { const files = Array.from(event.target.files); event.target.value = ''; uploadFiles(files); });
$('#dropzone').addEventListener('keydown', event => { if (['Enter',' '].includes(event.key)) { event.preventDefault(); $('#file-input').click(); } });
$('#dropzone').addEventListener('dragover', event => { event.preventDefault(); $('#dropzone').classList.add('dragging'); });
$('#dropzone').addEventListener('dragleave', () => $('#dropzone').classList.remove('dragging'));
$('#dropzone').addEventListener('drop', event => { event.preventDefault(); uploadFiles(event.dataTransfer.files); });
$('#retry-uploads').addEventListener('click', retryUploads);
$('#clear-upload-history').addEventListener('click', () => { if (!state.imports.running) { state.imports.entries = []; renderImports(); } });
$('#run-button').addEventListener('click', () => runItems(batchItems().map(item => item.id)));
$('#refresh-button').addEventListener('click', () => refresh(true));
$('#export-button').addEventListener('click', () => { const link = document.createElement('a'); link.href = '/api/export.csv?blind=' + state.blind; link.download = '音频评测评分表.csv'; document.body.appendChild(link); link.click(); link.remove(); });
$('#detail-panel').addEventListener('click', event => {
  const button = event.target.closest('[data-seek]'), player = $('#audio-player');
  if (button && player) { player.currentTime = Number(button.dataset.seek); player.play().catch(() => notify('点击播放器播放，即可回听所选片段。')); }
});
async function initialize() {
  try {
    state.health = await api('/api/health');
    if (!state.health.local_only) $('#runtime-label').innerHTML = '<span class="local-icon">◉</span> 服务器运行 <span class="separator">/</span> 文件保存在服务器';
    $('#engine-dot').classList.toggle('ready', state.health.ready);
    $('#engine-title').textContent = state.health.ready ? '本地音质模型已就绪' : '本地音质模型尚未安装';
    $('#engine-description').textContent = state.health.ready ? `DNSMOS P.835 · 在${storageLocation()}运行，无付费接口。主观评分待人工校准。` : '重新运行启动程序会安装模型。不调用外部评测服务。';
  } catch (error) {
    $('#engine-title').textContent = '检测服务暂时未连接'; $('#engine-description').textContent = '请运行本地启动程序后刷新页面。';
  }
  await refresh();
}
initialize();
setInterval(() => { if (!document.hidden) refresh(); }, 1500);

const modelContext = document.modelContext;
if (modelContext?.registerTool) {
  const lifecycle = new AbortController();
  const register = tool => {
    try { Promise.resolve(modelContext.registerTool(tool, {signal:lifecycle.signal})).catch(() => {}); } catch (_) {}
  };
  register({name:'list_audio_reviews', title:'查看音频评测结果',
    description:'Read the audio records already imported into this workbench. Does not upload files or call any external model provider.',
    inputSchema:{type:'object',properties:{},additionalProperties:false},
    annotations:{readOnlyHint:true,untrustedContentHint:true},
    async execute(input) {
      if (!input || typeof input !== 'object' || Object.keys(input).length) throw new Error('Expected an empty object.');
      await refresh();
      return {items:state.items.map(item => ({id:item.id,sample:item.sample_id,name:displayName(item),status:item.status,overall_quality:item.result?.quality.overall ?? null,human_review_complete:!!item.review.complete}))};
    }});
  register({name:'start_audio_quality_assessment', title:'开始音质检测',
    description:'Start DNSMOS and acoustic inspection for specified imported audio records on the runtime host. This queues processing and does not finish the assessment immediately. No external model provider is used.',
    inputSchema:{type:'object',properties:{ids:{type:'array',items:{type:'string'},minItems:1,maxItems:200},scope:{type:'string',enum:['fast','sample','full']}},required:['ids'],additionalProperties:false},
    annotations:{readOnlyHint:false,untrustedContentHint:false},
    async execute(input) {
      if (!input || !Array.isArray(input.ids) || !input.ids.length || input.ids.length > 200 || input.ids.some(id => typeof id !== 'string') || !['fast','sample','full'].includes(input.scope || 'fast') || Object.keys(input).some(key => !['ids','scope'].includes(key))) throw new Error('Invalid assessment request.');
      const result = await api('/api/run', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids:input.ids,scope:input.scope || 'fast'})});
      await refresh(true); return {queued_ids:result.submitted,status:'queued'};
    }});
  window.addEventListener('pagehide', () => lifecycle.abort(), {once:true});
}
