const state = {
  submitting: false,
  pollingTimer: null,
  activeSessionId: null,
};

const dom = {
  form: document.getElementById('video-audit-form'),
  videoUrl: document.getElementById('video-url'),
  feedback: document.getElementById('form-feedback'),
  empty: document.getElementById('results-empty'),
  content: document.getElementById('results-content'),
  status: document.getElementById('result-status'),
  statusCaption: document.getElementById('result-status-caption'),
  sessionId: document.getElementById('result-session-id'),
  videoId: document.getElementById('result-video-id'),
  report: document.getElementById('result-report'),
  issueCount: document.getElementById('issue-count'),
  issuesList: document.getElementById('issues-list'),
  errorsCard: document.getElementById('errors-card'),
  errorsList: document.getElementById('errors-list'),
  healthPill: document.getElementById('health-pill'),
  environment: document.getElementById('environment-value'),
  searchIndex: document.getElementById('search-index-value'),
  monitoring: document.getElementById('monitoring-value'),
  frontend: document.getElementById('frontend-value'),
  progressBadge: document.getElementById('progress-badge'),
  progressSteps: Array.from(document.querySelectorAll('.progress-step')),
};

function stopPolling() {
  if (state.pollingTimer) {
    clearTimeout(state.pollingTimer);
    state.pollingTimer = null;
  }
}

function setFeedback(message, tone = 'neutral') {
  dom.feedback.textContent = message;
  dom.feedback.className = `feedback feedback-${tone}`;
}

function setSubmitting(isSubmitting) {
  state.submitting = isSubmitting;
  const submitButton = dom.form.querySelector('button[type="submit"]');
  submitButton.disabled = isSubmitting;
  submitButton.textContent = isSubmitting ? 'Queueing audit…' : 'Audit video';
}

function renderIssues(issues = [], isPending = false) {
  dom.issuesList.innerHTML = '';

  if (isPending) {
    const pendingItem = document.createElement('li');
    pendingItem.className = 'issue-card issue-card-pending';
    pendingItem.innerHTML = `
      <div>
        <strong>Audit in progress</strong>
        <p>The backend is still processing the video and compliance checks.</p>
      </div>
    `;
    dom.issuesList.appendChild(pendingItem);
    return;
  }

  if (!issues.length) {
    const emptyItem = document.createElement('li');
    emptyItem.className = 'issue-card';
    emptyItem.innerHTML = `
      <div>
        <strong>No violations detected</strong>
        <p>The model did not identify any compliance issues for this video.</p>
      </div>
    `;
    dom.issuesList.appendChild(emptyItem);
    return;
  }

  for (const issue of issues) {
    const item = document.createElement('li');
    item.className = 'issue-card';
    item.innerHTML = `
      <div class="issue-card-header">
        <strong>${issue.category}</strong>
        <span class="pill ${issue.severity === 'CRITICAL' ? 'pill-danger' : 'pill-warning'}">${issue.severity}</span>
      </div>
      <p>${issue.description}</p>
      ${issue.timestamp ? `<small>Timestamp: ${issue.timestamp}</small>` : ''}
    `;
    dom.issuesList.appendChild(item);
  }
}

function renderErrors(errors = []) {
  dom.errorsList.innerHTML = '';

  if (!errors.length) {
    dom.errorsCard.classList.add('hidden');
    return;
  }

  dom.errorsCard.classList.remove('hidden');
  for (const error of errors) {
    const item = document.createElement('li');
    item.className = 'issue-card issue-card-error';
    item.textContent = error;
    dom.errorsList.appendChild(item);
  }
}

function applyStatusStyle(jobStatus, finalStatus) {
  const normalizedJobStatus = jobStatus || 'QUEUED';
  const normalizedFinalStatus = finalStatus || 'UNKNOWN';

  if (normalizedJobStatus === 'COMPLETED' && normalizedFinalStatus === 'PASS') {
    dom.status.className = 'status-pass';
    dom.progressBadge.className = 'pill pill-success';
    dom.progressBadge.textContent = 'Completed';
    return;
  }

  if (normalizedJobStatus === 'FAILED' || normalizedFinalStatus === 'FAIL') {
    dom.status.className = 'status-fail';
    dom.progressBadge.className = 'pill pill-danger';
    dom.progressBadge.textContent = 'Failed';
    return;
  }

  dom.status.className = 'status-pending';
  dom.progressBadge.className = normalizedJobStatus === 'QUEUED' ? 'pill pill-neutral' : 'pill pill-warning';
  dom.progressBadge.textContent = normalizedJobStatus === 'QUEUED' ? 'Queued' : 'Processing';
}

function renderProgress(jobStatus) {
  const normalized = jobStatus || 'QUEUED';
  const activeSteps = {
    QUEUED: ['queued'],
    PROCESSING: ['queued', 'processing'],
    COMPLETED: ['queued', 'processing', 'completed'],
    FAILED: ['queued', 'processing', 'failed'],
  }[normalized] || ['queued'];

  for (const step of dom.progressSteps) {
    const stepName = step.dataset.step;
    step.classList.toggle('progress-step-active', activeSteps.includes(stepName));
    step.classList.toggle('progress-step-failed', normalized === 'FAILED' && stepName === 'failed');
  }
}

function renderAuditResult(result) {
  const isPending = result.job_status === 'QUEUED' || result.job_status === 'PROCESSING';
  const statusLabel = isPending ? result.job_status : result.final_status;

  dom.empty.classList.add('hidden');
  dom.content.classList.remove('hidden');
  dom.status.textContent = statusLabel;
  dom.statusCaption.textContent = isPending
    ? 'Background processing is still running. The UI will refresh automatically.'
    : result.final_status === 'PASS'
      ? 'The audit completed successfully and no blocking issues were found.'
      : 'The audit completed with issues or captured processing errors.';
  applyStatusStyle(result.job_status, result.final_status);
  renderProgress(result.job_status);
  dom.sessionId.textContent = result.session_id;
  dom.videoId.textContent = result.video_id;
  dom.report.textContent = result.final_report;
  dom.issueCount.textContent = isPending
    ? 'Processing…'
    : `${result.compliance_results.length} issue${result.compliance_results.length === 1 ? '' : 's'}`;
  renderIssues(result.compliance_results, isPending);
  renderErrors(result.errors || []);
}

async function loadSystemStatus() {
  try {
    const [healthResponse, configResponse] = await Promise.all([
      fetch('/api/health'),
      fetch('/api/config'),
    ]);

    if (!healthResponse.ok || !configResponse.ok) {
      throw new Error('Unable to load service metadata.');
    }

    const health = await healthResponse.json();
    const config = await configResponse.json();

    dom.healthPill.textContent = health.status;
    dom.healthPill.className = 'pill pill-success';
    dom.environment.textContent = config.environment;
    dom.searchIndex.textContent = config.features.knowledge_base_index;
    dom.monitoring.textContent = config.features.azure_monitor ? 'Enabled' : 'Disabled';
    dom.frontend.textContent = `${config.features.frontend ? 'Ready' : 'Missing assets'} (${config.features.audit_mode})`;
  } catch (error) {
    dom.healthPill.textContent = 'Unavailable';
    dom.healthPill.className = 'pill pill-danger';
    dom.environment.textContent = 'Unknown';
    dom.searchIndex.textContent = 'Unknown';
    dom.monitoring.textContent = 'Unknown';
    dom.frontend.textContent = 'Unknown';
    setFeedback(error.message, 'error');
  }
}

async function pollAuditStatus(sessionId) {
  try {
    const response = await fetch(`/api/audit/${sessionId}`);
    const payload = await response.json();

    if (!response.ok) {
      const errorMessage = payload?.detail || 'Unable to fetch audit status.';
      throw new Error(errorMessage);
    }

    renderAuditResult(payload);

    if (payload.job_status === 'QUEUED' || payload.job_status === 'PROCESSING') {
      setFeedback('Audit accepted and still processing in the background. This prevents request timeouts.', 'neutral');
      state.pollingTimer = setTimeout(() => pollAuditStatus(sessionId), 5000);
      return;
    }

    setSubmitting(false);
    stopPolling();
    if (payload.job_status === 'COMPLETED') {
      setFeedback('Audit completed successfully.', 'success');
      return;
    }

    setFeedback('Audit finished with a failure state. Review the captured errors below.', 'error');
  } catch (error) {
    setSubmitting(false);
    stopPolling();
    setFeedback(error.message, 'error');
  }
}

async function handleSubmit(event) {
  event.preventDefault();
  if (state.submitting) {
    return;
  }

  const videoUrl = dom.videoUrl.value.trim();
  if (!videoUrl) {
    setFeedback('Please enter a valid YouTube URL.', 'error');
    return;
  }

  stopPolling();
  setSubmitting(true);
  setFeedback('Submitting the audit request. The backend will return immediately and continue processing in the background.', 'neutral');

  try {
    const response = await fetch('/api/audit', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ video_url: videoUrl }),
    });

    const payload = await response.json();
    if (!response.ok) {
      const errorMessage = payload?.detail?.message || payload?.detail?.error || 'Audit request failed.';
      throw new Error(errorMessage);
    }

    state.activeSessionId = payload.session_id;
    renderAuditResult({
      ...payload,
      compliance_results: [],
      errors: [],
    });
    state.pollingTimer = setTimeout(() => pollAuditStatus(payload.session_id), 1500);
  } catch (error) {
    setSubmitting(false);
    setFeedback(error.message, 'error');
  }
}

dom.form.addEventListener('submit', handleSubmit);
loadSystemStatus();