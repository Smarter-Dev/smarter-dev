/**
 * The user dashboard (/dashboard) as a single-page app.
 *
 * Every view paints from `cache`: the page embeds the first state, the API
 * fills in what a route needs, and the worker's `web_search` notifications
 * (Skrift's SSE stream) carry each search's whole snapshot, so any one event
 * is enough to render it. Snapshots carry a version and an older one never
 * replaces a newer one. The stream is ephemeral and closes while the tab is
 * in the background, so reconnecting re-reads the open search from the API.
 */
(function () {
  'use strict';

  const shell = document.querySelector('[data-dashboard]');
  const stateEl = document.getElementById('dashboard-state');
  if (!shell || !stateEl) return;

  const initial = JSON.parse(stateEl.textContent);
  // A search run from a search link without logging in: one search, never
  // saved, read from its own API; no rail and no recent searches.
  const anonymous = !!initial.anonymous;
  const searchApi = initial.search_api || '/dashboard/api/searches/';
  const stage = shell.querySelector('[data-stage]');
  const csrfInput = document.querySelector('[data-dashboard-csrf] input[name="_csrf"]');
  const csrfToken = (csrfInput && csrfInput.value) || '';
  const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const EVENT_TYPE = initial.event_type || 'web_search';
  const STEPS = ['planning', 'searching', 'ranking', 'answering'];
  const LEAVE_MS = 180;
  const POLL_MS = 4000;

  const cache = {
    searches: new Map(),
    recent: initial.recent || [],
    link: initial.link || null,
  };
  let view = null;        // {name, el, searchId, queriesOpen}
  let elapsedTimer = null;
  let pollTimer = null;
  let streamStatus = 'connecting';

  if (initial.search) cache.searches.set(initial.search.id, initial.search);

  // ── Helpers ───────────────────────────────────────────

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function plural(n, one, many) {
    return n + ' ' + (n === 1 ? one : (many || one + 's'));
  }

  function api(url, options) {
    options = options || {};
    options.headers = Object.assign({ 'Accept': 'application/json' }, options.headers || {});
    options.credentials = 'same-origin';
    if (options.body) options.headers['Content-Type'] = 'application/json';
    if (options.method && options.method !== 'GET') options.headers['X-CSRF-Token'] = csrfToken;
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        reloadIfDeployed(body.build);
        if (!response.ok) {
          const error = new Error(body.detail || 'Something went wrong. Try again.');
          error.status = response.status;
          throw error;
        }
        return body;
      });
    });
  }

  /** A tab left open across a deploy runs the old script, which can't render
      what the new server sends. Reload once the caller has updated the URL.
      During a rolling deploy old and new pods answer side by side, so each
      build triggers at most one reload per tab. */
  let reloading = false;
  function reloadIfDeployed(build) {
    if (reloading || !build || !initial.build || build === initial.build) return;
    try {
      if (sessionStorage.getItem('dashboard-reloaded-for') === build) return;
      sessionStorage.setItem('dashboard-reloaded-for', build);
    } catch (error) {
      return;
    }
    reloading = true;
    window.setTimeout(function () { location.reload(); }, 0);
  }

  function newKey() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }

  function relativeTime(iso) {
    if (!iso) return '';
    const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return Math.floor(seconds / 60) + ' min ago';
    if (seconds < 86400) return Math.floor(seconds / 3600) + ' h ago';
    return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  }

  function seconds(from, to) {
    if (!from) return '';
    const end = to ? Date.parse(to) : Date.now();
    return ((end - Date.parse(from)) / 1000).toFixed(1) + ' s';
  }

  /** Keep the newer of two snapshots of the same search. */
  function remember(search) {
    const known = cache.searches.get(search.id);
    if (known && known.version > search.version) return known;
    cache.searches.set(search.id, search);
    const row = {
      id: search.id,
      request: search.request,
      status: search.status,
      active: search.active,
      relevant: (search.results || []).filter(function (r) { return r.relevant; }).length,
      created_at: search.created_at,
    };
    const index = cache.recent.findIndex(function (r) { return r.id === search.id; });
    if (index === -1) cache.recent.unshift(row);
    else cache.recent[index] = row;
    return search;
  }

  // ── Views ─────────────────────────────────────────────

  /** Swap the stage to a new view: the old one fades up and out while the
      new one rises in, both in the same grid cell so nothing jumps. The
      first view rises in the same way. */
  function mount(name, searchId) {
    const template = document.getElementById('ud-' + name);
    const next = template.content.firstElementChild.cloneNode(true);
    const previous = view && view.el;
    // queriesOpen: null follows the search (open while it runs), else the reader's choice.
    view = { name: name, el: next, searchId: searchId || null, queriesOpen: null };

    if (previous) {
      previous.classList.add('is-leaving');
      previous.setAttribute('aria-hidden', 'true');
      previous.inert = true;
      window.setTimeout(function () { previous.remove(); }, reduceMotion.matches ? 0 : LEAVE_MS);
    }
    next.classList.add('is-entering');
    next.addEventListener('animationend', function done(event) {
      if (event.target !== next) return;
      next.classList.remove('is-entering');
      next.removeEventListener('animationend', done);
    });
    stage.appendChild(next);
    stage.scrollTo({ top: 0 });
    return next;
  }

  function showHome(options) {
    stopTimers();
    const node = mount('home');
    document.title = 'Dashboard · Smarter Dev';
    const name = (initial.user && initial.user.name) || 'there';
    node.querySelector('[data-greeting]').textContent = 'Hi ' + name.split(' ')[0];
    wireComposer(node);
    renderRecent();
    if (!options || options.focus !== false) {
      const input = node.querySelector('[data-request]');
      window.requestAnimationFrame(function () { input.focus({ preventScroll: true }); });
    }
  }

  function showSearch(id) {
    stopTimers();
    const node = mount('search', id);
    node.querySelector('[data-more-toggle]').addEventListener('click', toggleMore);
    node.querySelector('[data-queries-toggle]').addEventListener('click', function (event) {
      view.queriesOpen = event.currentTarget.getAttribute('aria-expanded') !== 'true';
      const search = cache.searches.get(view.searchId);
      if (search) renderQueries(view.el, search);
    });
    if (anonymous) {
      node.querySelector('[data-back]').hidden = true;
      node.querySelector('[data-unsaved]').hidden = false;
      node.querySelector('[data-login]').href = initial.login_url;
    }
    const search = cache.searches.get(id);
    if (search) renderSearch(search);
    else node.classList.add('is-loading');
    renderRecent();
    api(searchApi + id).then(function (body) {
      node.classList.remove('is-loading');
      renderSearch(remember(body.search));
      renderRecent();
    }).catch(function (error) {
      if (search) return;
      node.classList.remove('is-loading');
      showError(node, error.status === 404 ? 'This search no longer exists.' : error.message);
    });
  }

  function showError(node, message) {
    const box = node.querySelector('[data-error]');
    box.textContent = message;
    box.hidden = false;
  }

  // ── Browser search ────────────────────────────────────

  function showBrowser() {
    stopTimers();
    const node = mount('browser');
    document.title = 'Browser search · Smarter Dev';
    const status = node.querySelector('[data-link-status]');

    function save(options, done) {
      node.querySelector('[data-error]').hidden = true;
      const request = options === null
        ? api('/dashboard/api/link', { method: 'DELETE' })
        : api('/dashboard/api/link', { method: 'POST', body: JSON.stringify(options) });
      return request.then(function (body) {
        cache.link = body.link;
        renderBrowser(node);
        if (done) status.textContent = done;
      }).catch(function (error) {
        renderBrowser(node);
        showError(node, error.message);
      });
    }

    node.querySelector('[data-link-create]').addEventListener('click', function () { save({}); });
    node.querySelector('[data-link-copy]').addEventListener('click', function () {
      const field = node.querySelector('[data-link-url]');
      const copied = function () { status.textContent = 'Copied your search link.'; };
      if (navigator.clipboard) {
        navigator.clipboard.writeText(field.value).then(copied, function () { field.select(); });
      } else {
        field.select();
      }
    });
    node.querySelector('[data-link-open]').addEventListener('change', function (event) {
      const on = event.currentTarget.checked;
      save({ open_addresses: on }, on ? 'Web addresses now open directly.' : 'Everything you type is searched now.');
    });
    node.querySelector('[data-link-rotate]').addEventListener('click', function () {
      if (!window.confirm('Make a new search link? The one in your browser stops working, so you will need to add the new one.')) return;
      save({ rotate: true }, 'Made a new link. Replace the old one in your browser.');
    });
    node.querySelector('[data-link-delete]').addEventListener('click', function () {
      if (!window.confirm('Delete your search link? Searches from your browser stop working until you make a new one.')) return;
      save(null);
    });
    renderBrowser(node);
    renderRecent();
  }

  function renderBrowser(node) {
    const link = cache.link;
    node.querySelector('[data-link-empty]').hidden = !!link;
    node.querySelector('[data-link-panel]').hidden = !link;
    if (link) {
      node.querySelector('[data-link-url]').value = link.url;
      node.querySelector('[data-link-open]').checked = !!link.open_addresses;
    }
    // Firefox offers to add a search engine the page links to.
    let tag = document.head.querySelector('link[rel="search"]');
    if (!link) {
      if (tag) tag.remove();
      return;
    }
    if (!tag) {
      tag = document.createElement('link');
      tag.rel = 'search';
      tag.type = 'application/opensearchdescription+xml';
      tag.title = 'Smarter Dev';
      document.head.appendChild(tag);
    }
    if (tag.getAttribute('href') !== link.opensearch) tag.setAttribute('href', link.opensearch);
  }

  // ── Routing ───────────────────────────────────────────

  function route(path) {
    const match = path.match(/^\/dashboard\/search\/([0-9a-f-]{36})\/?$/);
    if (match) showSearch(match[1]);
    else if (/^\/dashboard\/browser\/?$/.test(path)) showBrowser();
    else showHome();
  }

  function go(path) {
    if (path === location.pathname) return;
    history.pushState({ dashboard: true, url: path }, '', path);
    route(path);
  }

  // Capture on window runs before spa-nav's own popstate listener, which
  // would otherwise reload the page for every back and forward.
  window.addEventListener('popstate', function (event) {
    if (!location.pathname.startsWith('/dashboard')) return;
    event.stopImmediatePropagation();
    route(location.pathname);
  }, true);

  shell.addEventListener('click', function (event) {
    const link = event.target.closest('a[data-dashboard-link]');
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    // Stop spa-nav from fetching the page again.
    event.stopPropagation();
    go(new URL(link.href).pathname);
  });

  // ── Composer ──────────────────────────────────────────

  function wireComposer(node) {
    const form = node.querySelector('[data-search-form]');
    const input = form.querySelector('[data-request]');
    const submit = form.querySelector('[data-submit]');
    const errorBox = form.querySelector('[data-form-error]');
    const max = initial.max_request_chars || 500;
    input.maxLength = max;

    function resize() {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 240) + 'px';
    }
    input.addEventListener('input', function () {
      resize();
      errorBox.hidden = true;
    });
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        form.requestSubmit();
      }
    });
    node.querySelector('[data-examples]').addEventListener('click', function (event) {
      const example = event.target.closest('.ud-example');
      if (!example) return;
      input.value = example.textContent;
      resize();
      input.focus();
    });

    let key = null;
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      // One key per distinct request, so a double submit reuses the search.
      key = key && form.dataset.lastRequest === text ? key : newKey();
      form.dataset.lastRequest = text;
      submit.disabled = true;
      submit.textContent = 'Starting…';
      api('/dashboard/api/searches', {
        method: 'POST',
        body: JSON.stringify({ request: text, submission_key: key }),
      }).then(function (body) {
        const search = remember(body.search);
        go('/dashboard/search/' + search.id);
      }).catch(function (error) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
        submit.disabled = false;
        submit.textContent = 'Search';
      });
    });
  }

  // ── Recent searches ───────────────────────────────────

  function renderRecent() {
    const current = view && view.searchId;
    const rows = cache.recent;
    shell.querySelectorAll('[data-recent]').forEach(function (list) {
      const existing = new Map();
      list.querySelectorAll('li[data-id]').forEach(function (li) { existing.set(li.dataset.id, li); });
      const seen = new Set();
      rows.forEach(function (row, index) {
        seen.add(row.id);
        let li = existing.get(row.id);
        if (!li) {
          li = el('li', 'ud-recent-row');
          li.dataset.id = row.id;
          const link = el('a', 'ud-recent-link');
          link.href = '/dashboard/search/' + row.id;
          link.setAttribute('data-dashboard-link', '');
          link.append(el('span', 'ud-recent-text'), el('span', 'ud-recent-meta'));
          li.appendChild(link);
          if (list.childElementCount) li.classList.add('is-new');
        }
        const link = li.firstElementChild;
        link.querySelector('.ud-recent-text').textContent = row.request;
        link.querySelector('.ud-recent-meta').textContent = recentMeta(row);
        link.classList.toggle('is-active', !!row.active);
        link.classList.toggle('is-failed', row.status === 'error');
        if (row.id === current) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
        if (list.children[index] !== li) list.insertBefore(li, list.children[index] || null);
      });
      existing.forEach(function (li, id) { if (!seen.has(id)) li.remove(); });
    });
    const empty = shell.querySelector('[data-recent-empty]');
    if (empty) empty.hidden = rows.length > 0;
    const homeRecent = stage.querySelector('[data-home-recent]');
    if (homeRecent) homeRecent.hidden = rows.length === 0;
    const tool = current ? null : (view && view.name === 'browser' ? 'browser' : 'search');
    shell.querySelectorAll('.ud-rail-tool').forEach(function (link) {
      if (link.dataset.tool === tool) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
  }

  function recentMeta(row) {
    const when = relativeTime(row.created_at);
    if (row.status === 'answering') return 'Writing an answer… · ' + when;
    if (row.active) return 'Searching… · ' + when;
    if (row.status === 'error') return 'Failed · ' + when;
    return plural(row.relevant, 'relevant result') + ' · ' + when;
  }

  // ── A search ──────────────────────────────────────────

  function stepState(search, step) {
    const order = STEPS.indexOf(step);
    const status = search.status;
    if (step === 'answering' && search.answer && search.answer.status === 'failed') return 'failed';
    if (status === 'complete') return 'done';
    if (status === 'queued') return order === 0 ? 'waiting' : 'pending';
    if (status === 'error') {
      const reached = search.results.length ? 2 : search.queries.length ? 1 : 0;
      if (order < reached) return 'done';
      return order === reached ? 'failed' : 'pending';
    }
    const current = STEPS.indexOf(status);
    if (order < current) return 'done';
    return order === current ? 'active' : 'pending';
  }

  function stepDetail(search, step, state) {
    const queries = search.queries;
    const results = search.results;
    const relevant = results.filter(function (r) { return r.relevant; }).length;
    if (step === 'planning') {
      if (state === 'waiting') return 'Waiting for a worker…';
      if (state === 'active') return 'Luna is writing five searches…';
      if (state === 'failed') return 'Luna couldn’t write the searches.';
      if (state === 'done') return 'Luna wrote ' + plural(queries.length, 'search', 'searches');
      return 'Luna writes five searches';
    }
    if (step === 'searching') {
      const finished = queries.filter(function (q) { return q.status === 'done' || q.status === 'failed'; }).length;
      const found = queries.reduce(function (sum, q) { return sum + (q.count || 0); }, 0);
      if (state === 'active') return 'Searching ' + Math.min(finished + 1, queries.length) + ' of ' + queries.length + ' · ' + plural(found, 'result') + ' so far';
      if (state === 'failed') return 'None of the searches found anything.';
      if (state === 'done') return plural(results.length, 'unique result') + ' from ' + plural(queries.length, 'search', 'searches');
      return 'Brave runs each one';
    }
    if (step === 'answering') {
      const reads = (search.answer && search.answer.reads) || [];
      const read = reads.filter(function (r) { return r.status === 'done'; }).length;
      if (state === 'failed') return 'Luna couldn’t write an answer.';
      if (state === 'done') return 'Luna read ' + plural(read, 'page') + ' and wrote an answer';
      if (state === 'active') {
        if (search.answer && search.answer.status === 'writing') return 'Luna is writing the answer…';
        return reads.length ? 'Luna is reading ' + plural(reads.length, 'page') + '…' : 'Luna is choosing pages to read…';
      }
      return 'Luna reads the best pages and answers';
    }
    if (state === 'active') return 'Jev is ranking ' + plural(results.length, 'result') + '…';
    if (state === 'done') {
      return search.ranked
        ? 'Jev ranked ' + plural(results.length, 'result') + ' · ' + relevant + ' relevant'
        : 'Ranking was unavailable, so these are in search order';
    }
    return 'Jev ranks every result';
  }

  function statusText(search) {
    if (search.status === 'complete') return search.ranked ? 'Ranked results' : 'Results';
    if (search.status === 'error') return 'Search failed';
    if (search.status === 'queued') return 'Queued';
    if (search.status === 'planning') return 'Planning searches';
    if (search.status === 'searching') return 'Searching the web';
    if (search.status === 'answering') return 'Writing an answer';
    return 'Ranking results';
  }

  function renderSearch(search) {
    if (!view || view.name !== 'search' || view.searchId !== search.id) return;
    const node = view.el;
    node.dataset.status = search.status;
    document.title = search.request + ' · Smarter Dev';
    node.querySelector('[data-request-text]').textContent = search.request;
    node.querySelector('[data-status-text]').textContent = statusText(search);

    STEPS.forEach(function (step) {
      const item = node.querySelector('[data-step="' + step + '"]');
      if (step === 'answering') item.hidden = !search.needs_answer;
      const state = stepState(search, step);
      item.dataset.state = state;
      const detail = item.querySelector('[data-step-detail]');
      const text = stepDetail(search, step, state);
      if (detail.textContent !== text) detail.textContent = text;
    });

    renderQueries(node, search);
    renderAnswer(node, search);
    renderResults(node, search);

    const errorBox = node.querySelector('[data-error]');
    errorBox.hidden = search.status !== 'error';
    if (search.status === 'error') errorBox.textContent = search.error || 'The search failed. Try again.';

    tickElapsed(search);
    if (search.active) startTimers(search);
    else stopTimers();
  }

  function renderQueries(node, search) {
    const list = node.querySelector('[data-queries]');
    search.queries.forEach(function (query, index) {
      let row = list.children[index];
      if (!row) {
        row = el('li', 'ud-query');
        row.style.setProperty('--i', index);
        const mark = el('span', 'ud-query-mark');
        mark.setAttribute('aria-hidden', 'true');
        const body = el('div', 'ud-query-body');
        body.append(el('code', 'ud-query-text'), el('span', 'ud-query-angle'), el('span', 'ud-query-domains'));
        row.append(mark, body, el('span', 'ud-query-count'));
        list.appendChild(row);
      }
      row.dataset.state = query.status;
      row.querySelector('.ud-query-text').textContent = query.query;
      row.querySelector('.ud-query-angle').textContent = query.angle;
      const count = row.querySelector('.ud-query-count');
      const countText = query.status === 'done' ? String(query.count) : query.status === 'failed' ? '!' : '';
      if (count.textContent !== countText) count.textContent = countText;
      count.title = query.status === 'failed' ? 'This search failed' : plural(query.count || 0, 'result');
      const domains = row.querySelector('.ud-query-domains');
      if (domains.childElementCount !== query.domains.length) {
        domains.replaceChildren.apply(domains, query.domains.map(function (domain, i) {
          const chip = el('span', 'ud-domain', domain);
          chip.style.setProperty('--i', i);
          return chip;
        }));
      }
    });
    while (list.children.length > search.queries.length) list.lastElementChild.remove();

    // Once the results are ranked, fold the queries away so the answer and
    // the results come up.
    const searching = search.active && search.status !== 'answering';
    const open = view.queriesOpen == null ? searching : view.queriesOpen;
    node.querySelector('[data-queries-wrap]').classList.toggle('is-closed', !open);
    const toggle = node.querySelector('[data-queries-toggle]');
    toggle.hidden = searching || !search.queries.length;
    toggle.setAttribute('aria-expanded', String(open));
    toggle.textContent = open ? 'Hide searches' : 'Show the ' + plural(search.queries.length, 'search', 'searches');
  }

  // ── Answer ────────────────────────────────────────────

  /** The answer panel: the pages Luna reads, then the answer as it streams.
      The HTML is rendered and sanitized by the worker. */
  function renderAnswer(node, search) {
    const panel = node.querySelector('[data-answer]');
    const answer = search.answer;
    panel.hidden = !search.needs_answer || !answer;
    if (panel.hidden) return;
    panel.dataset.state = answer.status;

    const list = panel.querySelector('[data-reads]');
    const rows = new Map();
    list.querySelectorAll('li').forEach(function (li) { rows.set(li.dataset.number, li); });
    (answer.reads || []).forEach(function (read) {
      let li = rows.get(String(read.number));
      if (!li) {
        li = el('li', 'ud-read', read.domain);
        li.dataset.number = String(read.number);
        list.appendChild(li);
      }
      li.dataset.state = read.status;
      li.title = read.status === 'failed' ? 'Couldn’t read this page' : read.status === 'done' ? 'Read' : 'Reading…';
    });

    const body = panel.querySelector('[data-answer-body]');
    const html = answer.html || '';
    if (body.dataset.html !== html) {
      body.innerHTML = html;
      body.dataset.html = html;
    }
    panel.querySelector('[data-answer-skeleton]').hidden = !!html || answer.status === 'failed' || answer.status === 'done';
    const note = panel.querySelector('[data-answer-note]');
    note.hidden = answer.status !== 'failed';
    if (!note.hidden) note.textContent = answer.error || 'Couldn’t write an answer this time.';
  }

  // ── Results ───────────────────────────────────────────

  function buildResult(result) {
    const li = el('li', 'ud-result');
    li.dataset.url = result.url;
    const link = el('a', 'ud-result-link');
    link.href = result.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    const source = el('span', 'ud-result-source');
    if (result.favicon) {
      const icon = el('img', 'ud-favicon');
      icon.src = result.favicon;
      icon.alt = '';
      icon.loading = 'lazy';
      icon.referrerPolicy = 'no-referrer';
      icon.addEventListener('error', function () { icon.remove(); });
      source.appendChild(icon);
    }
    source.appendChild(el('span', 'ud-result-domain', result.domain));
    if (result.age) source.appendChild(el('span', 'ud-result-age', result.age));
    link.append(source, el('span', 'ud-result-title', result.title || result.domain));
    const meta = el('div', 'ud-result-meta');
    li.append(link, el('p', 'ud-result-snippet', result.snippet), meta);
    return li;
  }

  function fillMeta(li, result, ranked) {
    const meta = li.querySelector('.ud-result-meta');
    const parts = [];
    if (ranked) {
      if (result.best) parts.push(el('span', 'ud-badge is-best', 'Top pick'));
      if (result.quality) parts.push(el('span', 'ud-badge is-trusted', 'Trusted source'));
      const meter = el('span', 'ud-meter');
      meter.style.setProperty('--score', Math.max(0, Math.min(3, result.score)) / 3);
      meter.title = 'Relevance ' + result.score.toFixed(2) + ' of 3';
      meter.setAttribute('role', 'img');
      meter.setAttribute('aria-label', meter.title);
      parts.push(meter);
    }
    const found = (result.queries || []).map(function (i) { return i + 1; });
    if (found.length) parts.push(el('span', 'ud-found', (found.length > 1 ? 'Searches ' : 'Search ') + found.join(', ')));
    meta.replaceChildren.apply(meta, parts);
    li.classList.toggle('is-best', !!(ranked && result.best));
  }

  /** Place every result in its list and order, then play each one from
      where it was (FLIP), so a ranking reads as the list sorting itself. */
  function renderResults(node, search) {
    const section = node.querySelector('[data-results]');
    const results = search.results || [];
    section.hidden = results.length === 0;
    if (!results.length) return;

    const main = node.querySelector('[data-result-list]');
    const quiet = node.querySelector('[data-more-list]');
    // Ranked results stand once Jev is done, even while the answer is written.
    const done = search.status === 'complete' || search.status === 'answering';
    const ranked = done && search.ranked;
    const pending = !done;
    section.classList.toggle('is-pending', pending);

    const existing = new Map();
    section.querySelectorAll('.ud-result').forEach(function (li) { existing.set(li.dataset.url, li); });
    const before = new Map();
    const animate = !reduceMotion.matches && existing.size > 0;
    if (animate) existing.forEach(function (li, url) { before.set(url, li.getBoundingClientRect()); });

    // The top pick always leads the main list, even if Jev scored it low.
    const primary = ranked ? results.filter(function (r) { return r.relevant || r.best; }) : results;
    const secondary = ranked ? results.filter(function (r) { return !r.relevant && !r.best; }) : [];
    // If Jev found nothing relevant, show everything rather than an empty list.
    const shown = primary.length ? primary : results;
    const hidden = primary.length ? secondary : [];

    function place(list, items) {
      items.forEach(function (result, index) {
        let li = existing.get(result.url);
        if (!li) {
          li = buildResult(result);
          li.classList.add('is-new');
          li.style.setProperty('--i', Math.min(index, 12));
        }
        fillMeta(li, result, ranked);
        if (list.children[index] !== li) list.insertBefore(li, list.children[index] || null);
      });
    }
    place(main, shown);
    place(quiet, hidden);
    const keep = new Set(results.map(function (r) { return r.url; }));
    existing.forEach(function (li, url) { if (!keep.has(url)) li.remove(); });

    const more = node.querySelector('[data-more]');
    more.hidden = hidden.length === 0;
    const toggle = node.querySelector('[data-more-toggle]');
    const open = toggle.getAttribute('aria-expanded') === 'true';
    toggle.textContent = (open ? 'Hide ' : 'Show ') + plural(hidden.length, 'less relevant result');

    if (animate) {
      section.querySelectorAll('.ud-result').forEach(function (li) {
        const was = before.get(li.dataset.url);
        if (!was) return;
        const now = li.getBoundingClientRect();
        const dx = was.left - now.left;
        const dy = was.top - now.top;
        if (!dx && !dy) return;
        li.animate(
          [{ transform: 'translate(' + dx + 'px,' + dy + 'px)' }, { transform: 'none' }],
          { duration: 520, easing: 'cubic-bezier(.2,.7,.2,1)' }
        );
      });
    }
  }

  function toggleMore(event) {
    const toggle = event.currentTarget;
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(open));
    view.el.querySelector('[data-more-wrap]').classList.toggle('is-open', open);
    const search = cache.searches.get(view.searchId);
    if (search) renderResults(view.el, search);
  }

  // ── Time and resync ───────────────────────────────────

  function tickElapsed(search) {
    if (!view || view.name !== 'search') return;
    const out = view.el.querySelector('[data-elapsed]');
    out.textContent = seconds(search.started_at || search.created_at, search.active ? null : search.finished_at);
  }

  function startTimers(search) {
    if (!elapsedTimer) {
      elapsedTimer = window.setInterval(function () {
        const current = view && cache.searches.get(view.searchId);
        if (current) tickElapsed(current);
      }, 100);
    }
    if (!pollTimer) {
      // Only while the stream is down: then nothing else would move the view.
      pollTimer = window.setInterval(function () {
        if (streamStatus === 'connected') return;
        refresh(search.id);
      }, POLL_MS);
    }
  }

  function stopTimers() {
    window.clearInterval(elapsedTimer);
    window.clearInterval(pollTimer);
    elapsedTimer = pollTimer = null;
  }

  function refresh(id) {
    return api(searchApi + id).then(function (body) {
      renderSearch(remember(body.search));
      renderRecent();
    }).catch(function () {});
  }

  document.addEventListener('sk:notification', function (event) {
    const data = event.detail || {};
    const payload = data.payload || {};
    if (data.type !== EVENT_TYPE || !payload.search) return;
    event.preventDefault();
    const search = remember(payload.search);
    renderSearch(search);
    renderRecent();
  });

  document.addEventListener('sk:notification-status', function (event) {
    const status = event.detail && event.detail.status;
    const wasDown = streamStatus !== 'connected';
    streamStatus = status;
    if (status !== 'connected' || !wasDown) return;
    // Events sent while the stream was closed are gone: re-read the open search.
    const current = view && view.searchId && cache.searches.get(view.searchId);
    if (current && current.active) refresh(current.id);
    if (anonymous) return;
    api('/dashboard/api/searches').then(function (body) {
      cache.recent = body.searches;
      renderRecent();
    }).catch(function () {});
  });

  // Recent-list times ("2 min ago") drift; refresh them once a minute.
  window.setInterval(renderRecent, 60000);

  // ── Start ─────────────────────────────────────────────

  history.replaceState({ dashboard: true, url: location.pathname }, '', location.pathname);
  if (initial.view === 'browser') {
    showBrowser();
  } else if (initial.view === 'search' && initial.search) {
    view = null;
    showSearch(initial.search.id);
  } else {
    showHome();
  }
  const skNotifications = window.__skriftNotifications;
  if (skNotifications && skNotifications.status) streamStatus = skNotifications.status;
})();
