// Admin → Curriculum: trainers review and edit the key points the interviewer tests for each
// curriculum sub-topic. Data: GET /api/admin/curriculum; saves: POST /api/admin/curriculum/points
// and /api/admin/curriculum/reset (one sub-topic at a time). Uses escapeHtml() from admin.html.
let CUR = null;           // {domains: [{domain, label, modules: [{module, subtopics: [...]}]}]}
let CUR_DOM = 0;          // selected domain index
let CUR_FILTER = 'all';   // all | draft | needs_change | approved | edited
let CUR_Q = '';           // search text
let CUR_ITEMS = [];       // flat list of rendered sub-topics (index = data-i)

const CUR_STATUS = {
  draft:        {label: 'Not reviewed', color: 'var(--t3)',   bg: 'var(--sf)', bd: 'var(--b)'},
  approved:     {label: 'Approved',     color: 'var(--green)', bg: 'var(--gb)', bd: 'var(--gbd)'},
  needs_change: {label: 'Needs change', color: 'var(--amber)', bg: 'var(--ab)', bd: 'var(--abd)'},
};

async function loadCurriculum() {
  const root = document.getElementById('cur-root');
  try {
    const r = await fetch('/api/admin/curriculum', {credentials: 'include'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    CUR = await r.json();
    renderCurriculum();
  } catch (e) {
    root.innerHTML = `<div style="color:var(--red);font-size:12px">Could not load the curriculum: ${escapeHtml(e.message)}</div>`;
  }
}

function curAll(d) { return d.modules.flatMap(m => m.subtopics); }

function curCounts(list) {
  const c = {total: list.length, approved: 0, needs_change: 0, draft: 0, edited: 0};
  list.forEach(s => { c[s.status] = (c[s.status] || 0) + 1; if (s.edited) c.edited++; });
  return c;
}

function curMatches(s) {
  if (CUR_FILTER === 'edited' ? !s.edited : (CUR_FILTER !== 'all' && s.status !== CUR_FILTER)) return false;
  if (!CUR_Q) return true;
  const q = CUR_Q.toLowerCase();
  return s.id.toLowerCase().includes(q) || s.points.some(p => p.toLowerCase().includes(q));
}

function renderCurriculum() {
  const root = document.getElementById('cur-root');
  const dom = CUR.domains[CUR_DOM];
  const tabs = CUR.domains.map((d, i) => {
    const c = curCounts(curAll(d));
    const on = i === CUR_DOM;
    return `<button class="btn" onclick="curSetDomain(${i})" style="${on ? 'background:#111;color:#fff;border-color:#111' : ''}">
      ${escapeHtml(d.label)} <span style="opacity:.7">· ${c.approved}/${c.total} approved</span></button>`;
  }).join(' ');
  const c = curCounts(curAll(dom));
  const pct = c.total ? Math.round(c.approved / c.total * 100) : 0;
  const opt = (v, t) => `<option value="${v}" ${CUR_FILTER === v ? 'selected' : ''}>${t}</option>`;

  CUR_ITEMS = [];
  const searching = CUR_FILTER !== 'all' || CUR_Q;
  const groups = dom.modules.map((m, mi) => {
    const shown = m.subtopics.filter(curMatches);
    if (!shown.length) return '';
    const mc = curCounts(m.subtopics);
    const cards = shown.map(s => { CUR_ITEMS.push(s); return curCard(s, CUR_ITEMS.length - 1); }).join('');
    return `<details ${searching || mi === 0 ? 'open' : ''} style="margin-bottom:10px">
      <summary style="cursor:pointer;padding:10px 12px;background:var(--w);border:1px solid var(--b);border-radius:8px;font-weight:600;font-size:13px">
        ${escapeHtml(m.module)} <span style="font-weight:400;color:var(--t3);font-size:12px">— ${mc.total} sub-topics · ${mc.approved} approved · ${mc.needs_change} need change</span>
      </summary>
      <div style="display:flex;flex-direction:column;gap:10px;margin-top:10px">${cards}</div>
    </details>`;
  }).join('');

  root.innerHTML = `
    <div style="margin-bottom:16px">
      <div style="font-size:15px;font-weight:600;color:var(--t);margin-bottom:4px">Curriculum key points</div>
      <div style="font-size:12px;color:var(--t3)">The interviewer asks only within the student's domain curriculum and tests one of these key points per question. The points started as AI drafts — edit them to match the course, then mark each sub-topic Approved. Saved points are used by new questions within a minute; approving does not change interviews, it only tracks the review. One point per line.</div>
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px">${tabs}</div>
    <div class="card" style="display:flex;flex-wrap:wrap;align-items:center;gap:14px;margin-bottom:14px">
      <div style="flex:1;min-width:200px">
        <div style="font-size:12px;color:var(--t2);margin-bottom:6px">${c.approved} of ${c.total} approved · ${c.needs_change} need change · ${c.edited} edited</div>
        <div style="height:6px;background:var(--sf);border-radius:3px;overflow:hidden"><div style="height:100%;width:${pct}%;background:var(--green)"></div></div>
      </div>
      <select onchange="curSetFilter(this.value)" style="padding:7px 10px;border:1px solid var(--b);border-radius:7px;font-size:12px;font-family:inherit;background:var(--off)">
        ${opt('all', 'All sub-topics')}${opt('draft', 'Not reviewed')}${opt('needs_change', 'Needs change')}${opt('approved', 'Approved')}${opt('edited', 'Edited by a trainer')}
      </select>
      <input id="cur-search" placeholder="Search sub-topic or point…" value="${escapeHtml(CUR_Q)}" oninput="curSearch(this.value)"
        style="padding:7px 10px;border:1px solid var(--b);border-radius:7px;font-size:12px;font-family:inherit;background:var(--off);min-width:220px">
    </div>
    ${groups || '<div style="font-size:12px;color:var(--t3);padding:20px 0">No sub-topics match.</div>'}`;
  const inp = document.getElementById('cur-search');
  if (CUR_Q && inp) { inp.focus(); inp.setSelectionRange(CUR_Q.length, CUR_Q.length); }
}

function curCard(s, i) {
  const st = CUR_STATUS[s.status] || CUR_STATUS.draft;
  const who = s.edited || s.status !== 'draft'
    ? `Saved by ${escapeHtml(s.updated_by || 'admin')}${s.updated_at ? ' · ' + escapeHtml(new Date(s.updated_at).toLocaleString()) : ''}`
    : 'AI draft — not reviewed yet';
  return `<div class="card" id="cur-card-${i}" style="padding:14px">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px">
      <div style="font-weight:600;font-size:13px;color:var(--t);flex:1;min-width:200px">${escapeHtml(s.name)}</div>
      <span style="font-size:11px;padding:2px 9px;border-radius:20px;color:${st.color};background:${st.bg};border:1px solid ${st.bd}">${st.label}</span>
    </div>
    <textarea id="cur-pts-${i}" rows="${Math.max(3, s.points.length + 1)}"
      style="width:100%;padding:9px 11px;border:1px solid var(--b);border-radius:7px;font-size:12.5px;font-family:inherit;background:var(--off);color:var(--t);resize:vertical;line-height:1.6">${escapeHtml(s.points.join('\n'))}</textarea>
    <input id="cur-note-${i}" placeholder="Note for other trainers (optional)" value="${escapeHtml(s.note || '')}"
      style="width:100%;margin-top:8px;padding:7px 11px;border:1px solid var(--b);border-radius:7px;font-size:12px;font-family:inherit;background:var(--off);color:var(--t)">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px">
      <button class="btn" onclick="curSave(${i}, null)">Save</button>
      <button class="btn" onclick="curSave(${i}, 'approved')" style="color:var(--green);border-color:var(--gbd)">Save &amp; approve</button>
      <button class="btn" onclick="curSave(${i}, 'needs_change')" style="color:var(--amber);border-color:var(--abd)">Needs change</button>
      ${s.edited || s.status !== 'draft' ? `<button class="btn" onclick="curReset(${i})" title="Discard the saved review and go back to the AI draft">Reset to draft</button>` : ''}
      <span id="cur-msg-${i}" style="font-size:11.5px;color:var(--t3);margin-left:auto">${who}</span>
    </div>
  </div>`;
}

function curSetDomain(i) { CUR_DOM = i; renderCurriculum(); }
function curSetFilter(v) { CUR_FILTER = v; renderCurriculum(); }
let _curSearchT = null;
function curSearch(v) { clearTimeout(_curSearchT); _curSearchT = setTimeout(() => { CUR_Q = v.trim(); renderCurriculum(); }, 250); }

function curMsg(i, text, color) {
  const el = document.getElementById('cur-msg-' + i);
  if (el) { el.textContent = text; el.style.color = color; }
}

// Same sub-topic id can appear in two domains (shared "SL / Linux" lessons): update every copy.
function curUpdateAll(id, patch) {
  CUR.domains.forEach(d => curAll(d).forEach(s => { if (s.id === id) Object.assign(s, patch); }));
}

async function curSave(i, status) {
  const s = CUR_ITEMS[i];
  const points = document.getElementById('cur-pts-' + i).value.split('\n').map(p => p.trim()).filter(Boolean);
  const note = document.getElementById('cur-note-' + i).value.trim();
  if (!points.length) { curMsg(i, 'Add at least one key point.', 'var(--red)'); return; }
  if (status === 'needs_change' && !note) { curMsg(i, 'Add a note saying what needs to change.', 'var(--amber)'); return; }
  curMsg(i, 'Saving…', 'var(--t3)');
  try {
    const r = await fetch('/api/admin/curriculum/points', {
      method: 'POST', credentials: 'include', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subtopic: s.id, points, note, status: status || s.status})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
    const edited = JSON.stringify(d.entry.points) !== JSON.stringify(s.draft_points);
    curUpdateAll(s.id, {points: d.entry.points, note: d.entry.note, status: d.entry.status, edited,
                        updated_by: d.entry.updated_by, updated_at: d.entry.updated_at});
    renderCurriculum();
    const j = CUR_ITEMS.findIndex(x => x.id === s.id);
    if (j >= 0) curMsg(j, 'Saved ✓ — used by new questions within a minute', 'var(--green)');
  } catch (e) { curMsg(i, 'Not saved: ' + e.message, 'var(--red)'); }
}

async function curReset(i) {
  const s = CUR_ITEMS[i];
  curMsg(i, 'Resetting…', 'var(--t3)');
  try {
    const r = await fetch('/api/admin/curriculum/reset', {
      method: 'POST', credentials: 'include', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subtopic: s.id})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
    curUpdateAll(s.id, {points: d.points, note: '', status: 'draft', edited: false, updated_by: '', updated_at: ''});
    renderCurriculum();
  } catch (e) { curMsg(i, 'Not reset: ' + e.message, 'var(--red)'); }
}
