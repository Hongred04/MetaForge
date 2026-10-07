/* MetaForge 前端逻辑 */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const S = {
  files: [],          // 扫描结果
  sel: new Set(),     // 已选文件路径
  cur: null,          // 当前文件路径
  meta: null,         // 当前文件元数据
  draft: {},          // 待写入改动 {changes, gps, xmp}
  fields: null,       // 可编辑字段清单
  preset: 'standard',
  rnPlan: [],
  coverBlob: null,
};

/* ─────────────── 工具 ─────────────── */
async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
  });
  const ct = r.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r;
  }
  const j = await r.json();
  if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body) });

function toast(msg, kind = '') {
  const el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transform = 'translateX(14px)';
    el.style.transition = 'all .2s';
    setTimeout(() => el.remove(), 220);
  }, kind === 'err' ? 6000 : 3200);
}

const ext = p => (p.split('.').pop() || '').toLowerCase();
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ─────────────── 侧栏 ─────────────── */
async function scan() {
  const folder = $('#folderInput').value.trim();
  if (!folder) return toast('请填写文件夹路径', 'warn');
  $('#btnScan').disabled = true;
  try {
    const q = new URLSearchParams({
      folder, recursive: $('#chkRecursive').checked ? '1' : '0',
    });
    const r = await api('/api/scan?' + q);
    S.files = r.files;
    S.sel.clear();
    renderFiles();
    toast(r.truncated
      ? `共 ${r.count} 个文件，已载入前 2000 个`
      : `找到 ${r.count} 个文件`, 'ok');
  } catch (e) {
    toast('扫描失败：' + e.message, 'err');
  } finally {
    $('#btnScan').disabled = false;
  }
}

function renderFiles() {
  const kw = $('#filterInput').value.trim().toLowerCase();
  const list = S.files.filter(f => !kw || f.name.toLowerCase().includes(kw));
  const box = $('#fileList');

  if (!list.length) {
    box.innerHTML = `<div class="empty">${S.files.length ? '没有匹配的文件' : '输入文件夹路径后点击「扫描」开始'}</div>`;
  } else {
    box.innerHTML = list.map(f => {
      const isVid = f.kind === 'video';
      return `<div class="fitem ${S.sel.has(f.path) ? 'sel' : ''} ${f.path === S.cur ? 'cur' : ''}"
                   data-path="${esc(f.path)}" title="${esc(f.path)}">
        <input type="checkbox" ${S.sel.has(f.path) ? 'checked' : ''}>
        <div class="fi ${isVid ? 'vid' : 'img'}">${isVid ? 'VID' : 'IMG'}</div>
        <div class="meta">
          <div class="fn">${esc(f.name)}</div>
          <div class="fs">${esc(f.size)}</div>
        </div>
      </div>`;
    }).join('');
  }
  $('#statCount').textContent = `${S.files.length} 个文件`;
  $('#statSel').textContent = `已选 ${S.sel.size}`;
}

$('#fileList').addEventListener('click', e => {
  const item = e.target.closest('.fitem');
  if (!item) return;
  const p = item.dataset.path;
  if (e.target.matches('input[type=checkbox]')) {
    e.stopPropagation();
    e.target.checked ? S.sel.add(p) : S.sel.delete(p);
    item.classList.toggle('sel', e.target.checked);
    $('#statSel').textContent = `已选 ${S.sel.size}`;
    return;
  }
  openFile(p);
});

$('#filterInput').addEventListener('input', renderFiles);
$('#btnScan').addEventListener('click', scan);
$('#folderInput').addEventListener('keydown', e => e.key === 'Enter' && scan());
$('#btnSelAll').addEventListener('click', () => {
  S.files.forEach(f => S.sel.add(f.path));
  renderFiles();
});
$('#btnSelNone').addEventListener('click', () => {
  S.sel.clear();
  renderFiles();
});

/* ─────────────── 打开文件 ─────────────── */
async function openFile(path) {
  S.cur = path;
  S.draft = { changes: {}, gps: null, xmp: null };
  renderFiles();
  $('#welcome').hidden = true;
  $('#editor').hidden = false;
  $('#edName').textContent = path.split(/[\\/]/).pop();

  try {
    S.meta = await api('/api/meta?path=' + encodeURIComponent(path));
  } catch (e) {
    return toast('读取失败：' + e.message, 'err');
  }
  renderAll();
  $('#edFoot').scrollIntoView?.({ block: 'nearest' });
}

async function openByOffset(d) {
  const idx = S.files.findIndex(f => f.path === S.cur);
  const next = S.files[idx + d];
  if (next) openFile(next.path);
}

/* ─────────────── 渲染 ─────────────── */
function renderAll() {
  renderHeader();
  renderBasic();
  renderGps();
  renderRaw();
  // DOM 已重建，之前的脏标记需清除（回填的值不算改动）
  clearDirtyMarks();
}

function renderHeader() {
  const m = S.meta, f = S.files.find(x => x.path === S.cur);
  const chips = [];
  chips.push(`<span class="chip">${m.kind === 'video' ? '视频' : '图片'}</span>`);
  if (f) chips.push(`<span class="chip">${esc(f.size)}</span>`);
  const hasGps = !!m.gps_decimal || Object.keys(m.gps || {}).length > 0 || m.ilst?.['©xyz'];
  chips.push(hasGps
    ? '<span class="chip gps">含 GPS 定位</span>'
    : '<span class="chip clean">无 GPS</span>');
  if (m.preview) chips.push('<span class="chip cover">含封面</span>');
  if (!m.has_exif && m.kind === 'image' && !Object.keys(m.exif || {}).length)
    chips.push('<span class="chip clean">无 EXIF</span>');
  $('#edChips').innerHTML = chips.join('');

  $('#edPath').textContent = S.cur;
  const i = S.files.findIndex(x => x.path === S.cur);
  $('#edPos').textContent = i >= 0 ? `${i + 1} / ${S.files.length}` : '';

  const img = $('#preview'), ph = $('#prevPh');
  if (m.preview) {
    img.onload = () => { img.hidden = false; ph.hidden = true; };
    img.onerror = () => {
      img.hidden = true; img.removeAttribute('src');
      ph.hidden = false; ph.textContent = '封面无法解码';
      toast('封面数据已损坏或格式不受支持，建议重新设置', 'warn');
    };
    img.src = m.preview;
    // data URL 可能同步命中缓存，此时 onload 不会再触发
    if (img.complete && img.naturalWidth > 0) { img.hidden = false; ph.hidden = true; }
  } else {
    img.hidden = true; img.removeAttribute('src');
    img.onload = img.onerror = null;
    ph.hidden = false; ph.textContent = '无封面';
  }
  $('#btnClearCover').style.display = m.preview ? '' : 'none';
}

function renderBasic() {
  const m = S.meta;
  const grid = $('#basicGrid');
  grid.innerHTML = Object.entries(m.basic || {}).map(([k, v]) => `
    <label class="fld"><span>${esc(k)}</span>
      <input type="text" value="${esc(v)}" readonly></label>`).join('');

  // EXIF 可编辑字段（仅图片）
  const exifBlock = $('#exifEditBlock');
  if (m.kind === 'image' && S.fields?.exif_editable?.length) {
    exifBlock.hidden = false;
    const cur = {};
    for (const [k, v] of Object.entries(m.exif || {})) {
      const name = k.split(' [')[0];
      if (S.fields.exif_editable.includes(name)) cur[name] = v;
    }
    $('#exifEditGrid').innerHTML = S.fields.exif_editable.map(k =>
      `<label class="fld"><span>${esc(k)}</span>
        <input type="text" data-exif="${esc(k)}" value="${esc(cur[k] || '')}"
               placeholder="留空则不修改"></label>`).join('');
  } else {
    exifBlock.hidden = true;
  }

  // 视频 ilst 可编辑字段
  const block = $('#videoTagBlock');
  if (m.kind !== 'video') { block.hidden = true; }
  else {
    block.hidden = false;
    const editable = new Set(S.fields?.video_editable || []);
    $('#videoTags').innerHTML = Object.entries(m.ilst || {}).map(([tag, info]) => {
      const isBin = info.type === 'binary';
      return `<label class="fld">
        <span>${esc(info.label)} <em>${esc(tag)}</em></span>
        <input type="text" data-vtag="${esc(tag)}" value="${esc(info.value)}"
               ${isBin || !editable.has(tag) ? 'readonly' : ''}
               placeholder="${isBin ? '二进制内容，请用封面按钮修改' : '留空则不修改'}">
      </label>`;
    }).join('');
  }

  $('#xmpInput').value = m.xmp || '';
}

function renderGps() {
  const m = S.meta;
  const g = m.gps_decimal;
  const st = $('#gpsStatus'), link = $('#gpsMapLink');

  if (g && g.lat != null) {
    st.textContent = `${g.lat.toFixed(7)}, ${g.lon.toFixed(7)}` +
      (g.alt != null ? `  海拔 ${g.alt} m` : '');
    $('#gpsInput').value = `${g.lat}, ${g.lon}`;
    $('#gpsLat').value = g.lat;
    $('#gpsLon').value = g.lon;
    $('#gpsAlt').value = g.alt ?? '';
    link.hidden = false;
    link.href = `https://www.openstreetmap.org/?mlat=${g.lat}&mlon=${g.lon}#map=16/${g.lat}/${g.lon}`;
  } else {
    st.textContent = '无定位信息';
    st.style.color = 'var(--text-3)';
    link.hidden = true;
  }
  st.style.color = g && g.lat != null ? 'var(--text)' : '';
}

function renderRaw() {
  const m = S.meta;

  $('#exifView').innerHTML = kvHtml(m.exif || {});
  $('#exifView').style.display = Object.keys(m.exif || {}).length ? '' : 'none';

  const gpsRows = { ...(m.gps || {}) };
  if (m.gps_decimal) {
    gpsRows['— 十进制坐标 —'] =
      `${m.gps_decimal.lat}, ${m.gps_decimal.lon}` +
      (m.gps_decimal.alt != null ? `  (海拔 ${m.gps_decimal.alt}m)` : '');
  }
  const gw = $('#gpsRawView');
  gw.innerHTML = Object.keys(gpsRows).length
    ? `<h4 style="margin:16px 0 7px;font-size:13px">GPS 原始字段</h4>` + kvHtml(gpsRows) : '';
  gw.style.display = Object.keys(gpsRows).length ? '' : 'none';

  const keys = m.keys || {};
  const kw = $('#keysView');
  kw.innerHTML = Object.keys(keys).length
    ? `<h4 style="margin:16px 0 7px;font-size:13px">厂商私有标签（Keys）</h4>` +
      kvHtml(keys) +
      '<p class="hint" style="margin-top:7px">这些是手机/相机厂商写入的私有字段，编辑时会原样保留。</p>'
    : '';
  kw.style.display = Object.keys(keys).length ? '' : 'none';

  const all = { ...(m.basic || {}) };
  if (m.kind === 'video') {
    for (const [k, v] of Object.entries(m.ilst || {})) all[`ilst:${k}`] = v.value;
    for (const [k, v] of Object.entries(keys)) all[`keys:${k}`] = v;
    for (const t of m.tracks || []) all[`轨道:${t.类型}`] = t.分辨率;
  } else {
    for (const [k, v] of Object.entries(m.exif || {})) all[k] = v;
  }
  if (m.gps_decimal) all['GPS(十进制)'] = `${m.gps_decimal.lat}, ${m.gps_decimal.lon}`;
  $('#rawView').innerHTML = kvHtml(all);
  $('#xmpRaw').value = m.xmp || '';
}

function kvHtml(obj) {
  const rows = Object.entries(obj).filter(([, v]) => v !== '' && v != null);
  if (!rows.length) return '<div class="row-kv"><div class="k">（空）</div></div>';
  return rows.map(([k, v]) => `
    <div class="row-kv">
      <div class="k">${esc(k)}</div>
      <div class="v">${esc(v)}</div>
    </div>`).join('');
}

/* ─────────────── 改动收集 ─────────────── */
// 委托监听：renderBasic 会重建 DOM，直接绑定会丢失
document.addEventListener('input', e => {
  if (e.target.matches('input[data-exif], input[data-vtag], #xmpInput, #gpsInput, #gpsLat, #gpsLon, #gpsAlt')) {
    e.target.classList.add('dirty');
    markDirty();
  }
});
document.addEventListener('change', e => {
  if (e.target.matches('input[data-exif], input[data-vtag], #xmpInput')) {
    e.target.classList.add('dirty');
    markDirty();
  }
});
// 渲染后清掉上一次的脏标记
function clearDirtyMarks() {
  $$('.dirty').forEach(el => el.classList.remove('dirty'));
  markDirty();
}

function collectChanges() {
  const changes = {};
  // 只读字段（封面占位、Keys 设备信息等）绝不参与写入，
  // 否则 "<1234 字节 jpeg>" 这样的占位文本会当成值写回去，毁掉二进制封面
  $$('input[data-exif]').forEach(i => {
    if (i.readOnly || i.disabled) return;
    if (i.value.trim()) changes[i.dataset.exif] = i.value.trim();
  });
  $$('input[data-vtag]').forEach(i => {
    if (i.readOnly || i.disabled) return;
    if (i.value.trim()) changes[i.dataset.vtag] = i.value.trim();
  });
  return changes;
}

/* ─────────────── 保存 ─────────────── */
async function save() {
  if (!S.cur) return;
  const targets = $('#chkApplyAll').checked && S.sel.size
    ? [...S.sel] : [S.cur];

  const changes = collectChanges();
  const gps = readGpsDraft();
  const xmpVal = $('#xmpInput').value.trim();

  if (!Object.keys(changes).length && !gps && !xmpVal) {
    return toast('没有需要保存的改动', 'warn');
  }
  if (!confirm(`确认将改动写入 ${targets.length} 个文件？\n\n` +
               (targets.length > 1 ? '（已开启「应用到所有已选」）' : ''))) return;

  const body = {
    paths: targets,
    changes, gps,
    xmp: xmpVal || null,
    backup: $('#optBackup').checked,
  };
  const btn = $('#btnSave');
  btn.disabled = true; btn.textContent = '写入中…';
  try {
    const r = await post('/api/write', body);
    const warns = r.results.flatMap(x => x.warnings || []);
    if (r.ok) {
      toast(`已写入 ${targets.length} 个文件${$('#optBackup').checked ? '，备份已保存' : ''}`, 'ok');
      warns.forEach(w => toast(w, 'warn'));
      openFile(S.cur);   // 重新读取校验
    } else {
      const bad = r.results.filter(x => !x.ok);
      toast(`部分失败：${bad.map(x => `${x.path.split(/[\\/]/).pop()} ${x.error || ''}`).join('；')}`, 'err');
    }
  } catch (e) {
    toast('写入失败：' + e.message, 'err');
  } finally {
    btn.disabled = false; btn.textContent = '保存到文件';
  }
}

function readGpsDraft() {
  const lat = parseFloat($('#gpsLat').value);
  const lon = parseFloat($('#gpsLon').value);
  if (isNaN(lat) || isNaN(lon)) return null;
  const alt = parseFloat($('#gpsAlt').value);
  return { lat, lon, alt: isNaN(alt) ? null : alt };
}

function markDirty() {
  const dirty = $$('.dirty').length > 0;
  const fi = $('#footInfo');
  fi.textContent = dirty
    ? `${$$('.dirty').length} 项待保存的改动`
    : '未修改';
  fi.classList.toggle('dirty', dirty);
}

$('#btnSave').addEventListener('click', save);
$('#btnRevert').addEventListener('click', () => openFile(S.cur));
$('#btnPrev').addEventListener('click', () => openByOffset(-1));
$('#btnNext').addEventListener('click', () => openByOffset(1));

/* ─────────────── GPS ─────────────── */
$('#gpsInput').addEventListener('input', () => {
  const m = $('#gpsInput').value.match(/([+-]?\d+(?:\.\d+)?)/g);
  if (m && m.length >= 2) {
    $('#gpsLat').value = m[0];
    $('#gpsLon').value = m[1];
  }
});
$('#btnSaveGps').addEventListener('click', async () => {
  const gps = readGpsDraft();
  if (!gps) return toast('请填写有效的经纬度', 'warn');
  const targets = $('#chkApplyAll').checked && S.sel.size ? [...S.sel] : [S.cur];
  try {
    const r = await post('/api/write', {
      paths: targets, gps, backup: $('#optBackup').checked,
    });
    toast(r.ok ? `已写入 ${targets.length} 个文件的 GPS` : '部分写入失败',
          r.ok ? 'ok' : 'err');
    openFile(S.cur);
  } catch (e) { toast('GPS 写入失败：' + e.message, 'err'); }
});
$('#btnClearGps').addEventListener('click', async () => {
  if (!confirm('确认清除当前文件的 GPS 定位信息？')) return;
  try {
    const r = await post('/api/write', {
      paths: [S.cur], changes: {}, gps: null, xmp: null,
      backup: $('#optBackup').checked,
    });
    // gps=null 不会触发清除，需要显式调用清理
    await post('/api/scrub', { paths: [S.cur], preset: 'geo',
                                backup: $('#optBackup').checked });
    toast('GPS 已清除', 'ok');
    openFile(S.cur);
  } catch (e) { toast('清除失败：' + e.message, 'err'); }
});

/* ─────────────── 隐私清理 ─────────────── */
function renderPresets() {
  const ps = S.fields?.presets || [];
  $('#presets').innerHTML = ps.map(p => `
    <div class="preset ${p.id === S.preset ? 'on' : ''}" data-p="${p.id}">
      <b>${esc(p.name)}</b><span>${esc(p.desc)}</span>
    </div>`).join('');
  const cur = ps.find(p => p.id === S.preset);
  $('#presetDesc').textContent = cur ? cur.desc : '';
}
$('#presets').addEventListener('click', e => {
  const el = e.target.closest('.preset');
  if (!el) return;
  S.preset = el.dataset.p;
  renderPresets();
});
$('#btnScrub').addEventListener('click', async () => {
  const targets = S.sel.size ? [...S.sel] : (S.cur ? [S.cur] : []);
  if (!targets.length) return toast('请先选择文件', 'warn');
  const pname = S.fields?.presets.find(p => p.id === S.preset)?.name || S.preset;
  if (!confirm(`将对 ${targets.length} 个文件执行「${pname}」清理。\n\n` +
               '此操作不可撤销' + ($('#optBackup').checked ? '，但会自动创建备份。' : '。\n\n建议开启「自动备份」。') + '\n\n确认继续？')) return;

  const btn = $('#btnScrub');
  btn.disabled = true; btn.textContent = '清理中…';
  try {
    const r = await post('/api/scrub', {
      paths: targets, preset: S.preset, backup: $('#optBackup').checked,
    });
    toast(r.ok ? `已清理 ${targets.length} 个文件` : '部分文件清理失败', r.ok ? 'ok' : 'err');
    openFile(S.cur);
  } catch (e) {
    toast('清理失败：' + e.message, 'err');
  } finally {
    btn.disabled = false; btn.textContent = '对已选文件执行清理';
  }
});

/* ─────────────── 封面 ─────────────── */
function openCoverModal() {
  S.coverBlob = null;
  $('#coverPrev').hidden = true;
  $('#coverFile').value = '';
  $('#coverOk').disabled = true;
  $('#coverModal').hidden = false;
}
$('#btnSetCover').addEventListener('click', openCoverModal);
$('#coverCancel').addEventListener('click', () => $('#coverModal').hidden = true);
$('#coverModal').addEventListener('click', e => {
  if (e.target.id === 'coverModal') $('#coverModal').hidden = true;
});
$('#coverDrop').addEventListener('click', () => $('#coverFile').click());
$('#coverDrop').addEventListener('dragover', e => {
  e.preventDefault(); $('#coverDrop').classList.add('over');
});
$('#coverDrop').addEventListener('dragleave', () => $('#coverDrop').classList.remove('over'));
$('#coverDrop').addEventListener('drop', e => {
  e.preventDefault();
  $('#coverDrop').classList.remove('over');
  if (e.dataTransfer.files[0]) loadCover(e.dataTransfer.files[0]);
});
$('#coverFile').addEventListener('change', e => {
  if (e.target.files[0]) loadCover(e.target.files[0]);
});
function loadCover(file) {
  const r = new FileReader();
  r.onload = () => {
    S.coverBlob = r.result;
    $('#coverPrev').src = r.result;
    $('#coverPrev').hidden = false;
    $('#coverOk').disabled = false;
  };
  r.readAsDataURL(file);
}
$('#coverOk').addEventListener('click', async () => {
  if (!S.coverBlob) return;
  try {
    const isVideo = S.meta?.kind === 'video';
    const r = await post(isVideo ? '/api/cover/set' : '/api/thumbnail/set', {
      path: S.cur, image: S.coverBlob, backup: $('#optBackup').checked,
    });
    toast(isVideo ? '封面已更新' : '内嵌预览图已更新', 'ok');
    (r.warnings || []).forEach(w => toast(w, 'warn'));
    $('#coverModal').hidden = true;
    openFile(S.cur);
  } catch (e) { toast('封面设置失败：' + e.message, 'err'); }
});
$('#btnClearCover').addEventListener('click', async () => {
  if (!confirm('确认移除封面 / 内嵌预览图？')) return;
  try {
    const r = await post('/api/cover/remove', {
      path: S.cur, backup: $('#optBackup').checked,
    });
    (r.warnings || []).forEach(w => toast(w, 'warn'));
    toast('已移除封面 / 预览图', 'ok');
    openFile(S.cur);
  } catch (e) { toast('移除失败：' + e.message, 'err'); }
});

/* ─────────────── 重命名 ─────────────── */
function renderRnVars() {
  const vars = S.fields?.rename_vars || [];
  $('#rnVars').innerHTML = vars.map(v =>
    `<span class="vchip" data-v="{${esc(v)}}">{${esc(v)}}</span>`).join('');
}
$('#rnVars').addEventListener('click', e => {
  const c = e.target.closest('.vchip');
  if (!c) return;
  const inp = $('#rnTemplate');
  const s = inp.selectionStart || inp.value.length;
  inp.value = inp.value.slice(0, s) + c.dataset.v + inp.value.slice(inp.selectionEnd || s);
  inp.focus();
  inp.selectionStart = inp.selectionEnd = s + c.dataset.v.length;
});

async function rnPlan() {
  const targets = S.sel.size ? [...S.sel] : (S.cur ? [S.cur] : []);
  if (!targets.length) return toast('请先选择文件', 'warn');
  const q = new URLSearchParams({
    folder: $('#folderInput').value.trim(),
    template: $('#rnTemplate').value,
    start: $('#rnStart').value || '1',
    step: $('#rnStep').value || '1',
    paths: targets.join('|'),
  });
  try {
    const r = await api('/api/rename/plan?' + q);
    S.rnPlan = r.plan;
    $('#rnList').innerHTML = r.plan.length ? r.plan.map(p => `
      <div class="rn-item ${p.ok ? '' : 'bad'}">
        <div class="old" title="${esc(p.path)}">${esc(p.path.split(/[\\/]/).pop())}</div>
        <div class="arrow">→</div>
        <div class="new" title="${esc(p.new_path)}">${esc(p.new_name)}</div>
        ${p.ok ? '' : `<div class="why">${esc(p.reason)}</div>`}
      </div>`).join('') : '<div class="empty small">没有可重命名的文件</div>';
    const bad = r.plan.filter(p => !p.ok).length;
    toast(`生成 ${r.plan.length} 个方案${bad ? `，${bad} 个存在冲突` : ''}`,
          bad ? 'warn' : 'ok');
  } catch (e) { toast('预览失败：' + e.message, 'err'); }
}
$('#btnRnPlan').addEventListener('click', rnPlan);

$('#btnRnExec').addEventListener('click', async () => {
  if (!S.rnPlan.length) return toast('请先生成预览', 'warn');
  const ok = S.rnPlan.filter(p => p.ok).length;
  if (!ok) return toast('没有可执行的重命名项', 'warn');
  if (!confirm(`确认执行 ${ok} 个重命名操作？`)) return;
  try {
    const r = await post('/api/rename/execute', { plan: S.rnPlan });
    toast(`已重命名 ${r.renamed.length} 个文件` +
          (r.failed.length ? `，${r.failed.length} 个失败` : ''),
          r.failed.length ? 'warn' : 'ok');
    S.rnPlan = [];
    $('#rnList').innerHTML = '<div class="empty small">已执行，请重新预览</div>';
    S.sel.clear();
    scan();
  } catch (e) { toast('重命名失败：' + e.message, 'err'); }
});

/* ─────────────── 备份恢复 ─────────────── */
$('#btnRestore').addEventListener('click', async () => {
  const targets = S.sel.size ? [...S.sel] : (S.cur ? [S.cur] : []);
  const files = targets.map(p => p + '.mforge.bak').filter(p => true);
  const msg = '将查找以下备份文件并恢复：\n\n' +
    targets.map(p => '· ' + p.split(/[\\/]/).pop()).join('\n') +
    '\n\n注意：恢复会覆盖当前文件内容。确认继续？';
  if (!confirm(msg)) return;
  let n = 0;
  for (const p of targets) {
    const bak = p + '.mforge.bak';
    try {
      const r = await post('/api/restore', { backup: bak, original: p });
      if (r.ok) n++;
    } catch {}
  }
  toast(n ? `已恢复 ${n} 个文件` : '未找到可用的备份文件', n ? 'ok' : 'warn');
  if (n) openFile(S.cur);
});

/* ─────────────── Tabs ─────────────── */
$('#edTabs').addEventListener('click', e => {
  const t = e.target.closest('.tab');
  if (!t) return;
  $$('.tab').forEach(x => x.classList.toggle('active', x === t));
  $$('.pane').forEach(p =>
    p.classList.toggle('active', p.dataset.pane === t.dataset.tab));
});

/* ─────────────── 启动 ─────────────── */
(async function init() {
  try {
    S.fields = await api('/api/fields');
  } catch (e) {
    console.error('字段清单加载失败', e);
    toast('字段清单加载失败，部分功能不可用', 'err');
  }
  renderPresets();
  renderRnVars();

  // 支持 run.py --folder 直接带入目录
  const qf = new URLSearchParams(location.search).get('folder');
  if (qf) {
    $('#folderInput').value = qf;
    scan();
  }

  window.addEventListener('keydown', e => {
    if (e.target.matches('input, textarea')) return;
    if (e.key === 'ArrowLeft') openByOffset(-1);
    if (e.key === 'ArrowRight') openByOffset(1);
    if (e.key === 's' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
  });
})();

/* ─────────────── 心跳：关网页后服务自动退出 ─────────────── */
// 必须用 Web Worker 发心跳：浏览器会把隐藏标签页的页面定时器节流到
// 每分钟一次，普通 setInterval 会让服务被误判退出；Worker 不受此节流。
// 标签页关闭/浏览器退出时 Worker 随之终止，服务端约 1 分钟后自动结束。
(() => {
  const workerSrc = "const p=()=>fetch('/api/ping').catch(()=>{});p();setInterval(p,2000);";
  try {
    new Worker(URL.createObjectURL(new Blob([workerSrc], { type: 'application/javascript' })));
  } catch {
    setInterval(() => fetch('/api/ping').catch(() => {}), 2000);  // Worker 不可用时的兜底
  }

  // 页面级慢速探测只用于提示：服务没了时给用户一个明确说法（如睡眠唤醒后）
  let fails = 0, notified = false;
  setInterval(async () => {
    try {
      await fetch('/api/ping');
      fails = 0;
    } catch {
      if (++fails >= 3 && !notified) {
        notified = true;
        toast('MetaForge 服务已退出，请重新启动程序', 'err');
      }
    }
  }, 5000);
})();
