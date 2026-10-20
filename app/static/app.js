const $ = (selector) => document.querySelector(selector);
const state = { assets: [], creators: [], jobs: [], youtube: [], projects: [], candidates: [], health: {} };
const routeTitles = { overview: 'Overview', creators: 'Creator sources', library: 'Clip library', editor: 'Editor', review: 'Review queue', publishing: 'Publishing', history: 'Production history', settings: 'Settings' };
let activeEditorAssetId = '';
let activeProjectId = '';
let timelineSegments = [];
let sourceTranscriptCues = [];

function showNotice(message) {
  const notice = $('#notice');
  notice.textContent = message;
  notice.classList.add('show');
  window.clearTimeout(showNotice.timer);
  showNotice.timer = window.setTimeout(() => notice.classList.remove('show'), 7000);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status}).`);
  return body;
}

function duration(value) {
  if (value == null) return 'Not inspected';
  const seconds = Math.round(value);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function localDateTime(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? '' : new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

function badge(status) {
  const cls = status === 'granted' || status === 'approved' || status === 'inspected' ? 'good' : status === 'unverified' || status === 'inspection_required' || status === 'failed' ? 'warn' : 'muted';
  return `<span class="badge ${cls}">${escapeHtml(String(status).replaceAll('_', ' '))}</span>`;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

function renderAssets() {
  const selectedAssetId = $('#edit-asset').value;
  const list = $('#asset-list');
  if (!state.assets.length) {
    list.innerHTML = '<tr><td colspan="5" class="empty">Your library is empty. Import creator-supplied footage to get started.</td></tr>';
  } else {
    list.innerHTML = state.assets.map(asset => {
      const permission = asset.permission_valid ? asset.permission_status : asset.permission_status === 'granted' ? 'expired' : asset.permission_status;
      return `<tr><td><b>${escapeHtml(asset.filename)}</b><br><small>${escapeHtml((asset.sha256 || '').slice(0, 14))}… · ${(asset.size_bytes / 1048576).toFixed(1)} MB</small></td><td>${badge(asset.status)}<br><small>${duration(asset.duration)}${asset.width ? ` · ${asset.width}×${asset.height}${asset.frame_rate ? ` · ${Number(asset.frame_rate).toFixed(2)} fps` : ''}` : ''}</small></td><td>${badge(permission)}<br><small>Commercial: ${escapeHtml(asset.commercial_permission_valid ? 'cleared' : asset.commercial_permission_status || 'unverified')}</small>${asset.creator_name ? `<br><small>${escapeHtml(asset.creator_name)}</small>` : ''}</td><td><small>${new Date(asset.imported_at).toLocaleString()}</small></td><td>${asset.status === 'inspection_required' ? `<button class="table-action" data-inspect="${escapeHtml(asset.id)}">Inspect</button><br>` : ''}<button class="table-action" data-permission="${escapeHtml(asset.id)}">Rights record</button></td></tr>`;
    }).join('');
  }
  list.querySelectorAll('[data-permission]').forEach(button => button.addEventListener('click', () => openPermission(button.dataset.permission)));
  list.querySelectorAll('[data-inspect]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try { await api(`/api/assets/${encodeURIComponent(button.dataset.inspect)}/inspect`, { method: 'POST' }); await refresh(); showNotice('Source metadata inspected successfully.'); }
    catch (error) { showNotice(error.message); button.disabled = false; }
  }));
  const commercialRequired = $('#commercial-use').checked;
  const eligible = state.assets.filter(asset => asset.duration && asset.permission_valid && (!commercialRequired || asset.commercial_permission_valid));
  $('#edit-asset').innerHTML = '<option value="">Select a source…</option>' + state.assets.map(asset => {
    const permission = asset.permission_valid ? asset.permission_status : asset.permission_status === 'granted' ? 'expired' : asset.permission_status;
    return `<option value="${escapeHtml(asset.id)}">${escapeHtml(asset.filename)} · ${escapeHtml(permission)}</option>`;
  }).join('');
  $('#edit-asset').value = selectedAssetId;
  $('#stat-assets').textContent = state.assets.length;
  $('#stat-permission').textContent = state.assets.filter(asset => !asset.permission_valid).length;
  $('#stat-review').textContent = state.jobs.filter(job => job.status === 'review').length;
  $('#stat-approved').textContent = state.jobs.filter(job => job.status === 'approved').length;
  if (!eligible.length) $('#edit-asset').title = 'Import, inspect, and record current permission for a source first.';
}

function renderYoutubeCandidates() {
  const list = $('#youtube-list');
  if (!list) return;
  list.innerHTML = state.youtube.length ? state.youtube.map(candidate => `<tr><td><a href="${escapeHtml(candidate.url)}" target="_blank" rel="noopener noreferrer">Open video on YouTube ↗</a><br><small>Reference only · no media fetched</small></td><td><code>${escapeHtml(candidate.video_id)}</code></td><td>${badge(candidate.status)}</td></tr>`).join('') : '<tr><td colspan="3" class="empty">No YouTube references saved.</td></tr>';
}

function renderCreators() {
  $('#creator-select').innerHTML = '<option value="">Select a creator…</option>' + state.creators.map(creator => `<option value="${escapeHtml(creator.id)}">${escapeHtml(creator.name)}</option>`).join('');
  const list = $('#creator-list');
  if (list) list.innerHTML = state.creators.length ? state.creators.map(creator => `<tr><td><b>${escapeHtml(creator.name)}</b></td><td>${creator.source_url ? `<a href="${escapeHtml(creator.source_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(creator.source_url)}</a>` : '—'}</td><td><small>${new Date(creator.created_at).toLocaleString()}</small></td></tr>`).join('') : '<tr><td colspan="3" class="empty">No creator records yet.</td></tr>';
}

function renderTimeline() {
  const list = $('#timeline-list');
  const total = timelineSegments.reduce((sum, segment) => sum + segment.end_seconds - segment.start_seconds, 0);
  $('#timeline-total').textContent = timelineSegments.length + ' segment' + (timelineSegments.length === 1 ? '' : 's') + ' · ' + duration(total);
  if (!timelineSegments.length) $('#timeline-total').textContent = 'No segments saved';
  list.innerHTML = timelineSegments.length ? timelineSegments.map((segment, index) =>
    '<div class="timeline-row"><span class="segment-index">' + String(index + 1).padStart(2, '0') +
    '</span><span>' + duration(segment.start_seconds) + ' → ' + duration(segment.end_seconds) + ' · ' +
    (segment.end_seconds - segment.start_seconds).toFixed(1) + 's</span><span class="segment-tools">' +
    '<button type="button" data-move-segment="' + index + '" data-direction="-1" aria-label="Move segment up" ' +
    (index === 0 ? 'disabled' : '') + '>↑</button><button type="button" data-move-segment="' + index +
    '" data-direction="1" aria-label="Move segment down" ' + (index === timelineSegments.length - 1 ? 'disabled' : '') +
    '>↓</button><button type="button" data-remove-segment="' + index + '" aria-label="Remove segment">×</button></span></div>').join('') :
    '<div class="empty">Set in/out points and add a segment.</div>';
  list.querySelectorAll('[data-move-segment]').forEach(button => button.addEventListener('click', () => {
    const index = Number(button.dataset.moveSegment), next = index + Number(button.dataset.direction);
    if (next < 0 || next >= timelineSegments.length) return;
    [timelineSegments[index], timelineSegments[next]] = [timelineSegments[next], timelineSegments[index]];
    renderTimeline();
  }));
  list.querySelectorAll('[data-remove-segment]').forEach(button => button.addEventListener('click', () => {
    timelineSegments.splice(Number(button.dataset.removeSegment), 1);
    renderTimeline();
  }));
}

function renderProjects() {
  const select = $('#project-select');
  const sourceId = $('#edit-asset').value;
  const projects = state.projects.filter(project => project.asset_id === sourceId);
  select.innerHTML = '<option value="">New timeline</option>' + projects.map(project =>
    '<option value="' + escapeHtml(project.id) + '">' + escapeHtml(project.title) + ' · v' + project.current_version +
    ' · ' + escapeHtml(project.review_status || 'draft') + '</option>').join('');
  select.value = activeProjectId && projects.some(project => project.id === activeProjectId) ? activeProjectId : '';
  renderTimeline();
}

function renderCandidates() {
  const list = $('#candidate-list');
  const sourceId = $('#edit-asset').value;
  const candidates = state.candidates.filter(candidate => candidate.asset_id === sourceId);
  if (!sourceId || !candidates.length) {
    list.innerHTML = '<div class="panel empty">Select an inspected source and find candidate clips to start.</div>';
    return;
  }
  list.innerHTML = candidates.map(candidate => {
    const id = escapeHtml(candidate.id), preview = '/api/assets/' + encodeURIComponent(candidate.asset_id) +
      '/source#t=' + candidate.start_seconds + ',' + candidate.end_seconds;
    const factors = (candidate.score_factors || []).map(factor => factor.name.replaceAll('_', ' ') + ' +' + factor.points).join(' · ');
    const warning = (candidate.warnings || []).map(escapeHtml).join('<br>');
    const reasons = (candidate.reasons || []).map(escapeHtml).join('<br>');
    const accepted = candidate.status === 'pending';
    const action = accepted ? '<button class="button primary" data-candidate-decision="' + id + '" data-decision="accepted">Accept for editing</button>' +
      '<button class="button" data-candidate-decision="' + id + '" data-decision="rejected">Reject</button>' :
      (candidate.project_id ? '<button class="button" data-open-project="' + escapeHtml(candidate.project_id) + '">Open edit project</button>' +
       '<label><input type="checkbox" data-batch-project="' + escapeHtml(candidate.project_id) + '"> Select for batch render</label>' : '');
    return '<article class="panel candidate-card"><video class="candidate-preview" controls preload="metadata" src="' + preview +
      '"></video><div class="candidate-body"><div class="candidate-title"><b>' + duration(candidate.start_seconds) + ' → ' +
      duration(candidate.end_seconds) + ' · ' + candidate.duration.toFixed(1) + ' sec</b>' + badge(candidate.status) +
      '<span class="badge muted">Review score ' + candidate.score + '</span></div><div class="candidate-signals">' + reasons +
      '<br>Signals: ' + escapeHtml((candidate.signals || []).join(', ') || 'none') + '<br>Factors: ' + escapeHtml(factors || 'baseline review window') +
      '</div>' + (warning ? '<div class="candidate-warning">' + warning + '</div>' : '') +
      '<div class="candidate-fields"><label>IN<input data-candidate-start="' + id + '" type="number" min="0" step="0.1" value="' +
      candidate.start_seconds + '"></label><label>OUT<input data-candidate-end="' + id + '" type="number" min="0" step="0.1" value="' +
      candidate.end_seconds + '"></label><label class="wide">Title<input data-candidate-title="' + id + '" maxlength="120" value="' +
      escapeHtml(candidate.title || '') + '"></label><label class="wide">Hook (editorial note, not generated speech)<input data-candidate-hook="' + id +
      '" maxlength="240" value="' + escapeHtml(candidate.hook || '') + '"></label><label class="wide">Review notes<textarea data-candidate-notes="' +
      id + '" maxlength="1000" rows="2">' + escapeHtml(candidate.notes || '') + '</textarea></label></div><div class="candidate-actions">' +
      action + '</div></div></article>';
  }).join('');
  list.querySelectorAll('[data-candidate-decision]').forEach(button => button.addEventListener('click', () =>
    decideCandidate(button.dataset.candidateDecision, button.dataset.decision)));
  list.querySelectorAll('[data-open-project]').forEach(button => button.addEventListener('click', () => loadEditProject(button.dataset.openProject)));
}

function captionStyleFromControls() {
  return { font_name: $('#caption-font').value, font_size: Number($('#caption-size').value),
    color: $('#caption-color').value, outline: Number($('#caption-outline').value),
    shadow: Number($('#caption-shadow').value), placement: $('#caption-placement').value,
    safe_margin: Number($('#caption-margin').value), max_line_length: Number($('#caption-line-length').value),
    wrap: $('#caption-wrap').checked };
}

function setCaptionStyle(style) {
  $('#caption-font').value = style.font_name;
  $('#caption-size').value = style.font_size;
  $('#caption-color').value = style.color;
  $('#caption-outline').value = style.outline;
  $('#caption-shadow').value = style.shadow;
  $('#caption-placement').value = style.placement;
  $('#caption-margin').value = style.safe_margin;
  $('#caption-line-length').value = style.max_line_length;
  $('#caption-wrap').checked = style.wrap;
}

function applyCaptionPreset(name) {
  const style = name === 'shorts' ? { font_name: 'Arial', font_size: 30, color: '#FFFFFF', outline: 3,
    shadow: 2, placement: 'bottom', safe_margin: 160, max_line_length: 24, wrap: true } :
    name === 'long_form' ? { font_name: 'Arial', font_size: 22, color: '#FFFFFF', outline: 2,
      shadow: 1, placement: 'bottom', safe_margin: 70, max_line_length: 42, wrap: true } : null;
  if (style) setCaptionStyle(style);
}

function parseTranscriptSrt(source, sourceDuration) {
  const normalized = source.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').trim();
  if (!normalized) return [];
  const blocks = normalized.split(/\n\s*\n/);
  if (blocks.length > 1000) throw new Error('Transcript SRT is limited to 1,000 timed cues.');
  const cues = blocks.map((block, index) => {
    const lines = block.split('\n');
    if (lines.length < 3 || lines[0].trim() !== String(index + 1)) throw new Error('Transcript cues need sequential numbers and valid SRT timing.');
    const match = lines[1].trim().match(/^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3}) --> (\d{2,}):(\d{2}):(\d{2})[,.](\d{3})$/);
    if (!match) throw new Error('Transcript timestamps must use HH:MM:SS,mmm --> HH:MM:SS,mmm.');
    const toSeconds = (hour, minute, second, milli) => ((Number(hour) * 60 + Number(minute)) * 60 + Number(second)) + Number(milli) / 1000;
    const start_seconds = toSeconds(...match.slice(1, 5)), end_seconds = toSeconds(...match.slice(5, 9));
    const text = lines.slice(2).join(' ').trim();
    if (!(start_seconds >= 0 && end_seconds > start_seconds && end_seconds <= sourceDuration) || text.length > 500) throw new Error('Transcript timing must fit within source duration and each cue must be 500 characters or less.');
    return { start_seconds, end_seconds, text };
  });
  return cues;
}

async function saveEditProject() {
  const assetId = $('#edit-asset').value;
  if (!assetId) throw new Error('Select a source first.');
  const segments = timelineSegments.length ? timelineSegments : [{ start_seconds: Number($('#start').value), end_seconds: Number($('#end').value) }];
  const payload = { title: $('#project-title').value, segments, profile: $('#render-profile').value,
    content_preset: $('#content-preset').value, preset: $('#preset').value, crop_x: Number($('#crop-x').value),
    crop_y: Number($('#crop-y').value), commercial_use: $('#commercial-use').checked,
    editorial_notes: $('#editorial-notes').value, captions_srt: $('#captions-srt').value,
    caption_preset: $('#caption-preset').value, caption_style: captionStyleFromControls(),
    transcript_cues: sourceTranscriptCues };
  const url = activeProjectId ? '/api/projects/' + encodeURIComponent(activeProjectId) : '/api/assets/' + encodeURIComponent(assetId) + '/projects';
  const project = await api(url, { method: activeProjectId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
  activeProjectId = project.id;
  timelineSegments = project.plan.segments.map(segment => ({ ...segment }));
  await refresh();
  renderProjects();
  showNotice('Saved ' + project.title + ' · version ' + project.current_version + '.');
  return project;
}

async function loadEditProject(projectId) {
  try {
    const project = await api('/api/projects/' + encodeURIComponent(projectId));
    activeProjectId = project.id;
    $('#edit-asset').value = project.asset_id;
    renderSourceMeta();
    $('#project-title').value = project.plan.title;
    $('#render-profile').value = project.plan.editing.profile;
    $('#content-preset').value = project.plan.editing.content_preset;
    $('#preset').value = project.plan.editing.visual_preset;
    $('#crop-x').value = project.plan.editing.crop.x;
    $('#crop-y').value = project.plan.editing.crop.y;
    $('#captions-srt').value = project.plan.captions.text;
    sourceTranscriptCues = project.plan.transcript_cues || [];
    $('#transcript-status').textContent = sourceTranscriptCues.length + ' saved transcript cues loaded for timed overlap only.';
    $('#caption-preset').value = project.plan.captions.preset;
    setCaptionStyle(project.plan.captions.style);
    $('#editorial-notes').value = project.plan.editorial_notes;
    $('#commercial-use').checked = project.plan.commercial_use;
    timelineSegments = project.plan.segments.map(segment => ({ ...segment }));
    if (timelineSegments.length) {
      $('#start').value = timelineSegments[0].start_seconds;
      $('#end').value = timelineSegments[0].end_seconds;
    }
    renderProjects();
    updateCropPreview();
    location.hash = '#editor';
    showNotice('Loaded ' + project.title + ' · version ' + project.current_version + '.');
  } catch (error) { showNotice(error.message); }
}

async function decideCandidate(candidateId, decision) {
  const selector = '[data-candidate-' ;
  const start = $(selector + 'start="' + CSS.escape(candidateId) + '"]');
  const end = $(selector + 'end="' + CSS.escape(candidateId) + '"]');
  const title = $(selector + 'title="' + CSS.escape(candidateId) + '"]');
  const hook = $(selector + 'hook="' + CSS.escape(candidateId) + '"]');
  const notes = $(selector + 'notes="' + CSS.escape(candidateId) + '"]');
  try {
    await api('/api/candidates/' + encodeURIComponent(candidateId) + '/decision', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision, start_seconds: Number(start.value),
        end_seconds: Number(end.value), title: title.value, hook: hook.value, notes: notes.value,
        profile: $('#render-profile').value, content_preset: $('#content-preset').value, preset: $('#preset').value,
        caption_preset: $('#caption-preset').value, caption_style: captionStyleFromControls(),
        transcript_cues: sourceTranscriptCues }) });
    await refresh();
    showNotice(decision === 'accepted' ? 'Candidate accepted into an editable draft project. It is not output-approved.' : 'Candidate rejected and recorded.');
  } catch (error) { showNotice(error.message); }
}

async function discoverCandidates() {
  const assetId = $('#edit-asset').value;
  if (!assetId) return showNotice('Select a source first.');
  const button = $('#discover-button');
  button.disabled = true;
  try {
    const result = await api('/api/assets/' + encodeURIComponent(assetId) + '/discover', { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content_preset: $('#content-preset').value,
        transcript_cues: sourceTranscriptCues }) });
    await refresh();
    showNotice(result.candidates.length + ' review-only candidates found. ' + (result.diagnostics.join(' ') || result.scoring_note));
  } catch (error) { showNotice(error.message); }
  finally { button.disabled = false; }
}

async function renderSelectedProjects() {
  const selected = [...document.querySelectorAll('[data-batch-project]:checked')].map(input => input.dataset.batchProject);
  if (!selected.length) return showNotice('Accept and select one or more candidate projects first.');
  let queued = 0;
  for (const projectId of selected) {
    try {
      await api('/api/projects/' + encodeURIComponent(projectId) + '/render', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: '{}' });
      queued += 1;
    } catch (error) { showNotice(queued + ' projects queued before the queue stopped: ' + error.message); break; }
  }
  await refresh();
  location.hash = '#review';
  if (queued) showNotice(queued + ' accepted timeline' + (queued === 1 ? '' : 's') + ' queued. Each output still needs human review.');
}

function qualitySummary(source) {
  try {
    const report = JSON.parse(source);
    const checks = Array.isArray(report.checks) ? report.checks : [];
    const summary = checks.map(check => `${escapeHtml(check.name)}: ${escapeHtml(check.status)}`).join(' · ');
    const warnings = Array.isArray(report.warnings) ? report.warnings.map(escapeHtml).join('<br>') : '';
    return `<p>QA ${escapeHtml(report.quality_status || 'UNKNOWN')} · ${escapeHtml(report.width)} × ${escapeHtml(report.height)} · ${duration(report.duration)}</p>${summary ? `<p>${summary}</p>` : ''}${warnings ? `<p>Warnings: ${warnings}</p>` : ''}`;
  } catch {
    return '<p>Quality report could not be read.</p>';
  }
}

function renderJobs() {
  const review = state.jobs.filter(job => ['queued', 'running', 'review', 'approved', 'changes_requested', 'rejected', 'failed'].includes(job.status));
  const list = $('#review-list');
  list.innerHTML = review.length ? review.map(job => `<article class="panel review-card" data-profile="${escapeHtml(job.profile || 'short_vertical')}">${job.output_path ? `<video class="review-video" controls preload="metadata" src="/api/files/outputs/${encodeURIComponent(job.output_path)}"></video>` : `<div class="review-thumb">${['queued', 'running'].includes(job.status) ? 'Local render' : 'No rendered file'}</div>`}<div><h3>${escapeHtml(job.filename)} · ${duration(job.end_seconds - job.start_seconds)}</h3><p>${escapeHtml(job.profile || 'short_vertical')} · Preset: ${escapeHtml(job.preset)} · Status: ${escapeHtml(job.status)}${job.stage ? ` · ${escapeHtml(job.stage)}` : ''}${job.editorial_notes ? `<br>Editorial notes: ${escapeHtml(job.editorial_notes)}` : ''}</p>${job.quality_report ? qualitySummary(job.quality_report) : ''}${job.error_category ? `<p>Failure: ${escapeHtml(job.error_category)} · ${escapeHtml(job.diagnostic || '')}</p>` : ''}${job.log_path ? `<p><a href="/api/files/logs/${encodeURIComponent(job.log_path)}" target="_blank" rel="noopener noreferrer">Open render log</a></p>` : ''}</div><div class="review-actions">${job.status === 'review' ? `<button class="button primary" data-review="${escapeHtml(job.id)}" data-decision="approved">Approve locally</button><button class="button" data-review="${escapeHtml(job.id)}" data-decision="changes_requested">Request changes</button>` : job.status === 'failed' && job.retry_safe ? `<button class="button" data-retry="${escapeHtml(job.id)}">Retry safely</button>` : ['queued', 'running'].includes(job.status) ? `<button class="button" data-cancel="${escapeHtml(job.id)}">Cancel job</button>` : badge(job.status)}</div></article>`).join('') : '<div class="panel empty">No queued jobs or drafts are waiting for review.</div>';
  list.querySelectorAll('[data-review]').forEach(button => button.addEventListener('click', () => decideReview(button.dataset.review, button.dataset.decision)));
  list.querySelectorAll('[data-retry]').forEach(button => button.addEventListener('click', () => retryRender(button.dataset.retry, button)));
  list.querySelectorAll('[data-cancel]').forEach(button => button.addEventListener('click', () => cancelRender(button.dataset.cancel, button)));
}

function renderHistory(events) {
  const body = $('#history-list');
  body.innerHTML = events.length ? events.map(item => `<tr><td><small>${new Date(item.created_at).toLocaleString()}</small></td><td><small>${escapeHtml(item.entity_type)} ${escapeHtml(item.entity_id.slice(0, 8))}</small></td><td>${escapeHtml(item.stage)}</td><td>${badge(item.outcome)}</td><td><small>${escapeHtml(item.details || '—')}</small></td></tr>`).join('') : '<tr><td colspan="5" class="empty">No recorded events yet.</td></tr>';
}

function renderSettings() {
  const rows = [
    ['Application', 'Local-only · 127.0.0.1'],
    ['Media engine', `FFmpeg ${state.health.ffmpeg_available ? 'available' : 'missing'} · FFprobe ${state.health.ffprobe_available ? 'available' : 'missing'}`],
    ['Publishing', 'Disabled · no upload connector configured'],
    ['AI editing', 'Unavailable · no local AI model or analysis module configured'],
    ['YouTube links', 'Reference only · no media download or scraping'],
    ['Database', state.health.data_dir ? `${state.health.database_ok ? 'Healthy' : 'Check failed'} · ${state.health.data_dir}\\factory.sqlite3` : 'Unavailable'],
    ['Source files', state.health.data_dir ? `${state.health.data_dir}\\sources` : 'Unavailable'],
    ['Rendered files', state.health.data_dir ? `${state.health.data_dir}\\outputs` : 'Unavailable'],
  ];
  $('#settings-panel').innerHTML = rows.map(([key, value]) => `<span>${escapeHtml(key)}</span><div>${escapeHtml(value)}</div>`).join('');
}

function renderSourceMeta() {
  const asset = state.assets.find(item => item.id === $('#edit-asset').value);
  if (!asset) {
    activeEditorAssetId = '';
    $('#source-meta').textContent = 'Choose an inspected source to see duration and rights status.';
    $('#preview-screen').innerHTML = '<div class="preview-placeholder"><span>9:16</span><b>SOURCE PREVIEW</b><small>Select a source to scrub and frame it</small></div>';
    $('#render-button').disabled = true;
    $('#discover-button').disabled = true;
    $('#set-in').disabled = true;
    $('#set-out').disabled = true;
    $('#preview-status').textContent = 'Awaiting a source';
    return;
  }
  if (asset.id !== activeEditorAssetId) {
    activeEditorAssetId = asset.id;
    $('#start').value = 0;
    $('#end').value = asset.duration ? Math.min($('#render-profile').value === 'short_vertical' ? 30 : 300, Math.floor(asset.duration)) : 30;
    $('#crop-x').value = 50;
    $('#crop-y').value = 50;
  }
  const permission = asset.permission_valid ? asset.permission_status : asset.permission_status === 'granted' ? 'expired' : asset.permission_status;
  const audioState = asset.has_audio == null ? 'audio not inspected' : asset.has_audio ? 'source audio present' : 'no source audio';
  $('#source-meta').textContent = `${asset.creator_name || 'Creator not linked'} · ${duration(asset.duration)} · ${asset.width || '—'}×${asset.height || '—'} · ${asset.frame_rate ? Number(asset.frame_rate).toFixed(2) + ' fps' : 'frame rate unknown'} · ${audioState} · permission ${permission} · ${asset.status}`;
  const commercial = $('#commercial-use').checked;
  const ready = Boolean(asset.duration && asset.permission_valid && (!commercial || asset.commercial_permission_valid) && state.health.ffmpeg_available && state.health.ffprobe_available);
  $('#render-button').disabled = !ready;
  $('#discover-button').disabled = !Boolean(asset.duration && asset.permission_valid && state.health.ffmpeg_available);
  $('#render-button').title = ready ? '' : 'Requires inspected media, applicable current rights, FFmpeg, and FFprobe.';
  $('#render-help').textContent = !asset.duration ? 'Inspect this source with FFprobe before rendering.' : !asset.permission_valid ? 'Record current editing permission before rendering.' : commercial && !asset.commercial_permission_valid ? 'Commercial use is selected; record separate current commercial clearance.' : !state.health.ffmpeg_available || !state.health.ffprobe_available ? 'Install or configure FFmpeg and FFprobe, then restart the app to render.' : [0, false].includes(asset.has_audio) ? 'This source has no audio track. The output will also have no audio; the QA report will disclose this.' : 'Source audio is preserved. Captions are burned into the output. Review the rendered file before approval.';
  const vertical = $('#render-profile').value === 'short_vertical';
  $('#start').max = asset.duration || '';
  const maximumCut = asset.duration ? Math.min(asset.duration, vertical ? 60 : asset.duration) : null;
  $('#end').max = maximumCut == null ? '' : maximumCut;
  if (maximumCut != null && Number($('#end').value) > maximumCut) $('#end').value = maximumCut;
  if (Number($('#start').value) >= Number($('#end').value)) $('#start').value = 0;
  $('#frame-label').textContent = vertical ? 'VERTICAL FRAMING' : 'WIDESCREEN FRAMING';
  $('#preview-profile-label').textContent = vertical ? 'SOURCE · 9:16 FRAMING PREVIEW' : 'SOURCE · 16:9 FRAMING PREVIEW';
  const placeholder = $('#preview-screen .preview-placeholder');
  if (placeholder) placeholder.style.aspectRatio = vertical ? '9/16' : '16/9';
  $('#preview-screen').innerHTML = `<video id="source-video" class="editor-video source-video" controls playsinline preload="metadata" src="/api/assets/${encodeURIComponent(asset.id)}/source"></video>`;
  $('#set-in').disabled = false;
  $('#set-out').disabled = false;
  $('#preview-status').textContent = 'Source ready · play to find your moment';
  updateCropPreview();
}

function updateCropPreview() {
  const cropX = Number($('#crop-x').value);
  const cropY = Number($('#crop-y').value);
  $('#crop-x-value').textContent = `${cropX}%`;
  $('#crop-y-value').textContent = `${cropY}%`;
  const video = $('#source-video');
  if (video) {
    video.style.objectPosition = `${cropX}% ${cropY}%`;
    video.style.transform = `scale(${{ clean_vertical: 1, punchline: 1.14, reaction: 1.06 }[$('#preset').value] || 1})`;
  }
}

async function refresh() {
  [state.health, state.assets, state.creators, state.jobs, state.youtube, state.projects, state.candidates] = await Promise.all([
    api('/api/health'), api('/api/assets'), api('/api/creators'), api('/api/jobs'), api('/api/youtube-candidates'),
    api('/api/projects'), api('/api/candidates')
  ]);
  renderAssets(); renderCreators(); renderJobs(); renderSettings(); renderSourceMeta(); renderYoutubeCandidates(); renderProjects(); renderCandidates();
  $('#engine-status').textContent = state.health.ffmpeg_available && state.health.ffprobe_available ? 'FFmpeg + FFprobe ready' : 'Media tools unavailable';
  if (!state.health.ffmpeg_available || !state.health.ffprobe_available || !state.health.database_ok) {
    $('#engine-status').style.color = 'var(--orange)';
  }
  const active = (location.hash.slice(1) || 'overview').toLowerCase();
  if (active === 'history') renderHistory(await api('/api/history'));
}

function navigate() {
  const route = (location.hash.slice(1) || 'overview').toLowerCase();
  const page = routeTitles[route] ? route : 'overview';
  document.querySelectorAll('.page').forEach(section => section.classList.toggle('active', section.id === `page-${page}`));
  document.querySelectorAll('#nav a').forEach(link => link.classList.toggle('active', link.hash === `#${page}`));
  $('#page-title').textContent = routeTitles[page];
  if (page === 'history') api('/api/history').then(renderHistory).catch(error => showNotice(error.message));
}

async function importFiles(files) {
  for (const file of files) {
    try {
      await api(`/api/import?filename=${encodeURIComponent(file.name)}`, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file,
      });
      showNotice(`${file.name} imported. Permission is still unverified.`);
    } catch (error) { showNotice(`${file.name}: ${error.message}`); }
  }
  await refresh();
}

async function openPermission(assetId) {
  const asset = state.assets.find(item => item.id === assetId);
  $('#permission-asset').value = assetId;
  $('#permission-evidence').value = asset?.permission_evidence || '';
  $('#permission-status').value = asset?.permission_status || 'unverified';
  $('#permission-expiry').value = asset?.permission_expires_at ? localDateTime(asset.permission_expires_at) : '';
  $('#commercial-status').value = asset?.commercial_permission_status || 'unverified';
  $('#commercial-evidence').value = asset?.commercial_permission_evidence || '';
  $('#commercial-expiry').value = asset?.commercial_permission_expires_at ? localDateTime(asset.commercial_permission_expires_at) : '';
  $('#source-url').value = asset?.source_url || '';
  $('#license-reference').value = asset?.license_reference || '';
  $('#attribution').value = asset?.attribution || '';
  $('#restrictions').value = asset?.restrictions || '';
  if (asset?.creator_id) $('#creator-select').value = asset.creator_id;
  if (!state.creators.length) {
    location.hash = '#creators';
    showNotice('Add the creator record first, then attach it to this asset.');
    return;
  }
  $('#permission-dialog').showModal();
}

$('#permission-form').addEventListener('submit', async event => {
  if (event.submitter?.value === 'cancel') return;
  event.preventDefault();
  const expires = $('#permission-expiry').value ? new Date($('#permission-expiry').value).toISOString() : null;
  const commercialExpires = $('#commercial-expiry').value ? new Date($('#commercial-expiry').value).toISOString() : null;
  try {
    await api(`/api/assets/${encodeURIComponent($('#permission-asset').value)}/permission`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ creator_id: $('#creator-select').value, status: $('#permission-status').value, evidence: $('#permission-evidence').value, expires_at: expires, commercial_status: $('#commercial-status').value, commercial_evidence: $('#commercial-evidence').value, commercial_expires_at: commercialExpires, source_url: $('#source-url').value, license_reference: $('#license-reference').value, attribution: $('#attribution').value, restrictions: $('#restrictions').value }),
    });
    $('#permission-dialog').close();
    await refresh();
    showNotice('Rights record saved. Confirm its scope and evidence before editing.');
  } catch (error) { showNotice(error.message); }
});

$('#creator-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api('/api/creators', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: $('#creator-name').value, source_url: $('#creator-url').value }) });
    $('#creator-form').reset();
    await refresh();
    showNotice('Creator source added. Record permission separately for each supplied asset.');
  } catch (error) { showNotice(error.message); }
});

$('#youtube-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api('/api/youtube-candidates', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: $('#youtube-url').value }) });
    $('#youtube-form').reset();
    await refresh();
    showNotice('YouTube link saved as a reference. Add an authorized local media file before editing.');
  } catch (error) { showNotice(error.message); }
});

async function decideReview(jobId, decision) {
  if (decision === 'approved' && !window.confirm('Approve this verified draft locally? This will not upload or publish it.')) return;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ decision, notes: '' }) });
    await refresh();
    showNotice(decision === 'approved' ? 'Approved for local use. No upload was made.' : 'Changes requested and recorded.');
  } catch (error) { showNotice(error.message); }
}

async function waitForRender(jobId) {
  const deadline = Date.now() + 30 * 60 * 1000;
  while (Date.now() < deadline) {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    $('#preview-status').textContent = job.status === 'queued' ? 'Waiting for the local renderer…' : job.status === 'running' ? 'Rendering on this device…' : job.status === 'review' ? 'Quality verified · ready for review' : job.status;
    if (job.status === 'review') return job;
    if (job.status === 'failed') throw new Error(`Render failed (${job.error_category || 'unknown'}): ${job.diagnostic || 'See production history.'}`);
    await new Promise(resolve => window.setTimeout(resolve, 2000));
  }
  throw new Error('Render is still running. Check Production history for its latest status.');
}

async function retryRender(jobId, button) {
  button.disabled = true;
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    await waitForRender(jobId);
    await refresh();
    showNotice('Retry completed and passed output verification.');
  } catch (error) { showNotice(error.message); await refresh(); }
}

async function cancelRender(jobId, button) {
  if (!window.confirm('Cancel this render and discard any partial output?')) return;
  button.disabled = true;
  try {
    const result = await api(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    await refresh();
    showNotice(result.status === 'cancelled' ? 'Queued render cancelled.' : 'Cancellation requested. ClipForge will remove the partial output when FFmpeg stops.');
  } catch (error) { showNotice(error.message); button.disabled = false; }
}

$('#open-import').addEventListener('click', () => { $('#import-panel').hidden = !$('#import-panel').hidden; });
$('#file-input').addEventListener('change', event => importFiles([...event.target.files]));
$('#drop-zone').addEventListener('dragover', event => { event.preventDefault(); event.currentTarget.classList.add('drag'); });
$('#drop-zone').addEventListener('dragleave', event => event.currentTarget.classList.remove('drag'));
$('#drop-zone').addEventListener('drop', event => { event.preventDefault(); event.currentTarget.classList.remove('drag'); importFiles([...event.dataTransfer.files]); });
$('#edit-asset').addEventListener('change', () => {
  activeProjectId = '';
  timelineSegments = [];
  sourceTranscriptCues = [];
  $('#transcript-file').value = '';
  $('#transcript-status').textContent = 'No source transcript loaded. Scene and silence signals remain available.';
  $('#project-title').value = 'Untitled edit';
  renderSourceMeta(); renderProjects(); renderCandidates();
});
$('#project-select').addEventListener('change', event => {
  if (event.target.value) loadEditProject(event.target.value);
  else { activeProjectId = ''; timelineSegments = []; renderTimeline(); }
});
$('#new-project').addEventListener('click', () => {
  activeProjectId = ''; timelineSegments = []; $('#project-select').value = '';
  $('#project-title').value = 'Untitled edit'; $('#editorial-notes').value = ''; $('#captions-srt').value = '';
  $('#caption-preset').value = $('#render-profile').value === 'short_vertical' ? 'shorts' : 'long_form';
  applyCaptionPreset($('#caption-preset').value);
  renderTimeline(); showNotice('New unsaved timeline started.');
});
$('#add-segment').addEventListener('click', () => {
  const start = Number($('#start').value), end = Number($('#end').value);
  const asset = state.assets.find(item => item.id === $('#edit-asset').value);
  if (!asset || !Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start || end > asset.duration) {
    return showNotice('Choose valid in/out points inside the source duration.');
  }
  if (timelineSegments.length >= 100) return showNotice('A timeline can contain at most 100 segments.');
  const total = timelineSegments.reduce((sum, item) => sum + item.end_seconds - item.start_seconds, 0) + end - start;
  if ($('#render-profile').value === 'short_vertical' && total > 60) return showNotice('The combined Shorts timeline cannot exceed 60 seconds.');
  timelineSegments.push({ start_seconds: start, end_seconds: end });
  renderTimeline();
});
$('#save-project').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try { await saveEditProject(); }
  catch (error) { showNotice(error.message); }
  finally { button.disabled = false; }
});
$('#discover-button').addEventListener('click', discoverCandidates);
$('#batch-render').addEventListener('click', renderSelectedProjects);
$('#caption-preset').addEventListener('change', event => applyCaptionPreset(event.target.value));
['#caption-font', '#caption-size', '#caption-color', '#caption-outline', '#caption-shadow', '#caption-placement',
 '#caption-margin', '#caption-line-length', '#caption-wrap'].forEach(selector => {
  $(selector).addEventListener('input', () => { $('#caption-preset').value = 'custom'; });
  $(selector).addEventListener('change', () => { $('#caption-preset').value = 'custom'; });
});
$('#transcript-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  const asset = state.assets.find(item => item.id === $('#edit-asset').value);
  if (file.size > 128 * 1024 || !asset?.duration) {
    event.target.value = '';
    return showNotice('Choose an SRT transcript up to 128 KiB for an inspected source.');
  }
  try {
    sourceTranscriptCues = parseTranscriptSrt(await file.text(), asset.duration);
    $('#transcript-status').textContent = sourceTranscriptCues.length + ' source transcript cues loaded for candidate overlap signals. Text is not semantically analyzed.';
    showNotice('Source transcript timings loaded locally.');
  } catch (error) {
    sourceTranscriptCues = [];
    $('#transcript-status').textContent = 'No source transcript loaded.';
    showNotice(error.message);
  }
});
$('#clear-transcript').addEventListener('click', () => {
  sourceTranscriptCues = [];
  $('#transcript-file').value = '';
  $('#transcript-status').textContent = 'Source transcript cleared. Timed overlap signals are unavailable.';
});
$('#render-profile').addEventListener('change', () => {
  renderSourceMeta();
  if ($('#caption-preset').value !== 'custom') {
    $('#caption-preset').value = $('#render-profile').value === 'short_vertical' ? 'shorts' : 'long_form';
    applyCaptionPreset($('#caption-preset').value);
  }
});
$('#commercial-use').addEventListener('change', () => { renderAssets(); renderSourceMeta(); });
$('#crop-x').addEventListener('input', updateCropPreview);
$('#crop-y').addEventListener('input', updateCropPreview);
$('#preset').addEventListener('change', updateCropPreview);
$('#set-in').addEventListener('click', () => {
  const video = $('#source-video');
  if (!video || !Number.isFinite(video.currentTime)) return showNotice('Select a source and seek to the desired moment first.');
  $('#start').value = video.currentTime.toFixed(1);
});
$('#set-out').addEventListener('click', () => {
  const video = $('#source-video');
  if (!video || !Number.isFinite(video.currentTime)) return showNotice('Select a source and seek to the desired moment first.');
  $('#end').value = video.currentTime.toFixed(1);
});
$('#srt-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 128 * 1024) { showNotice('SRT files must be 128 KiB or smaller.'); event.target.value = ''; return; }
  $('#captions-srt').value = await file.text();
  showNotice(`Loaded captions from ${file.name}. Check their timestamps against the selected clip.`);
});
$('#render-button').addEventListener('click', async () => {
  if (!$('#edit-asset').value) return showNotice('Select an asset first.');
  $('#render-button').disabled = true;
  $('#preview-status').textContent = 'Render queued…';
  try {
    const project = await saveEditProject();
    const queued = await api('/api/projects/' + encodeURIComponent(project.id) + '/render', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    await refresh();
    location.hash = '#review';
    showNotice(`Render queued (${queued.id.slice(0, 8)}). The local queue processes one job at a time.`);
  } catch (error) { $('#preview-status').textContent = 'Render failed or is still running'; showNotice(error.message); await refresh(); }
  finally { renderSourceMeta(); }
});

window.addEventListener('hashchange', navigate);
navigate();
refresh().catch(error => showNotice(`Could not connect to the local API: ${error.message}`));
window.setInterval(() => {
  if (state.jobs.some(job => ['queued', 'running'].includes(job.status))) refresh().catch(error => showNotice(error.message));
}, 3000);
