const state = {
  submitting: false,
};

const dom = {
  form: document.getElementById('video-audit-form'),
  videoUrl: document.getElementById('video-url'),
  feedback: document.getElementById('form-feedback'),
  empty: document.getElementById('results-empty'),
  content: document.getElementById('results-content'),
  status: document.getElementById('result-status'),
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
};

function setFeedback(message, tone = 'neutral') {
  dom.feedback.textContent = message;
  dom.feedback.className = `feedback feedback-${tone}`;
}

function setSubmitting(isSubmitting) {
  state.submitting = isSubmitting;
  const submitButton = dom.form.querySelector('button[type="submit"]');
  submitButton.disabled = isSubmitting;
  submitButton.textContent = isSubmitting ? 'Running audit…' : 'Audit video';
}

function renderIssues(issues = []) {
  dom.issuesList.innerHTML = '';

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

function renderAuditResult(result) {
  dom.empty.classList.add('hidden');
  dom.content.classList.remove('hidden');
  dom.status.textContent = result.status;
  dom.status.className = result.status === 'PASS' ? 'status-pass' : 'status-fail';
  dom.sessionId.textContent = result.session_id;
  dom.videoId.textContent = result.video_id;
  dom.report.textContent = result.final_report;
  dom.issueCount.textContent = `${result.compliance_results.length} issue${result.compliance_results.length === 1 ? '' : 's'}`;
  renderIssues(result.compliance_results);
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
    dom.frontend.textContent = config.features.frontend ? 'Ready' : 'Missing assets';
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

  setSubmitting(true);
  setFeedback('Submitting the video to the compliance pipeline. This may take a few minutes.', 'neutral');

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

    renderAuditResult(payload);
    setFeedback('Audit completed successfully.', 'success');
  } catch (error) {
    setFeedback(error.message, 'error');
  } finally {
    setSubmitting(false);
  }
}

dom.form.addEventListener('submit', handleSubmit);
loadSystemStatus();
