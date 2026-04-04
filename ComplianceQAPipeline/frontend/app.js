/**
 * Brand Guardian AI — Frontend App (v2.0)
 *
 * Key features:
 *  • Server-Sent Events (EventSource) for live agent log streaming
 *  • Pipeline step activation driven by SSE log keywords
 *  • Polling fallback for browsers / proxies that close SSE connections
 *  • Full results rendering with per-finding severity styling
 */

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
const state = {
  submitting:    false,
  sessionId:     null,
  pollingTimer:  null,
  eventSource:   null,
  streamDone:    false,
};

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const $  = id => document.getElementById(id);
const dom = {
  form:          $('audit-form'),
  videoUrl:      $('video-url'),
  submitBtn:     $('submit-btn'),
  feedback:      $('form-feedback'),

  healthDot:     $('health-dot'),
  healthLabel:   $('health-label'),

  cfgEnv:        $('cfg-environment'),
  cfgIndex:      $('cfg-index'),
  cfgRatelimit:  $('cfg-ratelimit'),
  cfgMonitor:    $('cfg-monitor'),
  cfgTavily:     $('cfg-tavily'),

  resultsEmpty:  $('results-empty'),
  resultsWrap:   $('results-wrap'),

  rSessionId:    $('r-session-id'),
  rVideoId:      $('r-video-id'),
  rJobBadge:     $('r-job-badge'),

  pipelineBadge: $('pipeline-badge'),

  steps: {
    indexer:    $('step-indexer'),
    supervisor: $('step-supervisor'),
    audio:      $('step-audio'),
    visual:     $('step-visual'),
    critic:     $('step-critic'),
  },

  terminalSection: $('terminal-section'),
  terminalBody:    $('terminal-body'),
  terminalBadge:   $('terminal-badge'),

  verdictCard:   $('verdict-card'),
  verdictBadge:  $('verdict-badge'),
  rReport:       $('r-report'),

  findingsCard:  $('findings-card'),
  findingsCount: $('findings-count'),
  findingsList:  $('findings-list'),

  errorsCard:    $('errors-card'),
  errorsList:    $('errors-list'),
};

// ---------------------------------------------------------------------------
// Feedback helpers
// ---------------------------------------------------------------------------
function setFeedback(msg, tone = 'neutral') {
  dom.feedback.textContent = msg;
  dom.feedback.className   = `feedback feedback-${tone}`;
}

function setSubmitting(on) {
  state.submitting    = on;
  dom.submitBtn.disabled  = on;
  dom.submitBtn.textContent = on ? 'Submitting…' : 'Audit Video';
}

// ---------------------------------------------------------------------------
// System status
// ---------------------------------------------------------------------------
async function loadSystemStatus() {
  try {
    const [hRes, cRes] = await Promise.all([
      fetch('/api/health'),
      fetch('/api/config'),
    ]);
    if (!hRes.ok || !cRes.ok) throw new Error('Status unavailable');

    const health = await hRes.json();
    const cfg    = await cRes.json();

    dom.healthDot.classList.add('healthy');
    dom.healthLabel.textContent = `${health.status} · ${cfg.environment}`;
    dom.cfgEnv.textContent        = cfg.environment;
    dom.cfgIndex.textContent      = cfg.features.knowledge_base_index || '—';
    dom.cfgRatelimit.textContent  = cfg.features.rate_limiter  || '—';
    dom.cfgMonitor.textContent    = cfg.features.azure_monitor ? '✓ Enabled' : '—';
    dom.cfgTavily.textContent     = cfg.features.tavily_search ? '✓ Enabled' : '—';
  } catch {
    dom.healthDot.classList.add('error');
    dom.healthLabel.textContent = 'Unavailable';
  }
}

// ---------------------------------------------------------------------------
// Pipeline step management
// ---------------------------------------------------------------------------
const STEP_KEYWORDS = {
  indexer:    ['Indexer', '📥', '⬇️', '☁️', '⏳', '🔍'],
  supervisor: ['Supervisor', '🧠'],
  audio:      ['Audio Agent', '🎙'],
  visual:     ['Visual Agent', '👁'],
  critic:     ['Critic', '⚖️'],
};

function setStep(name, mode) {
  // mode: 'idle' | 'running' | 'done' | 'error'
  const el = dom.steps[name];
  if (!el) return;
  el.classList.remove('active', 'running', 'done', 'error');
  const statusEl = el.querySelector('.pipe-status');
  if (mode === 'running') {
    el.classList.add('running');
    if (statusEl) statusEl.textContent = '⟳ Running';
  } else if (mode === 'done') {
    el.classList.add('done');
    if (statusEl) statusEl.textContent = '✓ Done';
  } else if (mode === 'error') {
    el.classList.add('active');
    if (statusEl) statusEl.textContent = '✗ Error';
  }
}

function detectStepFromLog(line) {
  for (const [step, keywords] of Object.entries(STEP_KEYWORDS)) {
    if (keywords.some(k => line.includes(k))) return step;
  }
  return null;
}

// Coarsely track which steps we've seen "complete" messages for
const _stepsCompleted = new Set();

function updatePipelineFromLog(line) {
  const step = detectStepFromLog(line);
  if (!step) return;

  if (line.includes('✅') || line.includes('complete') || line.includes('Complete')) {
    _stepsCompleted.add(step);
    setStep(step, 'done');
  } else if (line.includes('❌') || line.includes('Error') || line.includes('error')) {
    setStep(step, 'error');
  } else if (!_stepsCompleted.has(step)) {
    setStep(step, 'running');
  }
}

// ---------------------------------------------------------------------------
// Terminal rendering
// ---------------------------------------------------------------------------
function clearTerminal() {
  dom.terminalBody.innerHTML = '';
  dom.terminalSection.classList.remove('hidden');
}

function appendTerminalLine(text) {
  const placeholder = dom.terminalBody.querySelector('.terminal-placeholder');
  if (placeholder) placeholder.remove();

  const line = document.createElement('span');
  line.className = 'terminal-line';

  if (text.includes('❌') || text.toLowerCase().includes('error')) {
    line.classList.add('error');
  } else if (text.includes('✅') || text.includes('🏁')) {
    line.classList.add('success');
  } else if (text.includes('⚠️') || text.includes('warning')) {
    line.classList.add('warning');
  }

  line.textContent = text;
  dom.terminalBody.appendChild(line);
  dom.terminalBody.scrollTop = dom.terminalBody.scrollHeight;

  // Update pipeline vis
  updatePipelineFromLog(text);
}

function showTerminalCursor() {
  const cursor = document.createElement('span');
  cursor.className = 'terminal-cursor';
  cursor.id = 'terminal-cursor';
  dom.terminalBody.appendChild(cursor);
}

function removeTerminalCursor() {
  const c = document.getElementById('terminal-cursor');
  if (c) c.remove();
}

// ---------------------------------------------------------------------------
// Results rendering
// ---------------------------------------------------------------------------
function showResultsPanel() {
  dom.resultsEmpty.classList.add('hidden');
  dom.resultsWrap.classList.remove('hidden');
}

function updateMeta(job) {
  dom.rSessionId.textContent = job.session_id;
  dom.rVideoId.textContent   = job.video_id;

  dom.rJobBadge.textContent  = job.job_status;
  dom.rJobBadge.className    = 'badge';
  if (job.job_status === 'COMPLETED') dom.rJobBadge.classList.add('badge-green');
  else if (job.job_status === 'FAILED') dom.rJobBadge.classList.add('badge-red');
  else dom.rJobBadge.classList.add('badge-yellow');
}

function renderVerdict(job) {
  if (job.job_status !== 'COMPLETED' && job.job_status !== 'FAILED') return;

  dom.verdictCard.classList.remove('hidden');
  dom.rReport.textContent      = job.final_report || '—';

  dom.verdictBadge.textContent = job.final_status;
  dom.verdictBadge.className   = 'badge badge-large';
  dom.verdictBadge.classList.add(job.final_status === 'PASS' ? 'badge-green' : 'badge-red');

  dom.pipelineBadge.textContent = job.final_status === 'PASS' ? 'Passed' : 'Failed';
  dom.pipelineBadge.className   = `badge ${job.final_status === 'PASS' ? 'badge-green' : 'badge-red'}`;
}

function renderFindings(findings = []) {
  dom.findingsList.innerHTML = '';

  if (!findings.length) {
    dom.findingsCard.classList.remove('hidden');
    dom.findingsCount.textContent = '0 issues';
    const li = document.createElement('li');
    li.className = 'finding-item success';
    li.innerHTML = '<span class="finding-desc">✅ No compliance violations detected.</span>';
    dom.findingsList.appendChild(li);
    return;
  }

  dom.findingsCard.classList.remove('hidden');
  dom.findingsCount.textContent = `${findings.length} issue${findings.length === 1 ? '' : 's'}`;

  findings.forEach(issue => {
    const li = document.createElement('li');
    li.className = `finding-item ${issue.severity === 'CRITICAL' ? 'critical' : 'warning'}`;
    li.innerHTML = `
      <div class="finding-header">
        <span class="finding-category">${escHtml(issue.category)}</span>
        <span class="badge ${issue.severity === 'CRITICAL' ? 'badge-red' : 'badge-yellow'}">
          ${escHtml(issue.severity)}
        </span>
        <span class="finding-source">${escHtml(issue.source || 'unknown')}</span>
      </div>
      <p class="finding-desc">${escHtml(issue.description)}</p>
      ${issue.timestamp ? `<div class="finding-timestamp">⏱ ${escHtml(issue.timestamp)}</div>` : ''}
    `;
    dom.findingsList.appendChild(li);
  });
}

function renderErrors(errors = []) {
  if (!errors.length) { dom.errorsCard.classList.add('hidden'); return; }
  dom.errorsCard.classList.remove('hidden');
  dom.errorsList.innerHTML = '';
  errors.forEach(e => {
    const li = document.createElement('li');
    li.className = 'finding-item critical';
    li.innerHTML = `<p class="finding-desc">${escHtml(e)}</p>`;
    dom.errorsList.appendChild(li);
  });
}

function escHtml(str) {
  const d = document.createElement('div');
  d.textContent = String(str);
  return d.innerHTML;
}

// ---------------------------------------------------------------------------
// Polling fallback (called after SSE closes or on error)
// ---------------------------------------------------------------------------
function stopPolling() {
  if (state.pollingTimer) { clearTimeout(state.pollingTimer); state.pollingTimer = null; }
}

async function pollAuditStatus(sessionId) {
  try {
    const res  = await fetch(`/api/audit/${sessionId}`);
    const job  = await res.json();
    if (!res.ok) throw new Error(job?.detail || 'Poll failed');

    updateMeta(job);

    // Drain any new agent logs not yet seen in terminal
    (job.agent_logs || []).forEach(line => {
      if (!dom.terminalBody.textContent.includes(line)) {
        appendTerminalLine(line);
      }
    });

    if (job.job_status === 'COMPLETED' || job.job_status === 'FAILED') {
      removeTerminalCursor();
      dom.terminalBadge.textContent = 'Done';
      dom.terminalBadge.className   = 'badge badge-green';
      renderVerdict(job);
      renderFindings(job.compliance_results || []);
      renderErrors(job.errors || []);
      setSubmitting(false);
      setFeedback(
        job.job_status === 'COMPLETED' ? 'Audit complete.' : 'Audit finished with errors.',
        job.job_status === 'COMPLETED' ? 'success' : 'error',
      );
      return;
    }

    state.pollingTimer = setTimeout(() => pollAuditStatus(sessionId), 5000);
  } catch (err) {
    setSubmitting(false);
    setFeedback(err.message, 'error');
    stopPolling();
  }
}

// ---------------------------------------------------------------------------
// SSE streaming
// ---------------------------------------------------------------------------
function startStreaming(sessionId) {
  if (state.eventSource) { state.eventSource.close(); }

  const es = new EventSource(`/api/audit/${sessionId}/stream`);
  state.eventSource = es;

  es.onmessage = function(event) {
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }

    if (payload.type === 'log') {
      appendTerminalLine(payload.message);
    } else if (payload.type === 'complete') {
      state.streamDone = true;
      es.close();
      removeTerminalCursor();
      dom.terminalBadge.textContent = 'Done';
      dom.terminalBadge.className   = 'badge badge-green';
      // Fetch final state for results
      pollAuditStatus(sessionId);
    } else if (payload.type === 'error') {
      appendTerminalLine(`❌ ${payload.message}`);
      es.close();
    }
  };

  es.onerror = function() {
    if (!state.streamDone) {
      // SSE dropped (e.g. proxy timeout) — fall back to polling
      es.close();
      appendTerminalLine('⚠️ Live stream disconnected — switching to polling…');
      state.pollingTimer = setTimeout(() => pollAuditStatus(sessionId), 3000);
    }
  };
}

// ---------------------------------------------------------------------------
// Form submit
// ---------------------------------------------------------------------------
async function handleSubmit(event) {
  event.preventDefault();
  if (state.submitting) return;

  const url = dom.videoUrl.value.trim();
  if (!url) { setFeedback('Please enter a YouTube URL.', 'error'); return; }

  // Close prev SSE / polling
  if (state.eventSource) { state.eventSource.close(); state.eventSource = null; }
  stopPolling();
  _stepsCompleted.clear();

  // Reset pipeline UI
  Object.keys(dom.steps).forEach(s => {
    dom.steps[s].classList.remove('active', 'running', 'done', 'error');
    const st = dom.steps[s].querySelector('.pipe-status');
    if (st) st.textContent = '–';
  });
  dom.pipelineBadge.textContent = 'Running';
  dom.pipelineBadge.className   = 'badge badge-yellow';

  // Reset results
  dom.verdictCard.classList.add('hidden');
  dom.findingsCard.classList.add('hidden');
  dom.errorsCard.classList.add('hidden');

  state.streamDone = false;
  setSubmitting(true);
  setFeedback('Submitting audit request…', 'neutral');

  try {
    const res     = await fetch('/api/audit', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ video_url: url }),
    });
    const payload = await res.json();

    if (!res.ok) {
      const msg = payload?.detail?.[0]?.msg || payload?.detail || 'Submission failed.';
      throw new Error(msg);
    }

    state.sessionId = payload.session_id;
    showResultsPanel();
    updateMeta({ ...payload, job_status: 'QUEUED' });

    clearTerminal();
    showTerminalCursor();
    appendTerminalLine(`📋 Session ${payload.session_id} created.`);
    appendTerminalLine(`🔗 Streaming from ${payload.stream_url}`);

    setFeedback('Audit queued. Streaming live agent logs below.', 'neutral');
    startStreaming(payload.session_id);

  } catch (err) {
    setSubmitting(false);
    setFeedback(err.message, 'error');
  }
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
dom.form.addEventListener('submit', handleSubmit);
loadSystemStatus();