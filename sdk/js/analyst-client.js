/**
 * Analyst Agent — browser client.
 *
 * ES module, no build step, no dependencies. Drop it next to your page:
 *
 *     import { AnalystClient } from './analyst-client.js';
 *     const analyst = new AnalystClient();
 *     const pkg = await analyst.getPackage(pid);
 *
 * BASE URL. By default every request is RELATIVE to the directory of the current
 * page, which is what reqoach's frontend already does (`fetch('projects/...')`).
 * That way the same code works locally at `/` and deployed behind nginx at
 * `/reqoach/` with no configuration. Pass `baseUrl` only when calling the service
 * directly across origins, e.g. `new AnalystClient({baseUrl: 'http://localhost:7803/'})`.
 *
 * LONG RUNS. Analysis is slow — a 386-requirement quality run takes tens of
 * minutes, a 78-gap authoring pass ~9. Every `run*` method returns immediately
 * with `{job_id}`. Follow it with `waitForJob` (polling, always works) or
 * `streamJob` (socket.io, needs the socket.io client loaded on the page).
 */

const JOB_TERMINAL = new Set(['done', 'error', 'cancelled']);

/**
 * The API wraps lists in different envelopes per endpoint — `{projects:[]}`,
 * `{runs:[]}`, `{domains:[]}` — and sometimes returns a bare array. Unwrapping is
 * absorbed here so callers always get an array and never have to remember which
 * endpoint uses which key.
 */
function unwrap(body, key) {
  if (Array.isArray(body)) return body;
  if (body && Array.isArray(body[key])) return body[key];
  return [];
}

/** Directory of the current page, e.g. "/reqoach/" — no regex, just the last slash. */
function pageBase() {
  if (typeof location === 'undefined') return '/';
  const p = location.pathname;
  return p.slice(0, p.lastIndexOf('/') + 1) || '/';
}

export class AnalystError extends Error {
  constructor(message, { status, body, url } = {}) {
    super(message);
    this.name = 'AnalystError';
    this.status = status;
    this.body = body;
    this.url = url;
  }
}

export class AnalystClient {
  /**
   * @param {object}   [opts]
   * @param {string}   [opts.baseUrl]  override the page-relative default
   * @param {function} [opts.fetch]    injectable for tests
   * @param {function} [opts.io]       socket.io factory; defaults to window.io
   */
  constructor({ baseUrl, fetch: fetchImpl, io } = {}) {
    this.baseUrl = baseUrl || pageBase();
    if (!this.baseUrl.endsWith('/')) this.baseUrl += '/';
    this._fetch = fetchImpl || (typeof fetch !== 'undefined' ? fetch.bind(globalThis) : null);
    this._io = io || (typeof window !== 'undefined' ? window.io : undefined);
  }

  // ---- transport ---------------------------------------------------------

  url(path, params) {
    const p = String(path);
    const u = this.baseUrl + (p.startsWith('/') ? p.slice(1) : p);
    if (!params) return u;
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) q.set(k, v);
    }
    const s = q.toString();
    return s ? `${u}?${s}` : u;
  }

  async request(path, { method = 'GET', params, body, raw = false, signal } = {}) {
    const url = this.url(path, params);
    const init = { method, signal, headers: {} };
    if (body instanceof FormData) {
      init.body = body;                       // let the browser set the boundary
    } else if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    const res = await this._fetch(url, init);
    if (!res.ok) {
      let detail = '';
      try { detail = (await res.clone().json())?.detail ?? ''; } catch { /* not json */ }
      if (!detail) { try { detail = await res.text(); } catch { /* ignore */ } }
      throw new AnalystError(`${method} ${path} → ${res.status}${detail ? `: ${detail}` : ''}`,
        { status: res.status, body: detail, url });
    }
    return raw ? res : res.json();
  }

  // ---- meta --------------------------------------------------------------

  health()        { return this.request('health'); }
  version()       { return this.request('version'); }
  /** Reachability of agent_server / ingestion-server / embeddings. Note the first
   *  two report `status: 404` (no /health route) — `reachable` is the field that matters. */
  dependencies()  { return this.request('dependencies'); }

  // ---- projects ----------------------------------------------------------

  /** @returns {Promise<object[]>} always an array. */
  async listProjects()      { return unwrap(await this.request('projects'), 'projects'); }
  createProject(name)       { return this.request('projects', { method: 'POST', body: { name } }); }
  getProject(pid)           { return this.request(`projects/${pid}`); }
  deleteProject(pid)        { return this.request(`projects/${pid}`, { method: 'DELETE' }); }

  listDocuments(pid)        { return this.request(`projects/${pid}/documents`); }

  /** Upload one or more files. The field name is `files` (plural) — `file` yields a 422. */
  uploadDocuments(pid, files) {
    const fd = new FormData();
    for (const f of (files.length !== undefined ? files : [files])) fd.append('files', f);
    return this.request(`projects/${pid}/documents`, { method: 'POST', body: fd });
  }

  /** URL of the original uploaded bytes — for a PDF viewer, not a JSON fetch. */
  documentSourceUrl(pid, did) { return this.url(`projects/${pid}/documents/${did}/source`); }

  // ---- runs (each returns 202 {job_id}; follow with waitForJob/streamJob) --

  runQuality(pid, opts = {})   { return this.request(`projects/${pid}/quality:run`, { method: 'POST', body: opts }); }
  runRefine(pid, run)          { return this.request(`projects/${pid}/refine:run`, { method: 'POST', body: { run } }); }
  runClassify(pid, run)        { return this.request(`projects/${pid}/classify:run`, { method: 'POST', body: { run } }); }
  runCoverage(pid)             { return this.request(`projects/${pid}/coverage:run`, { method: 'POST' }); }
  runFraming(pid, userRequest = '') { return this.request(`projects/${pid}/framing:run`, { method: 'POST', body: { user_request: userRequest } }); }
  /** Author a requirement per open coverage gap. Needs a quality AND a coverage run. */
  runAuthor(pid, run)          { return this.request(`projects/${pid}/author:run`, { method: 'POST', body: { run } }); }
  /** The convergence loop: refine → coverage → author, repeated until the gap
   *  count hits zero (converged), stops dropping (stalled) or the round cap. */
  runConverge(pid, run)        { return this.request(`projects/${pid}/converge:run`, { method: 'POST', body: { run } }); }
  generateProblemStatement(pid, userRequest = '') { return this.request(`projects/${pid}/problem-statement:generate`, { method: 'POST', body: { user_request: userRequest } }); }

  // ---- results -----------------------------------------------------------

  /** The handover package. `format:'md'` returns text, not JSON. */
  async getPackage(pid, { run, format } = {}) {
    if (format === 'md') {
      const res = await this.request(`projects/${pid}/package`, { params: { run, format }, raw: true });
      return res.text();
    }
    return this.request(`projects/${pid}/package`, { params: { run, format } });
  }

  /** @returns {Promise<object[]>} always an array, newest last by `finished_at`. */
  async listQualityRuns(pid)    { return unwrap(await this.request(`projects/${pid}/quality`), 'runs'); }
  getScorecard(pid, run)        { return this.request(`projects/${pid}/quality/scorecard`, { params: { run } }); }
  getCoverage(pid, run)         { return this.request(`projects/${pid}/coverage`, { params: { run } }); }
  /** What a human must answer before the set can converge. No LLM call server-side. */
  getQuestions(pid, run)        { return this.request(`projects/${pid}/questions`, { params: { run } }); }
  /** Convergence loop state: round, per-round gap counts, outcome, questions. */
  getConvergence(pid)           { return this.request(`projects/${pid}/convergence`); }

  getProblemStatement(pid)      { return this.request(`projects/${pid}/problem-statement`); }
  putProblemStatement(pid, doc) { return this.request(`projects/${pid}/problem-statement`, { method: 'PUT', body: doc }); }
  getCoverageProfile(pid)       { return this.request(`projects/${pid}/coverage-profile`); }
  putCoverageProfile(pid, p)    { return this.request(`projects/${pid}/coverage-profile`, { method: 'PUT', body: p }); }

  // ---- review state ------------------------------------------------------

  getReview(pid, run)           { return this.request(`projects/${pid}/reviews/${run}`); }
  /** Patch one requirement: {status, final_text, note, overall_after}. */
  updateRequirement(pid, run, reqId, patch) {
    return this.request(`projects/${pid}/reviews/${run}/requirements/${reqId}`, { method: 'PUT', body: patch });
  }
  getThreshold(pid, run)        { return this.request(`projects/${pid}/reviews/${run}/threshold`); }
  setThreshold(pid, run, t)     { return this.request(`projects/${pid}/reviews/${run}/threshold`, { method: 'PUT', body: t }); }

  // ---- jobs --------------------------------------------------------------

  getJob(jobId)        { return this.request(`jobs/${jobId}`); }
  getJobEvents(jobId)  { return this.request(`jobs/${jobId}/events`); }
  cancelJob(jobId)     { return this.request(`jobs/${jobId}/cancel`, { method: 'POST' }); }
  /** Reattach after a page reload. */
  getActiveJob(pid)    { return this.request(`projects/${pid}/active-job`); }

  // ---- reference data ----------------------------------------------------

  /** NOT a list: a lookup map keyed by rule id — `{R1: {...}, R7: {...}}`.
   *  Left as-is because that is what a UI wants when rendering `rules_triggered`. */
  getRules()      { return this.request('rules'); }
  async getDomains()    { return unwrap(await this.request('catalog/domains'), 'domains'); }
  async getArchetypes() { return unwrap(await this.request('catalog/archetypes'), 'archetypes'); }
  async getStandards()  { return unwrap(await this.request('catalog/standards'), 'standards'); }

  // ---- helpers: following long runs --------------------------------------

  /**
   * Poll a job to completion. Works everywhere; no socket.io needed.
   * @returns the final job snapshot. Does NOT throw on `status:'error'` — inspect
   *          `.status` yourself, since a cancelled run is a normal outcome.
   */
  async waitForJob(jobId, { onProgress, intervalMs = 2000, signal } = {}) {
    for (;;) {
      const job = await this.getJob(jobId, { signal });
      if (onProgress) onProgress(job);
      if (JOB_TERMINAL.has(job.status)) return job;
      await new Promise((resolve, reject) => {
        const t = setTimeout(resolve, intervalMs);
        if (signal) signal.addEventListener('abort', () => { clearTimeout(t); reject(signal.reason); }, { once: true });
      });
    }
  }

  /** Start a run and poll it to completion in one call. */
  async runAndWait(starter, { onProgress, intervalMs, signal } = {}) {
    const { job_id: jobId } = await starter;
    return this.waitForJob(jobId, { onProgress, intervalMs, signal });
  }

  /**
   * Stream job events over socket.io. Requires the socket.io client on the page;
   * the server path is `<base>socket.io`, matching reqoach's existing pages.
   * @param {object} handlers  event name → callback. `'*'` receives every event.
   * @returns {{socket: object, close: function}}
   */
  streamJob(jobId, handlers = {}) {
    if (!this._io) throw new AnalystError('socket.io client not available — load it, pass {io}, or use waitForJob');
    const socket = this._io({ path: `${this.baseUrl}socket.io` });
    const names = ['stage', 'requirement', 'characteristic', 'deterministic', 'review_result',
      'refined', 'refine_summary', 'classified', 'classify_summary', 'authored', 'author_summary',
      'domain', 'coverage', 'scorecard', 'set_level', 'aggregates', 'round', 'round_signal',
      'converge_done', 'job_done', 'job_cancelled', 'job_error', 'cancelled', 'error'];
    socket.on('connect', () => socket.emit('join', { job_id: jobId }));
    for (const n of names) {
      socket.on(n, (data) => {
        if (handlers[n]) handlers[n](data);
        if (handlers['*']) handlers['*'](n, data);
      });
    }
    return { socket, close: () => socket.close() };
  }

  /**
   * Live single-requirement assessment — no project needed. Streams the nine
   * characteristic judges as they land, then `done`. This is what an editor pane
   * binds to on a debounce.
   */
  assess(text, handlers = {}) {
    if (!this._io) throw new AnalystError('socket.io client not available');
    const socket = this._io({ path: `${this.baseUrl}socket.io` });
    for (const n of ['start', 'deterministic', 'characteristic', 'review', 'done', 'error']) {
      socket.on(n, (data) => handlers[n] && handlers[n](data));
    }
    socket.on('connect', () => socket.emit('assess', { text }));
    return { socket, close: () => socket.close() };
  }

  // ---- helpers: the review view ------------------------------------------

  /**
   * Everything a review UI needs for one run, joined and flagged.
   *
   * The scorecard and the review session are two separate files that nobody
   * joins server-side (that join only happens inside `/package`, which is shaped
   * for the Architect, not for editing). This does the same join for a UI, and
   * adds the flags a reviewer must not miss:
   *
   *   belowThreshold   fails the absolute quality floor — blocks release
   *   generated        analyst-authored to fill a coverage gap; NO stakeholder
   *                    wrote it, and it needs ratifying
   *   textChanged      refinement rewrote it; `originalText` is kept for drift audit
   *
   * @returns {{run, threshold, requirements: object[], counts: object}}
   */
  async loadReview(pid, run) {
    if (!run) {
      const list = await this.listQualityRuns(pid);
      if (!list.length) throw new AnalystError('no quality run for this project');
      list.sort((a, b) => String(a.finished_at || '').localeCompare(String(b.finished_at || '')));
      run = list[list.length - 1].run_id;
    }
    const [scorecard, review] = await Promise.all([
      this.getScorecard(pid, run),
      this.getReview(pid, run),
    ]);
    const entries = (review && review.requirements) || {};
    const threshold = Number((review && review.threshold && review.threshold.value) ?? 4.3);

    const requirements = (scorecard.requirements || [])
      .filter((r) => !(r.lineage && r.lineage.duplicate_of))
      .map((r) => {
        const e = entries[r.req_id] || {};
        const prov = r.provenance || {};
        const score = e.overall_after ?? r.overall;
        const text = e.final_text || r.text || '';
        return {
          reqId: r.req_id,
          text,
          originalText: e.original_text || r.text || '',
          textChanged: (e.final_text || r.text || '').trim() !== (e.original_text || r.text || '').trim(),
          score,
          scoreBefore: e.overall_before ?? null,
          belowThreshold: score === null || score === undefined || score < threshold,
          judgesOk: r.judges_ok ?? null,
          judgesTotal: r.judges_total ?? null,
          incompletelyJudged: r.judges_ok != null && r.judges_total != null && r.judges_ok < r.judges_total,
          status: e.status || 'unreviewed',
          note: e.note || '',
          characteristics: r.characteristics || {},
          deterministicFindings: r.deterministic_findings || [],
          review: r.review || null,
          refinement: e.refinement || null,
          classification: e.classification || null,
          provenance: prov,
          generated: prov.origin === 'analyst_authored',
          ratified: prov.ratified === true,
          gapTitle: prov.gap_title || null,
          openQuestion: prov.open_question || '',
          raw: r,
        };
      });

    const counts = {
      total: requirements.length,
      belowThreshold: requirements.filter((r) => r.belowThreshold).length,
      generated: requirements.filter((r) => r.generated).length,
      unratifiedGenerated: requirements.filter((r) => r.generated && !r.ratified).length,
      incompletelyJudged: requirements.filter((r) => r.incompletelyJudged).length,
      needsHuman: requirements.filter((r) => r.status === 'needs_human').length,
    };
    return { run, threshold, requirements, counts };
  }

  /** Accept a requirement's current text as-is. */
  acceptRequirement(pid, run, reqId, note = '') {
    return this.updateRequirement(pid, run, reqId, { status: 'accepted', note });
  }

  /** Submit a human correction. Note: the new text is NOT re-scored automatically —
   *  run `refine:run` or `quality:run` afterwards if you need an updated score. */
  correctRequirement(pid, run, reqId, text, note = '') {
    return this.updateRequirement(pid, run, reqId, { status: 'accepted', final_text: text, note });
  }
}

export default AnalystClient;
