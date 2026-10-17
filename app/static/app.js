const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { key: '', me: null, page: 'search', mode: 'hybrid', upload: false, cursor: null, searchId: 0, epoch: 0 };
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = (epoch) => new Date(epoch * 1000).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
const badge = (value) => `<span class="badge ${esc(value)}">${esc(value)}</span>`;
let toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('#toast').hidden = true, 4500); }
async function api(path, options = {}) {
  const headers = { Authorization: `Bearer ${state.key}`, ...options.headers };
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(`/api${path}`, { ...options, headers });
  const data = await response.json();
  if (!response.ok) {
    const detail = typeof data.detail === 'string' ? data.detail : 'Please check the input fields.';
    throw new Error(detail);
  }
  return data;
}
function post(path, data) { return api(path, {method:'POST', body:JSON.stringify(data)}); }
async function busy(button, operation) {
  button.disabled = true;
  try { return await operation(); } catch (error) { toast(error.message); } finally { button.disabled = false; }
}
function safeLink(source) {
  try { const url = new URL(source); return ['https:', 'http:'].includes(url.protocol) ? esc(url.href) : null; } catch { return null; }
}
$('#connect-form').addEventListener('submit', async event => {
  event.preventDefault();
  $('#login-error').textContent = '';
  state.key = $('#api-key').value.trim();
  const button = event.submitter; button.disabled = true;
  try {
    state.me = await api('/me'); state.epoch++;
    $('#api-key').value = '';
    $('#login').hidden = true; $('#workspace').hidden = false;
    $('#tenant-name').textContent = state.me.name;
    $('#tenant-scope').textContent = `${state.me.scope === 'write' ? 'Editor' : 'Read-only'} workspace`;
    $('#backend-label').textContent = state.me.backend === 'local' ? 'Local development' : 'Distributed cluster';
    $('#embedding-label').textContent = state.me.embeddings === 'hash' ? 'Offline hash vectors · demo only' : 'Semantic embeddings enabled';
    $('#footer-backend').textContent = `${state.me.backend.toUpperCase()} / ${state.me.embeddings.toUpperCase()}`;
    $('#mode-note').textContent = state.me.embeddings === 'hash' ? 'Demo mode uses deterministic feature hashing. Connect an embedding endpoint for semantic retrieval.' : 'Vector search uses your configured embedding model. Retrieval quality depends on your data and model.';
    $$('.write-action').forEach(el => el.hidden = state.me.scope !== 'write');
    await navigate('search');
  } catch (error) { $('#login-error').textContent = error.message; state.key = ''; }
  finally { button.disabled = false; }
});
$('#disconnect').addEventListener('click', () => { state.key = ''; state.me = null; state.epoch++; location.reload(); });
const pages = { search:'Search playground', documents:'Document library', pipeline:'Indexing pipeline', settings:'Access & system' };
async function navigate(page) {
  state.page = page;
  $$('.page').forEach(el => el.hidden = el.id !== `page-${page}`);
  $$('[data-page]').forEach(el => el.classList.toggle('active', el.dataset.page === page));
  $('#page-breadcrumb').textContent = pages[page];
  try { await refresh(); } catch (error) { toast(error.message); }
}
$$('[data-page]').forEach(button => button.addEventListener('click', () => navigate(button.dataset.page)));
$('#refresh').addEventListener('click', event => busy(event.currentTarget, refresh));
async function refresh() {
  if (state.page === 'search') await loadStats();
  if (state.page === 'documents') await loadDocuments();
  if (state.page === 'pipeline') await loadJobs();
  if (state.page === 'settings') await loadSettings();
}
async function loadStats() {
  const data = await api('/stats');
  const items = [ ['Documents', data.documents, 'In your workspace', '▤'], ['Indexed chunks', data.chunks, 'Active revision passages', '◫'], ['Pending jobs', (data.jobs.queued || 0) + (data.jobs.running || 0), `${data.queue_lag_seconds}s queue lag`, '⇄'], ['Completed jobs', data.jobs.done || 0, `${data.jobs.dead || 0} dead-letter jobs`, '✓'] ];
  $('#stats').innerHTML = items.map(([label,value,note,icon]) => `<div class="stat"><span>${esc(label)}<i>${icon}</i></span><b>${Number(value).toLocaleString()}</b><small>${esc(note)}</small></div>`).join('');
}
$$('[data-mode]').forEach(button => button.addEventListener('click', () => {
  state.mode = button.dataset.mode;
  $$('[data-mode]').forEach(el => el.classList.toggle('selected', el === button));
}));
$$('[data-query]').forEach(button => button.addEventListener('click', () => { $('#query').value = button.dataset.query; $('#search-form').requestSubmit(); }));
$('#search-form').addEventListener('submit', async event => {
  event.preventDefault();
  const query = $('#query').value.trim(); if (!query) return;
  const id = ++state.searchId;
  $('#search-submit').disabled = true;
  $('#results').innerHTML = '<div class="loading" role="status">Searching your workspace…</div>';
  $('#answer-panel').hidden = true;
  try {
    const data = await post($('#answer-toggle').checked ? '/answer' : '/search', { query, mode:state.mode, collection:$('#collection-filter').value.trim() || null, limit:8 });
    if (id !== state.searchId) return;
    $('#search-status').innerHTML = `<span>${data.results.length} RESULTS · ${esc(data.mode.toUpperCase())}</span><span>${data.latency_ms}ms retrieval · ${data.cache_hit ? 'CACHE HIT' : 'FRESH QUERY'}</span>`;
    if (data.answer) {
      $('#answer-panel').hidden = false;
      const label = {extractive:'Source excerpts · no LLM configured', generated:'Generated answer', no_evidence:'No supporting evidence', unverified:'Review required'}[data.answer_mode];
      $('#answer-panel').innerHTML = `<div class="answer-card"><h3>✳ ${esc(label)}</h3><p>${esc(data.answer)}</p><small>Evidence sources: ${(data.citations || []).map(c => `[${c.number}] ${esc(c.title)}`).join(' · ')}</small></div>`;
    }
    $('#results').innerHTML = data.results.length ? data.results.map((row, i) => {
      const link = safeLink(row.source);
      return `<article class="result-card"><div class="result-top"><span>${String(i+1).padStart(2,'0')} / ${esc(row.collection.toUpperCase())}</span><span>RRF ${row.score.toFixed(5)}</span></div><h3>${esc(row.title)}</h3><p>${esc(row.content.slice(0,600))}${row.content.length > 600 ? '…' : ''}</p><div class="result-bottom"><span>REVISION ${row.revision}</span><span>CHUNK ${row.ordinal + 1}</span>${Object.entries(row.ranks).map(([k,v]) => `<span>${esc(k.toUpperCase())} #${v}</span>`).join('')}${link ? `<a href="${link}" target="_blank" rel="noopener noreferrer">Open source ↗</a>` : ''}</div><details><summary>Inspect full passage</summary><p>${esc(row.content)}</p><small>Document ${esc(row.document_id)}</small></details></article>`;
    }).join('') : '<div class="empty-state"><h2>No matching documents.</h2><p>Try a broader query, remove the collection filter, or check that indexing has completed.</p></div>';
  } catch (error) { $('#results').innerHTML = `<div class="empty-state"><h2>Search unavailable</h2><p>${esc(error.message)}</p></div>`; $('#search-status').textContent = 'Request failed · try again'; }
  finally { $('#search-submit').disabled = false; }
});
async function loadDocuments(append = false) {
  const params = append && state.cursor ? '?' + new URLSearchParams(state.cursor) : '';
  const data = await api('/documents' + params); state.cursor = data.next;
  const html = data.documents.map(row => `<tr><td><b>${esc(row.title)}</b><small>${esc(row.external_id)}</small></td><td>${esc(row.collection)}</td><td>${badge(row.status)}</td><td>v${row.revision}<small>active v${row.active_revision}</small></td><td>${row.chunk_count}</td><td>${state.me.scope === 'write' ? `<button class="row-action" data-reindex="${row.id}">Reindex</button><button class="row-action danger" data-delete="${row.id}">Delete</button>` : 'Read only'}</td></tr>`).join('');
  if (append) $('#documents-body').insertAdjacentHTML('beforeend', html);
  else $('#documents-body').innerHTML = html || '<tr><td colspan="6">No documents yet. Add a document to start building your corpus.</td></tr>';
  $('#load-more').hidden = !data.next;
}
$('#load-more').addEventListener('click', event => busy(event.currentTarget, () => loadDocuments(true)));
$('#documents-body').addEventListener('click', event => {
  const button = event.target.closest('button'); if (!button) return;
  busy(button, async () => {
    if (button.dataset.delete) {
      if (!confirm('Delete this document from search? You can ingest it again later.')) return;
      await api(`/documents/${button.dataset.delete}`, {method:'DELETE'}); toast('Document removed from search. Cleanup queued.');
    } else { await post(`/documents/${button.dataset.reindex}/reindex`, {}); toast('Reindexing queued.'); }
    await loadDocuments();
  });
});
async function loadJobs() {
  const data = await api('/jobs?' + new URLSearchParams({state:$('#job-filter').value}));
  $('#jobs-body').innerHTML = data.jobs.map(row => `<tr><td><b>${esc(row.id.slice(0,8))}</b><small>${esc(row.document_id.slice(0,8))} · revision ${row.revision}</small>${row.error ? `<small class="error">${esc(row.error)}</small>` : ''}</td><td>${esc(row.operation)}</td><td>${badge(row.state)}</td><td>${row.attempts}</td><td>${esc(date(row.created_at))}</td><td>${row.state === 'dead' && state.me.scope === 'write' ? `<button class="row-action" data-retry="${row.id}">Retry job</button>` : '—'}</td></tr>`).join('') || '<tr><td colspan="6">No indexing jobs in this view.</td></tr>';
}
$('#job-filter').addEventListener('change', () => loadJobs().catch(error => toast(error.message)));
$('#jobs-body').addEventListener('click', event => {
  const button = event.target.closest('[data-retry]'); if (!button) return;
  busy(button, async () => { await post(`/jobs/${button.dataset.retry}/retry`, {}); toast('Job queued for another attempt.'); await loadJobs(); });
});
async function loadSettings() {
  const me = state.me;
  let cluster = {status:'Read-only access'};
  if (me.scope === 'write') cluster = await api('/cluster');
  $('#runtime-info').innerHTML = [['Workspace',me.name], ['Tenant ID',me.tenant_id], ['Search backend',me.backend], ['Cluster status',cluster.status], ['Embedding provider',me.embeddings === 'hash' ? 'Hash vectors (offline demo)' : 'OpenAI-compatible embeddings'], ['Answer provider',me.llm_enabled ? 'LLM configured' : 'Extractive source excerpts']].map(([k,v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
  const audit = await api('/audit');
  $('#audit-list').innerHTML = audit.events.map(row => `<div class="list-row"><div>${esc(row.action)}<small>${esc(row.resource_id)}</small></div><span class="muted">${esc(date(row.created_at))}</span></div>`).join('') || '<p class="muted">No audit events yet.</p>';
  if (me.scope === 'write') await loadKeys();
}
async function loadKeys() {
  const data = await api('/keys');
  $('#keys-list').innerHTML = data.keys.map(row => `<div class="list-row"><div>${esc(row.label)}${row.current ? ' · current key' : ''}<small>${esc(row.scope)} access · ${row.revoked ? 'revoked' : 'active'}</small></div>${!row.current && !row.revoked ? `<button class="row-action danger" data-revoke="${row.id}">Revoke</button>` : ''}</div>`).join('');
}
$('#key-form').addEventListener('submit', event => {
  event.preventDefault(); busy(event.submitter, async () => {
    const data = await post('/keys', {label:$('#key-label').value, scope:$('#key-scope').value});
    $('#new-key').hidden = false; $('#new-key').textContent = `Copy now — shown once: ${data.api_key}`;
    $('#key-label').value = ''; await loadKeys();
  });
});
$('#keys-list').addEventListener('click', event => {
  const button = event.target.closest('[data-revoke]'); if (!button || !confirm('Revoke this API key immediately?')) return;
  busy(button, async () => { await api(`/keys/${button.dataset.revoke}`, {method:'DELETE'}); toast('API key revoked.'); await loadKeys(); });
});
function showIngest() { $('#ingest-error').textContent = ''; $('#ingest-dialog').showModal(); }
$('#open-ingest').addEventListener('click', showIngest); $('#library-ingest').addEventListener('click', showIngest);
$('#close-dialog').addEventListener('click', () => $('#ingest-dialog').close());
function setUpload(value) {
  state.upload = value; $('#paste-fields').hidden = value; $('#file-fields').hidden = !value;
  $('#paste-tab').classList.toggle('selected', !value); $('#file-tab').classList.toggle('selected', value);
}
$('#paste-tab').addEventListener('click', () => setUpload(false)); $('#file-tab').addEventListener('click', () => setUpload(true));
$('#ingest-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = event.submitter; button.disabled = true; $('#ingest-error').textContent = '';
  try {
    let data;
    if (state.upload) {
      const file = $('#doc-file').files[0]; if (!file) throw new Error('Choose a file first.');
      if (file.size > 10_000_000) throw new Error('File exceeds 10 MB.');
      const form = new FormData(); form.append('file', file);
      data = await api('/documents/upload?' + new URLSearchParams({collection:$('#doc-collection').value}), {method:'POST', body:form});
    } else {
      if (!$('#doc-title').value.trim() || !$('#doc-id').value.trim() || !$('#doc-content').value.trim()) throw new Error('Title, external ID, and content are required.');
      data = await post('/documents', { title:$('#doc-title').value, external_id:$('#doc-id').value, content:$('#doc-content').value, collection:$('#doc-collection').value });
    }
    $('#ingest-dialog').close(); $('#ingest-form').reset();
    toast(data.unchanged ? 'Document is unchanged. No indexing needed.' : 'Document queued. Your worker will index it shortly.');
    await refresh();
  } catch(error) { $('#ingest-error').textContent = error.message; }
  finally { button.disabled = false; }
});
let refreshing = false;
setInterval(async () => {
  if (!state.key || document.hidden || refreshing || !['pipeline','search'].includes(state.page)) return;
  refreshing = true;
  try { await refresh(); } catch { /* Explicit refresh reports errors; keep existing data on polling failure. */ }
  finally { refreshing = false; }
}, 8000);
