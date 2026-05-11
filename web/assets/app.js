const $ = (sel) => document.querySelector(sel);

const state = {
  jobId: null,
  eventSource: null,
  artifacts: {},
  original: '',
};

const sampleText = `Model Context Protocol（MCP）由 OpenAI 在 2023 年发布，最初是为了替代 HTTP API。到 2025 年，MCP 已经拥有 9700 万月下载量，并且 GitHub 上有超过 10 万个 MCP server。Anthropic 的 Claude Desktop 是第一个支持 MCP 的客户端。A2A 协议由 Google 提出，主要解决 Agent 之间互相调用的问题。ACP 已经完全并入 A2A，所以现在不再需要单独关注 ACP。`;

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
}

function setStatus(text) {
  $('#statusText').textContent = text;
}

function setTimeline(step, label, status = 'done', meta = '') {
  const id = `step-${step}`;
  let li = document.getElementById(id);
  if (!li) {
    li = document.createElement('li');
    li.id = id;
    $('#timeline').appendChild(li);
  }
  li.className = status;
  li.innerHTML = `
    <div class="dot">${status === 'done' ? '✓' : status === 'failed' ? '!' : '…'}</div>
    <div><div class="step-title">${escapeHtml(label)}</div><div class="step-meta">${escapeHtml(meta)}</div></div>
  `;
}

function resetUi() {
  state.artifacts = {};
  $('#timeline').innerHTML = '';
  $('#summaryCards').innerHTML = '';
  $('#suggestions').innerHTML = '';
  $('#claimsList').innerHTML = '';
  $('#planList').innerHTML = '';
  $('#evidenceList').innerHTML = '';
  $('#originalOut').textContent = '';
  $('#revisedOut').textContent = '';
  $('#reportOut').textContent = '';
  $('#claimCount').textContent = '0';
  $('#issueCount').textContent = '0';
}

function verdictClass(verdict) {
  if (verdict === 'supported') return 'supported';
  if (verdict === 'refuted') return 'refuted';
  if (verdict === 'not_enough_evidence') return 'missing';
  if (verdict === 'conflicting_evidence') return 'conflicting';
  return '';
}

function renderSummary() {
  const claims = state.artifacts.claims || [];
  const verdicts = state.artifacts.verdicts || [];
  const suggestions = state.artifacts.suggestions || [];
  const counts = verdicts.reduce((acc, v) => {
    const key = v.truth_verdict || 'unknown';
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
  const issueCount = verdicts.filter(v => !['supported', 'not_a_factual_claim'].includes(v.truth_verdict)).length;
  $('#claimCount').textContent = claims.length;
  $('#issueCount').textContent = issueCount;
  const cards = [
    ['claims', claims.length],
    ['supported', counts.supported || 0],
    ['refuted', counts.refuted || 0],
    ['needs review', issueCount],
  ];
  $('#summaryCards').innerHTML = cards.map(([label, value]) => `
    <div class="summary-card"><strong>${value}</strong><span>${label}</span></div>
  `).join('');
  $('#suggestions').innerHTML = suggestions.length ? suggestions.map(s => `
    <div class="item">
      <div class="item-title">${escapeHtml(s.claim_text || s.claim_id || 'Suggestion')}</div>
      <div class="item-meta">${escapeHtml(s.suggested_edit || s.action || s.reason || JSON.stringify(s))}</div>
    </div>
  `).join('') : '<p>还没有生成修改建议。</p>';
}

function renderClaims() {
  const claims = state.artifacts.claims || [];
  const verdicts = Object.fromEntries((state.artifacts.verdicts || []).map(v => [v.claim_id, v]));
  $('#claimsList').innerHTML = claims.length ? claims.map(c => {
    const v = verdicts[c.claim_id] || {};
    return `<div class="item">
      <div class="item-title"><span class="badge ${verdictClass(v.truth_verdict)}">${escapeHtml(v.truth_verdict || 'pending')}</span>${escapeHtml(c.claim_text)}</div>
      <div class="item-meta">${escapeHtml(c.claim_id)} · ${escapeHtml(c.claim_type)} · ${escapeHtml(c.subject)} · confidence ${escapeHtml(v.confidence ?? '')}</div>
      ${v.reason ? `<p>${escapeHtml(v.reason)}</p>` : ''}
    </div>`;
  }).join('') : '<p>还没有 claims。</p>';
}

function renderPlan() {
  const plans = state.artifacts.plan || [];
  $('#planList').innerHTML = plans.length ? plans.map(p => `
    <div class="item">
      <div class="item-title"><span class="badge">${escapeHtml(p.source_kind || 'source')}</span>${escapeHtml(p.claim_id || '')} · ${escapeHtml(p.execution_method || '')}</div>
      <div class="item-meta">authority: ${escapeHtml(p.authority_target || '')} · adapter: ${escapeHtml(p.adapter_status || '')}</div>
      <p>${escapeHtml(p.rationale || '')}</p>
      <div class="item-meta">locator: ${escapeHtml((p.locator_hints || []).join(' / '))}</div>
      <div class="item-meta">query: ${escapeHtml((p.queries || []).join(' | '))}</div>
    </div>
  `).join('') : '<p>还没有 verification plan。</p>';
}

function renderEvidence() {
  const evidence = state.artifacts.evidence || [];
  $('#evidenceList').innerHTML = evidence.length ? evidence.map(e => `
    <div class="item">
      <div class="item-title"><span class="badge">${escapeHtml(e.source_type || 'source')}</span>${escapeHtml(e.claim_id || e.evidence_id || '')}</div>
      <div class="item-meta">authority ${escapeHtml(e.authority || '')} · match ${escapeHtml(e.subject_match || '')}</div>
      <p>${escapeHtml(e.quote || e.url || JSON.stringify(e).slice(0, 500))}</p>
    </div>
  `).join('') : '<p>还没有 evidence。missing 模式会生成离线复核记录；auto/live 模式会尝试找外部证据。</p>';
}

function renderRevision() {
  $('#originalOut').textContent = state.original;
  $('#revisedOut').textContent = state.artifacts.revised || '还没有 revised.md。';
  $('#reportOut').textContent = state.artifacts.report || '还没有 actual-report.md。';
}

function renderAll() {
  renderSummary();
  renderClaims();
  renderPlan();
  renderEvidence();
  renderRevision();
}

async function runAudit() {
  const text = $('#inputText').value.trim();
  if (text.length < 20) {
    alert('先粘贴一段至少 20 个字符的文本。');
    return;
  }
  resetUi();
  state.original = text;
  $('#runBtn').disabled = true;
  setStatus('提交中');
  setTimeline('queued', '提交审查任务', 'running');

  const payload = {
    text,
    evidence_mode: $('#evidenceMode').value,
    claim_extractor: $('#claimExtractor').value,
    max_claims: Number($('#maxClaims').value || 16),
    post_audit_revision: 'none',
  };
  const resp = await fetch('/api/audit', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const data = await resp.json();
  if (!resp.ok) {
    $('#runBtn').disabled = false;
    setTimeline('queued', '提交失败', 'failed', data.error || resp.statusText);
    setStatus('失败');
    return;
  }
  state.jobId = data.job_id;
  $('#jobId').textContent = data.job_id;
  setTimeline('queued', '提交审查任务', 'done', `job ${data.job_id}`);
  connectEvents(data.job_id);
}

function connectEvents(jobId) {
  if (state.eventSource) state.eventSource.close();
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  state.eventSource = es;
  es.addEventListener('status', ev => {
    const data = JSON.parse(ev.data);
    setStatus(data.status === 'running' ? '审查中' : data.status);
    if (data.status === 'running') setTimeline('running', '后端审查进程启动', 'running');
  });
  es.addEventListener('artifact', ev => {
    const data = JSON.parse(ev.data);
    state.artifacts[data.step] = data.data;
    setTimeline(data.step, data.label, 'done', data.file);
    renderAll();
  });
  es.addEventListener('complete', ev => {
    const data = JSON.parse(ev.data);
    state.artifacts = {...state.artifacts, ...(data.artifacts || {})};
    renderAll();
    $('#runBtn').disabled = false;
    setStatus(data.status === 'done' ? '完成' : '失败');
    setTimeline('running', '后端审查进程启动', data.status === 'done' ? 'done' : 'failed', data.status === 'done' ? 'process exited 0' : (data.error || 'process failed'));
    setTimeline('complete', data.status === 'done' ? '审查完成' : '审查失败', data.status === 'done' ? 'done' : 'failed', data.error || '');
    es.close();
  });
  es.addEventListener('error', () => {
    $('#runBtn').disabled = false;
    setStatus('连接中断');
  });
}

function setupTabs() {
  document.querySelectorAll('.tab').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(x => x.classList.remove('active'));
      btn.classList.add('active');
      document.getElementById(btn.dataset.tab).classList.add('active');
    });
  });
}

$('#runBtn').addEventListener('click', runAudit);
$('#sampleBtn').addEventListener('click', () => { $('#inputText').value = sampleText; });
setupTabs();
renderAll();
