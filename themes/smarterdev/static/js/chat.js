(function () {
  'use strict';

  var shell = document.querySelector('[data-chat-shell]');
  if (!shell) return;

  var mode = shell.dataset.mode;
  var conversationId = shell.dataset.conversationId || null;
  var form = document.querySelector('[data-chat-composer]');
  var input = document.querySelector('[data-chat-input]');
  var thread = document.querySelector('[data-chat-thread]');
  var statusEl = document.querySelector('[data-chat-status]');
  var errorEl = document.querySelector('[data-chat-error]');
  var stopBtn = document.querySelector('[data-chat-stop]');
  var submitBtn = form && form.querySelector('button[type=submit]');
  var hintEl = document.querySelector('[data-chat-hint]');
  var toBottomBtn = document.querySelector('[data-chat-to-bottom]');
  var IDLE_HINT = 'shift+enter · newline';
  var csrfInput = document.querySelector('[data-chat-csrf] input[name="_csrf"]');
  var csrfToken = csrfInput && csrfInput.value || '';
  var pendingAttachments = [];
  var uploadCount = 0;
  var activeTurn = shell.dataset.activeTurnId || null;
  var resourceRunning = shell.dataset.resourceRunning === 'true';
  var conversationPromise = null;
  var catalog = null;
  var pendingChange = null;
  var agentPanel = document.querySelector('[data-chat-agent-panel]');
  var subagentList = document.querySelector('[data-chat-subagents]');
  var notificationStreamStatus = null;
  var pendingDocuments = [];
  var dock = document.querySelector('[data-chat-dock]');
  var dockToggle = document.querySelector('[data-dock-toggle]');
  var dockClose = document.querySelector('[data-dock-close]');
  var dockBack = document.querySelector('[data-dock-back]');
  var dockTitle = document.querySelector('[data-dock-title]');
  var dockList = document.querySelector('[data-dock-list]');
  var dockEmpty = document.querySelector('[data-dock-empty]');
  var dockCount = document.querySelector('[data-dock-count]');
  var dockPreview = document.querySelector('[data-dock-preview]');
  var previewFilename = document.querySelector('[data-preview-filename]');
  var previewContent = document.querySelector('[data-preview-content]');
  var previewDownload = document.querySelector('[data-preview-download]');
  var previewBody = dockPreview && dockPreview.querySelector('.chat-dock-preview-body');
  var railToggle = document.querySelector('[data-rail-toggle]');
  var resizeHandle = document.querySelector('[data-dock-resize]');
  var quoteBox = document.querySelector('[data-chat-quote]');
  var quoteText = document.querySelector('[data-quote-text]');
  var quoteSource = document.querySelector('[data-quote-source]');
  var quoteInput = document.querySelector('[data-quote-input]');
  var quotedPassage = '';
  var dockDocuments = [];
  var dockPreviewing = null;
  var dockReturnFocus = null;
  // Set once the reader moves the rail themselves; from then on the dock stops
  // folding it for them.
  var railUserSet = false;
  var railAutoCollapsed = false;
  var settingsDisclosure = document.querySelector('[data-chat-settings]');
  var mediaInstructionRow = document.querySelector('[data-media-instruction-row]');
  var imageUploads = 0;

  // ── Layout state ──────────────────────────────────────
  // Rail and dock positions persist, so the workshop view a reader sets up is
  // still there next visit. Storage is best-effort: private-mode browsers throw
  // on both read and write, and a lost preference is not worth an error.
  function store(key, value) {
    try { window.localStorage.setItem(key, value); } catch (error) { /* no-op */ }
  }

  function stored(key) {
    try { return window.localStorage.getItem(key); } catch (error) { return null; }
  }

  function setRail(open, focusTarget, automatic) {
    if (!shell) return;
    shell.dataset.rail = open ? 'open' : 'collapsed';
    if (railToggle) railToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    var sidebar = document.querySelector('.chat-sidebar');
    if (sidebar) {
      // A zero-width column keeps its contents tabbable without this.
      if (open) sidebar.removeAttribute('inert');
      else sidebar.setAttribute('inert', '');
    }
    if (!automatic) {
      railUserSet = true;
      railAutoCollapsed = false;
      store('chat.rail', open ? 'open' : 'collapsed');
    }
    if (focusTarget) focusTarget.focus();
  }

  if (railToggle) railToggle.addEventListener('click', function () {
    setRail(shell.dataset.rail === 'collapsed');
  });

  // ── Disclosures ───────────────────────────────────────
  // Every quiet chip on the page is a <details>, so it works without JS and
  // exposes its state to assistive tech natively. JS only adds the extras a
  // popover needs: an explicit aria-expanded, Escape, and click-away.
  function closeDisclosure(disclosure, restoreFocus) {
    if (!disclosure || !disclosure.open) return;
    disclosure.open = false;
    if (restoreFocus) {
      var summary = disclosure.querySelector('summary');
      if (summary) summary.focus();
    }
  }

  Array.prototype.forEach.call(document.querySelectorAll('.p-disclosure'), function (disclosure) {
    var summary = disclosure.querySelector('summary');
    if (!summary) return;
    summary.setAttribute('aria-expanded', disclosure.open ? 'true' : 'false');
    disclosure.addEventListener('toggle', function () {
      summary.setAttribute('aria-expanded', disclosure.open ? 'true' : 'false');
    });
  });

  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    var open = document.querySelector('.p-pop-host[open]');
    if (open) closeDisclosure(open, true);
  });

  document.addEventListener('click', function (event) {
    Array.prototype.forEach.call(document.querySelectorAll('.p-pop-host[open]'), function (disclosure) {
      if (!disclosure.contains(event.target)) closeDisclosure(disclosure, false);
    });
  });

  function syncModelLabel(modelKey) {
    var label = document.querySelector('[data-chat-model-label]');
    if (!label || !modelKey) return;
    var model = catalog && catalog.models.find(function (item) { return item.key === modelKey; });
    label.textContent = model ? model.label : modelKey;
    // The vendor is the second half of the chip ("Grok 4.5 · xAI"); drop it
    // rather than show a dangling separator when the model is off-catalog.
    var vendor = document.querySelector('[data-chat-model-vendor]');
    if (vendor) vendor.textContent = model && model.vendor ? '· ' + model.vendor : '';
    var rail = document.querySelector('[data-rail-model]');
    if (rail) rail.textContent = modelKey;
  }

  // ── Connection ────────────────────────────────────────
  // One line at the foot of the rail. It only matters when it is wrong, so the
  // only thing that changes is the mark's colour and one word.
  var CONNECTION = {
    connected: ['connected', 'is-ok'],
    connecting: ['connecting', 'is-warn'],
    reconnecting: ['reconnecting', 'is-warn'],
    suspended: ['suspended', 'is-warn'],
    disconnected: ['offline', 'is-bad'],
  };

  function syncConnection(status) {
    var mark = document.querySelector('[data-connection-mark]');
    var label = document.querySelector('[data-connection-label]');
    var state = CONNECTION[status];
    if (!mark || !label || !state) return;
    label.textContent = state[0];
    mark.className = 'p-mark ' + state[1];
  }

  // ── Quote-to-ask ──────────────────────────────────────
  // Selecting a passage in an open document raises a small composer beside it.
  // Sending quotes the passage above the question, so the agent answers about
  // that paragraph rather than the document in general.

  var QUOTE_MAX = 1200;   // leaves room under the 5000-char message cap

  function collapseWhitespace(value) {
    return String(value || '').replace(/\s+/g, ' ').trim();
  }

  function hideQuote() {
    if (!quoteBox || quoteBox.hidden) return;
    quoteBox.hidden = true;
    quoteText.textContent = '';
    quoteInput.value = '';
    quotedPassage = '';
  }

  // Anchors under the selection, then pulls back inside the viewport so a
  // passage near an edge does not push the box off-screen.
  function placeQuote(rect) {
    var margin = 8;
    var box = quoteBox.getBoundingClientRect();
    var left = rect.left + (rect.width / 2) - (box.width / 2);
    left = Math.min(window.innerWidth - box.width - margin, Math.max(margin, left));
    var top = rect.bottom + margin;
    if (top + box.height > window.innerHeight - margin) {
      top = Math.max(margin, rect.top - box.height - margin);
    }
    quoteBox.style.left = Math.round(left) + 'px';
    quoteBox.style.top = Math.round(top) + 'px';
  }

  function showQuote() {
    if (!quoteBox || !previewContent) return;
    var selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount) return hideQuote();
    var range = selection.getRangeAt(0);
    // Only passages inside the open document — not the thread, not the rail.
    if (!previewContent.contains(range.commonAncestorContainer)) return;
    var passage = collapseWhitespace(selection.toString());
    if (passage.length < 2) return hideQuote();
    quotedPassage = passage.length > QUOTE_MAX
      ? passage.slice(0, QUOTE_MAX).replace(/\s+\S*$/, '') + '…'
      : passage;
    quoteText.textContent = quotedPassage;
    quoteSource.textContent = dockTitle ? 'From ' + dockTitle.textContent : '';
    quoteBox.hidden = false;
    // Measure after it is laid out, otherwise the box has no height to place by.
    placeQuote(range.getBoundingClientRect());
  }

  if (quoteBox && previewContent) {
    // mouseup rather than selectionchange: the latter fires on every character
    // of a drag, and the box would chase the pointer across the document.
    previewContent.addEventListener('mouseup', function () {
      window.setTimeout(showQuote, 0);
    });
    // Touch selection does not raise a reliable mouseup, and the handles keep
    // moving after the first touchend, so give the selection a beat to settle.
    previewContent.addEventListener('touchend', function () {
      window.setTimeout(showQuote, 120);
    });
    previewContent.addEventListener('keyup', function (event) {
      if (event.shiftKey || event.key === 'Shift') window.setTimeout(showQuote, 0);
    });

    // Losing the selection is the signal to leave — but not when the click that
    // cleared it landed inside the box itself.
    document.addEventListener('mousedown', function (event) {
      if (quoteBox.contains(event.target)) return;
      hideQuote();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !quoteBox.hidden) hideQuote();
    });
    // The anchor is a viewport position, so it goes stale the moment anything
    // moves underneath it.
    if (previewBody) previewBody.addEventListener('scroll', hideQuote);
    window.addEventListener('resize', hideQuote);

    quoteInput.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        quoteBox.requestSubmit
          ? quoteBox.requestSubmit()
          : quoteBox.dispatchEvent(new Event('submit', {cancelable: true}));
      }
    });

    quoteBox.addEventListener('submit', function (event) {
      event.preventDefault();
      var question = (quoteInput.value || '').trim();
      if (!question) return quoteInput.focus();
      var source = dockTitle ? dockTitle.textContent : 'the document';
      // Markdown blockquote so it renders as a quotation in the thread, with
      // the source named underneath for a reader coming back to it later.
      var message = quotedPassage.split('\n').map(function (line) {
        return '> ' + line;
      }).join('\n') + '\n>\n> — ' + source + '\n\n' + question;
      // Build the optimistic bubble as a real blockquote. Rendering the raw
      // markdown as text would leave "> " markers on screen for the whole turn,
      // until the server's rendered HTML arrives at reconcile.
      var passage = quotedPassage;
      function build(content) {
        var quote = document.createElement('blockquote');
        var body = document.createElement('p');
        body.textContent = passage;
        var attribution = document.createElement('p');
        attribution.textContent = '— ' + source;
        quote.append(body, attribution);
        var ask = document.createElement('p');
        ask.textContent = question;
        content.append(quote, ask);
      }
      if (sendMessage(message, build)) {
        hideQuote();
        window.getSelection().removeAllRanges();
      }
    });
  }

  // ── Starters ──────────────────────────────────────────
  // A starter is a draft, not a submission: it fills the composer and hands
  // the reader the caret so they can edit before sending.
  document.addEventListener('click', function (event) {
    var starter = event.target.closest('[data-chat-starter]');
    if (!starter || !input) return;
    input.value = starter.textContent.trim() + ' ';
    autoGrow();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });

  // The instruction only steers image summarisation, so it stays out of the
  // composer until an image is actually in play.
  function syncMediaInstruction() {
    if (!mediaInstructionRow) return;
    var hasImage = imageUploads > 0 || pendingAttachments.some(function (item) {
      return String(item.media_type || '').indexOf('image/') === 0;
    });
    mediaInstructionRow.hidden = !hasImage;
  }

  function api(url, options) {
    options = options || {};
    options.credentials = 'same-origin';
    options.headers = Object.assign({'Accept': 'application/json'}, options.headers || {});
    if (options.method && options.method !== 'GET' && csrfToken) {
      options.headers['X-CSRF-Token'] = csrfToken;
    }
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var error = new Error(body.detail || body.message || 'Request failed');
          error.status = response.status;
          throw error;
        }
        return body;
      });
    });
  }

  function json(method, body) {
    return {
      method: method,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    };
  }

  function showError(message) {
    if (!errorEl) return;
    errorEl.textContent = message || '';
    errorEl.hidden = !message;
  }

  function setStatus(text) {
    if (mode === 'chat') {
      updateRootActivity({turn_id: activeTurn, status: text || ''});
      return;
    }
    if (!statusEl) return;
    statusEl.textContent = text || '';
    statusEl.hidden = !text;
  }

  function setBusy(busy) {
    var locked = Boolean(busy);
    var blocked = Boolean(locked || uploadCount);
    if (submitBtn) submitBtn.disabled = blocked;
    // Say why Send is off rather than leaving a greyed-out button unexplained.
    if (form) form.dataset.busy = blocked ? 'true' : 'false';
    if (hintEl) {
      hintEl.textContent = uploadCount
        ? 'Uploading attachments — Send unlocks when they finish'
        : (locked ? 'Working — Send unlocks when this turn finishes' : IDLE_HINT);
    }
    if (stopBtn) stopBtn.hidden = !activeTurn;
    document.querySelectorAll('[data-regenerate]').forEach(function (button) {
      button.disabled = locked;
    });
    document.querySelectorAll('[data-chat-model], [data-chat-reasoning]').forEach(function (control) {
      control.disabled = Boolean(locked || !conversationId);
    });
    document.querySelectorAll('[data-chat-new-intelligence], [data-chat-new-model], [data-chat-new-reasoning]').forEach(function (control) {
      // Intelligence is immutable and attachment-first creation already fixes
      // all three settings. Never leave editable controls that no longer apply.
      control.disabled = Boolean(locked || conversationId);
    });
  }

  // The reader's turn is a raised plane, so it takes .p-card from the product
  // system. Kept in one place because the template builds the same string.
  function messageClasses(role) {
    return 'chat-message chat-message-' + role + (role === 'user' ? ' p-card' : '');
  }

  // `build` lets a caller render the optimistic body itself. Without it the
  // draft goes in as plain text, which is right for a typed message but would
  // show a quoted passage as raw "> " markers for the whole turn.
  function bubble(role, text, turnId, build) {
    var article = document.createElement('article');
    article.className = messageClasses(role);
    if (turnId) article.dataset.turnId = turnId;
    var content = document.createElement('div');
    content.className = 'chat-content p-prose';
    if (build) build(content);
    else content.textContent = text;
    article.appendChild(createByline(role));
    if (role === 'assistant' && mode === 'chat') {
      article.appendChild(createActivity(new Date().toISOString(), 'Working…'));
    }
    article.appendChild(content);
    return article;
  }

  function documentDownloadUrl(documentId) {
    return '/v2/api/chat/conversations/' + conversationId + '/documents/' + encodeURIComponent(documentId) + '/download';
  }

  function formatDocumentSize(sizeBytes) {
    var size = Number(sizeBytes) || 0;
    if (size < 1024) return size + ' bytes';
    return (size / 1024).toFixed(size < 10240 ? 1 : 0) + ' KB';
  }

  function renderDocumentCard(item) {
    if (!item || !item.id || document.querySelector('[data-document-id="' + item.id + '"]')) return true;
    var article = item.assistant_message_id && thread.querySelector('[data-message-id="' + item.assistant_message_id + '"]');
    article = article || rootArticle(item.turn_id);
    if (!article) return false;
    var documents = article.querySelector('[data-chat-documents]');
    if (!documents) {
      documents = document.createElement('div');
      documents.className = 'chat-documents';
      documents.dataset.chatDocuments = '';
      var responseActions = article.querySelector('.chat-response-actions');
      article.insertBefore(documents, responseActions || null);
    }
    var card = document.createElement('article');
    card.className = 'chat-document';
    card.dataset.documentId = item.id;
    var mark = document.createElement('div');
    mark.className = 'chat-document-mark';
    mark.setAttribute('aria-hidden', 'true');
    mark.textContent = 'MD';
    var info = document.createElement('div');
    info.className = 'chat-document-info';
    var title = document.createElement('strong');
    title.textContent = item.title || 'Markdown document';
    var filename = document.createElement('span');
    filename.textContent = (item.filename || 'document.md') + ' · ' + formatDocumentSize(item.size_bytes);
    info.append(title, filename);
    var actions = document.createElement('div');
    actions.className = 'chat-document-actions';
    var preview = document.createElement('button');
    preview.type = 'button';
    preview.dataset.openDocument = '';
    preview.dataset.documentId = item.id;
    preview.textContent = 'Preview';
    var download = document.createElement('a');
    download.href = documentDownloadUrl(item.id);
    download.download = item.filename || 'document.md';
    download.dataset.skNoSpa = '';
    download.textContent = 'Download';
    actions.append(preview, download);
    card.append(mark, info, actions);
    documents.appendChild(card);
    return true;
  }

  function syncDocuments(items) {
    // The panel is the roll-up for the whole conversation, so it takes every
    // document immediately. The inline card needs its message to exist first
    // and may have to wait — that queue must not hold up the panel.
    syncDockList(items);
    (items || []).forEach(function (item) {
      if (renderDocumentCard(item)) return;
      if (!pendingDocuments.some(function (pending) { return pending.id === item.id; })) {
        pendingDocuments.push(item);
      }
    });
  }

  function flushPendingDocuments() {
    var queued = pendingDocuments;
    pendingDocuments = [];
    syncDocuments(queued);
  }

  // ── Document dock ─────────────────────────────────────
  // The panel is a column of the shell, not an overlay: opening it narrows the
  // conversation instead of covering it, so a document can be read alongside
  // the turn discussing it. Two views share the column — the roll-up list, and
  // one document open for reading.

  function dockItem(item) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'chat-dock-item';
    button.dataset.dockOpen = '';
    button.dataset.documentId = item.id;
    var mark = document.createElement('span');
    mark.className = 'chat-dock-item-mark';
    mark.setAttribute('aria-hidden', 'true');
    mark.textContent = 'MD';
    var info = document.createElement('span');
    info.className = 'chat-dock-item-info';
    var title = document.createElement('strong');
    title.textContent = item.title || 'Markdown document';
    var meta = document.createElement('span');
    meta.className = 'p-meta';
    meta.textContent = (item.filename || 'document.md') + ' \u00b7 ' + formatDocumentSize(item.size_bytes);
    info.append(title, meta);
    button.append(mark, info);
    return button;
  }

  // Appends only what is new, so the list never flickers and the row a reader
  // is pointing at cannot move out from under them mid-turn.
  function syncDockList(items) {
    if (!dockList) return;
    (items || []).forEach(function (item) {
      if (!item || !item.id) return;
      if (dockList.querySelector('[data-dock-open][data-document-id="' + item.id + '"]')) return;
      dockList.insertBefore(dockItem(item), dockEmpty);
      dockDocuments.push(item);
    });
    var count = dockList.querySelectorAll('[data-dock-open]').length;
    if (dockCount) dockCount.textContent = count;
    if (dockEmpty) dockEmpty.hidden = count > 0;
    // The button is the only route to the panel, so it appears with the first
    // document rather than sitting there empty for the whole conversation.
    if (dockToggle) dockToggle.hidden = count === 0;
  }

  // Mirrors the 1100px breakpoint in pages/chat.css, where the dock stops being
  // a grid column and starts floating over the conversation.
  function dockIsColumn() {
    return window.matchMedia('(min-width: 1101px)').matches;
  }

  // ── Dock width ────────────────────────────────────────
  // Width is per conversation: a reference table wants a wide panel, a short
  // note does not, and that choice belongs to the document being worked on
  // rather than to the browser. Kept as a most-recent-last list capped at 20,
  // so the store cannot grow without bound across a long chat history.
  var DOCK_WIDTHS_KEY = 'chat.dockWidths';
  var DOCK_WIDTH_CAP = 20;
  var DOCK_MIN = 288;

  function dockMax() {
    // Always leave the conversation the wider half of whatever is left.
    return Math.max(DOCK_MIN, Math.round(shell.clientWidth * 0.6));
  }

  function readDockWidths() {
    try {
      var parsed = JSON.parse(stored(DOCK_WIDTHS_KEY) || '[]');
      return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
      return [];
    }
  }

  function rememberDockWidth(width) {
    if (!conversationId) return;
    var entries = readDockWidths().filter(function (entry) {
      return entry && entry.id !== conversationId;
    });
    entries.push({id: conversationId, width: Math.round(width)});
    store(DOCK_WIDTHS_KEY, JSON.stringify(entries.slice(-DOCK_WIDTH_CAP)));
  }

  function savedDockWidth() {
    if (!conversationId) return null;
    var match = readDockWidths().filter(function (entry) {
      return entry && entry.id === conversationId;
    })[0];
    return match ? match.width : null;
  }

  function syncResizeRange(now) {
    if (!resizeHandle) return;
    resizeHandle.setAttribute('aria-valuemin', String(DOCK_MIN));
    resizeHandle.setAttribute('aria-valuemax', String(dockMax()));
    if (now) resizeHandle.setAttribute('aria-valuenow', String(Math.round(now)));
  }

  function applyDockWidth(width, persist) {
    var clamped = Math.min(dockMax(), Math.max(DOCK_MIN, Math.round(width)));
    shell.style.setProperty('--chat-dock', clamped + 'px');
    syncResizeRange(clamped);
    if (persist) rememberDockWidth(clamped);
    return clamped;
  }

  function currentDockWidth() {
    return dock ? dock.getBoundingClientRect().width : DOCK_MIN;
  }

  if (resizeHandle) {
    resizeHandle.addEventListener('pointerdown', function (event) {
      if (!dockIsColumn() || shell.dataset.dock !== 'open') return;
      event.preventDefault();
      shell.dataset.resizing = 'true';
      resizeHandle.setPointerCapture(event.pointerId);
      var right = shell.getBoundingClientRect().right;

      function move(moveEvent) {
        applyDockWidth(right - moveEvent.clientX, false);
      }
      function done() {
        shell.dataset.resizing = 'false';
        resizeHandle.removeEventListener('pointermove', move);
        resizeHandle.removeEventListener('pointerup', done);
        resizeHandle.removeEventListener('pointercancel', done);
        // Persist once, on release — not on every frame of the drag.
        rememberDockWidth(currentDockWidth());
      }
      resizeHandle.addEventListener('pointermove', move);
      resizeHandle.addEventListener('pointerup', done);
      resizeHandle.addEventListener('pointercancel', done);
    });

    // A separator that can only be dragged is unusable without a mouse.
    resizeHandle.addEventListener('keydown', function (event) {
      if (!dockIsColumn() || shell.dataset.dock !== 'open') return;
      var step = event.shiftKey ? 64 : 16;
      var width = currentDockWidth();
      var next = null;
      if (event.key === 'ArrowLeft') next = width + step;
      else if (event.key === 'ArrowRight') next = width - step;
      else if (event.key === 'Home') next = dockMax();
      else if (event.key === 'End') next = DOCK_MIN;
      if (next === null) return;
      event.preventDefault();
      applyDockWidth(next, true);
    });

    // Double-click resets to the stylesheet's default.
    resizeHandle.addEventListener('dblclick', function () {
      shell.style.removeProperty('--chat-dock');
      rememberDockWidth(currentDockWidth());
    });
  }

  function showDockList() {
    if (!dock) return;
    hideQuote();
    dockPreviewing = null;
    if (dockList) dockList.hidden = false;
    if (dockPreview) dockPreview.hidden = true;
    if (dockBack) dockBack.hidden = true;
    if (dockTitle) dockTitle.textContent = 'Documents';
    Array.prototype.forEach.call(dockList.querySelectorAll('[data-dock-open]'), function (row) {
      row.removeAttribute('aria-current');
    });
  }

  function setDock(open, focusTarget) {
    if (!dock || !shell) return;
    hideQuote();
    shell.dataset.dock = open ? 'open' : 'closed';
    if (dockToggle) dockToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    // A zero-width column is still in the DOM, so its contents stay tabbable
    // without this.
    if (open) dock.removeAttribute('inert');
    else dock.setAttribute('inert', '');
    store('chat.dock', open ? 'open' : 'closed');
    // Opening the workshop view folds the rail to keep the conversation wide —
    // but never overrules a reader who has set the rail themselves this session,
    // and only where the dock is actually a column. Below that the dock floats
    // over the conversation, so folding the rail would reclaim nothing and just
    // take the history strip away.
    if (open && dockIsColumn() && !railUserSet && shell.dataset.rail !== 'collapsed') {
      railAutoCollapsed = true;
      setRail(false, null, true);
    } else if (!open && railAutoCollapsed) {
      railAutoCollapsed = false;
      setRail(true, null, true);
    }
    if (focusTarget && open) focusTarget.focus();
  }

  function openDocument(documentId, trigger) {
    if (!dock || !documentId) return;
    setDock(true);
    hideQuote();
    dockPreviewing = documentId;
    if (dockList) dockList.hidden = true;
    if (dockPreview) dockPreview.hidden = false;
    if (dockBack) dockBack.hidden = false;
    dockReturnFocus = trigger || null;
    if (dockTitle) dockTitle.textContent = 'Loading\u2026';
    if (previewFilename) previewFilename.textContent = '';
    if (previewContent) previewContent.textContent = 'Loading document\u2026';
    if (previewDownload) previewDownload.removeAttribute('href');
    api('/v2/api/chat/conversations/' + conversationId + '/documents/' + encodeURIComponent(documentId)).then(function (data) {
      // A second click while this one was in flight wins; drop the stale reply.
      if (dockPreviewing !== documentId) return;
      if (dockTitle) dockTitle.textContent = data.title;
      if (previewFilename) previewFilename.textContent = data.filename + ' \u00b7 ' + formatDocumentSize(data.size_bytes);
      if (previewContent) previewContent.innerHTML = data.content_html;
      if (previewDownload) {
        previewDownload.href = documentDownloadUrl(documentId);
        previewDownload.download = data.filename;
      }
      if (previewBody) previewBody.scrollTop = 0;
    }).catch(function (error) {
      if (dockPreviewing !== documentId) return;
      if (dockTitle) dockTitle.textContent = 'Document unavailable';
      if (previewContent) previewContent.textContent = error.message;
    });
  }

  if (dockToggle) dockToggle.addEventListener('click', function () {
    var open = shell.dataset.dock !== 'open';
    setDock(open);
    if (open && !dockPreviewing) showDockList();
  });

  if (dockClose) dockClose.addEventListener('click', function () {
    setDock(false);
    if (dockToggle) dockToggle.focus();
  });

  if (dockBack) dockBack.addEventListener('click', function () {
    showDockList();
    if (dockReturnFocus && dockReturnFocus.isConnected) dockReturnFocus.focus();
    dockReturnFocus = null;
  });

  if (dock) dock.addEventListener('click', function (event) {
    var row = event.target.closest('[data-dock-open]');
    if (row) openDocument(row.dataset.documentId, row);
  });

  // Escape closes the panel only while it holds focus, so it cannot steal the
  // key from the composer or an open disclosure.
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape' || !dock) return;
    if (shell.dataset.dock !== 'open' || !dock.contains(document.activeElement)) return;
    setDock(false);
    if (dockToggle) dockToggle.focus();
  });

  // Terminal turns reconcile in place instead of reloading, so every mutation
  // has to leave the reader where they were. The thread is the scroll container
  // but the window can scroll too on short viewports; restore both.
  function atBottom() {
    if (!thread) return true;
    return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 48;
  }

  function scrollToBottom(smooth) {
    if (!thread) return;
    if (smooth && thread.scrollTo) thread.scrollTo({top: thread.scrollHeight, behavior: 'smooth'});
    else thread.scrollTop = thread.scrollHeight;
    syncToBottomButton();
  }

  // Streaming output only follows the reader when the reader is already at the
  // bottom; scrolled-up readers keep their place and get the jump button.
  function stick(wasAtBottom) {
    if (wasAtBottom) scrollToBottom(false);
    else syncToBottomButton();
  }

  function syncToBottomButton() {
    if (!toBottomBtn) return;
    toBottomBtn.hidden = atBottom();
  }

  if (thread) thread.addEventListener('scroll', syncToBottomButton, {passive: true});
  if (toBottomBtn) toBottomBtn.addEventListener('click', function () { scrollToBottom(true); });

  function captureScroll() {
    if (!thread) return null;
    return {
      top: thread.scrollTop,
      atBottom: atBottom(),
      windowTop: window.scrollY,
    };
  }

  function restoreScroll(state) {
    if (!state || !thread) return;
    thread.scrollTop = state.atBottom ? thread.scrollHeight : state.top;
    window.scrollTo(window.scrollX, state.windowTop);
    syncToBottomButton();
  }

  // The composer grows with the draft up to the max height set in chat.css,
  // after which it scrolls instead of pushing the thread off screen.
  function autoGrow() {
    if (!input) return;
    input.style.height = 'auto';
    input.style.height = input.scrollHeight + 'px';
  }

  if (input) {
    input.addEventListener('input', autoGrow);
    input.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter' || event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
      if (event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      if (!form || (submitBtn && submitBtn.disabled)) return;
      if (form.requestSubmit) form.requestSubmit();
      else form.dispatchEvent(new Event('submit', {cancelable: true, bubbles: true}));
    });
    window.requestAnimationFrame(autoGrow);
  }

  // A server-rendered thread opens at the newest turn, not the oldest.
  if (thread && thread.querySelector('.chat-message')) {
    window.requestAnimationFrame(function () { scrollToBottom(false); });
  }

  function adoptArticle(message, adopted) {
    // Optimistic bubbles carry no message id until the server names them.
    var candidates = thread.querySelectorAll('.chat-message-' + message.role + ':not([data-message-id])');
    for (var index = 0; index < candidates.length; index += 1) {
      var candidate = candidates[index];
      if (adopted.indexOf(candidate) !== -1) continue;
      if (candidate.dataset.turnId && candidate.dataset.turnId !== message.turn_id) continue;
      return candidate;
    }
    return null;
  }

  function messageArticle(message, existing) {
    var article = existing;
    if (!article) {
      article = document.createElement('article');
      article.className = messageClasses(message.role);
    }
    article.dataset.messageId = message.id;
    article.dataset.turnId = message.turn_id;
    article.dataset.versionGroupId = message.version_group;
    var byline = article.querySelector('.chat-byline');
    if (!byline) {
      byline = createByline(message.role);
      article.insertBefore(byline, article.firstChild);
    }
    renderTurnMeta(byline, message);
    var content = article.querySelector('.chat-content');
    if (!content) {
      content = document.createElement('div');
      content.className = 'chat-content p-prose';
      article.appendChild(content);
    }
    content.classList.add('sdanswer-prose');
    return article;
  }

  // Who spoke, on the left; which model answered and how long it took, on the
  // right. The right half only exists for finished assistant turns — while one
  // is running the activity row below already carries a live timer.
  function createByline(role) {
    var byline = document.createElement('div');
    byline.className = 'chat-byline';
    var label = document.createElement('div');
    label.className = 'chat-role p-label';
    if (role === 'assistant') {
      var mark = document.createElement('span');
      mark.className = 'p-mark is-accent';
      mark.setAttribute('aria-hidden', 'true');
      label.appendChild(mark);
    }
    label.appendChild(document.createTextNode(
      role === 'user' ? 'You' : (mode === 'resources' ? 'Resource Agent' : 'Smarter.Dev')
    ));
    byline.appendChild(label);
    return byline;
  }

  function renderTurnMeta(byline, message) {
    var meta = byline.querySelector('[data-turn-meta]');
    if (message.role !== 'assistant' || !message.model_key) {
      if (meta) meta.remove();
      return;
    }
    if (!meta) {
      meta = document.createElement('div');
      meta.className = 'chat-byline-meta p-meta';
      meta.dataset.turnMeta = '';
      byline.appendChild(meta);
    }
    meta.textContent = message.model_key + (
      message.elapsed === null || message.elapsed === undefined
        ? ''
        : ' · ' + message.elapsed + 's'
    );
  }

  // Matches the inline SVG the template emits, so a reconciled attachment row
  // is indistinguishable from a server-rendered one.
  var SVG_NS = 'http://www.w3.org/2000/svg';
  function fileGlyph() {
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 14 16');
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', 'M8.5.5H2.5a1 1 0 0 0-1 1v13a1 1 0 0 0 1 1h9a1 1 0 0 0 1-1V4.5zM8.5.5V4.5h4');
    path.setAttribute('fill', 'none');
    path.setAttribute('stroke', 'currentColor');
    path.setAttribute('stroke-width', '1');
    svg.appendChild(path);
    return svg;
  }

  function renderSentAttachments(article, message) {
    var box = article.querySelector('.chat-sent-attachments');
    var items = message.attachments || [];
    if (!items.length) {
      if (box) box.remove();
      return;
    }
    if (!box) {
      box = document.createElement('div');
      box.className = 'chat-sent-attachments';
      article.appendChild(box);
    }
    box.textContent = '';
    items.forEach(function (item) {
      var link = document.createElement('a');
      link.className = 'chat-sent-attachment';
      link.href = '/v2/api/chat/conversations/' + conversationId + '/attachments/' + item.id;
      link.appendChild(fileGlyph());
      link.appendChild(document.createTextNode(item.name));
      box.appendChild(link);
    });
  }

  function renderStoppedBadge(article, message) {
    var badge = article.querySelector('.chat-stopped');
    if (!message.stopped) {
      if (badge) badge.remove();
      return;
    }
    if (badge) return;
    badge = document.createElement('span');
    badge.className = 'chat-stopped';
    badge.textContent = 'Stopped';
    article.insertBefore(badge, article.querySelector('.chat-response-actions'));
  }

  function renderResponseActions(article, message, choices, locked) {
    var actions = article.querySelector('.chat-response-actions');
    if (!actions) {
      actions = document.createElement('div');
      actions.className = 'chat-response-actions';
      article.appendChild(actions);
    }
    var regenerate = actions.querySelector('[data-regenerate]');
    if (!regenerate) {
      regenerate = document.createElement('button');
      regenerate.type = 'button';
      regenerate.className = 'p-btn is-bare';
      regenerate.dataset.regenerate = '';
      regenerate.textContent = 'Regenerate';
      actions.appendChild(regenerate);
    }
    regenerate.dataset.turnId = message.turn_id;
    regenerate.disabled = Boolean(locked);
    var select = actions.querySelector('[data-version-group]');
    if ((choices || []).length < 2) {
      if (select) select.remove();
      return;
    }
    if (!select) {
      select = document.createElement('select');
      select.className = 'p-select';
      select.dataset.versionGroup = '';
      actions.appendChild(select);
    }
    select.textContent = '';
    choices.forEach(function (choice) {
      var option = document.createElement('option');
      option.value = choice.id;
      option.textContent = 'Response ' + choice.version_number;
      option.selected = Boolean(choice.is_active);
      select.appendChild(option);
    });
  }

  function renderMessage(article, message, choices, locked) {
    // Markdown is rendered server-side and shipped as content_html so the client
    // never needs a second markdown implementation.
    var content = article.querySelector('.chat-content');
    if (message.role === 'assistant' && !message.content) content.textContent = 'Working…';
    else content.innerHTML = message.content_html || '';
    renderSentAttachments(article, message);
    if (message.role === 'assistant' && mode === 'chat') {
      renderResponseActions(article, message, choices, locked);
    }
    renderStoppedBadge(article, message);
  }

  function visibleDocuments(snapshot) {
    var visible = {};
    (snapshot.messages || []).forEach(function (message) {
      if (message.role === 'assistant' && message.is_active) visible[message.id] = true;
    });
    return (snapshot.documents || []).filter(function (item) {
      return !item.assistant_message_id || visible[item.assistant_message_id];
    });
  }

  // Rebuild the thread from the durable snapshot: active versions only, rendered
  // markdown, regenerate/version controls, and no stale placeholders. This is the
  // client-side equivalent of the server-rendered page after a reload.
  function syncThread(snapshot) {
    if (mode !== 'chat' || !thread) return;
    var messages = snapshot.messages || [];
    var activeTurnId = snapshot.active_turn && snapshot.active_turn.id;
    var locked = Boolean(activeTurnId);
    var versions = {};
    messages.forEach(function (message) {
      if (message.role !== 'assistant') return;
      versions[message.version_group] = versions[message.version_group] || [];
      versions[message.version_group].push(message);
    });
    var empty = thread.querySelector('.chat-empty');
    if (empty && messages.length) empty.remove();
    var kept = [];
    var adopted = [];
    messages.filter(function (message) {
      return message.role !== 'assistant' || message.is_active;
    }).forEach(function (message) {
      var article = thread.querySelector('[data-message-id="' + message.id + '"]') || adoptArticle(message, adopted);
      if (article && !article.dataset.messageId) adopted.push(article);
      article = messageArticle(message, article);
      var streaming = Boolean(activeTurnId && message.role === 'assistant' && message.turn_id === activeTurnId);
      if (streaming) {
        article.dataset.pendingTurn = activeTurnId;
      } else {
        article.removeAttribute('data-pending-turn');
        var activity = article.querySelector('[data-root-activity]');
        if (activity) activity.remove();
        renderMessage(article, message, versions[message.version_group], locked);
      }
      thread.insertBefore(article, statusEl || null);
      kept.push(article);
    });
    Array.prototype.forEach.call(thread.querySelectorAll('.chat-message'), function (article) {
      if (kept.indexOf(article) === -1) article.remove();
    });
  }

  function createActivity(startedAt, label) {
    var activity = document.createElement('div');
    activity.className = 'chat-agent-activity';
    activity.dataset.rootActivity = '';
    activity.dataset.startedAt = startedAt || new Date().toISOString();
    var pulse = document.createElement('span');
    pulse.className = 'p-mark is-live';
    pulse.setAttribute('aria-hidden', 'true');
    var text = document.createElement('span');
    text.dataset.activityLabel = '';
    text.textContent = label || 'Working…';
    var timer = document.createElement('time');
    timer.dataset.activityTimer = '';
    timer.textContent = '0:00';
    activity.append(pulse, text, timer);
    return activity;
  }

  function elapsed(startedAt, finishedAt) {
    var start = Date.parse(startedAt || '');
    var finish = finishedAt ? Date.parse(finishedAt) : Date.now();
    if (!Number.isFinite(start) || !Number.isFinite(finish)) return '0:00';
    var seconds = Math.max(0, Math.floor((finish - start) / 1000));
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var remainder = seconds % 60;
    return (hours ? hours + ':' + String(minutes).padStart(2, '0') : minutes) + ':' + String(remainder).padStart(2, '0');
  }

  function refreshActivityTimers() {
    document.querySelectorAll('[data-activity-timer]').forEach(function (timer) {
      var holder = timer.closest('[data-started-at]');
      if (!holder) return;
      timer.textContent = elapsed(holder.dataset.startedAt, holder.dataset.finishedAt);
    });
  }

  function rootArticle(turnId) {
    if (turnId) {
      return thread.querySelector('[data-pending-turn="' + turnId + '"]') || thread.querySelector('[data-turn-id="' + turnId + '"]');
    }
    return thread.querySelector('[data-pending-turn]:last-of-type') || thread.querySelector('.chat-message-assistant:last-of-type');
  }

  function updateRootActivity(data) {
    if (mode !== 'chat') return;
    var turnId = data.turn_id || activeTurn;
    var article = rootArticle(turnId);
    if (!article) return;
    var activity = article.querySelector('[data-root-activity]');
    if (!activity) {
      activity = createActivity(data.started_at || data.occurred_at, data.status);
      article.insertBefore(activity, article.querySelector('.chat-content'));
    }
    if (data.started_at) activity.dataset.startedAt = data.started_at;
    if (data.finished_at) activity.dataset.finishedAt = data.finished_at;
    activity.dataset.phase = data.phase || 'running';
    var label = activity.querySelector('[data-activity-label]');
    if (label && data.status !== undefined) label.textContent = data.status || '';
    activity.hidden = !data.status;
    refreshActivityTimers();
  }

  // The panel stays collapsed to a count until the reader asks for detail.
  function syncSubagentSummary() {
    var summary = document.querySelector('[data-chat-subagent-summary]');
    if (!summary || !subagentList) return;
    var count = subagentList.children.length;
    summary.textContent = count + (count === 1 ? ' sub-agent' : ' sub-agents');
  }

  function terminalPhase(status) {
    return ['complete', 'error', 'cancelled', 'usage_limited', 'lease_lost'].indexOf(status) !== -1;
  }

  function updateSubagentActivity(data) {
    if (!subagentList || !data.subagent_id) return;
    var row = subagentList.querySelector('[data-subagent-id="' + data.subagent_id + '"]');
    if (!row) {
      row = document.createElement('div');
      row.className = 'chat-agent-row';
      row.dataset.subagentId = data.subagent_id;
      var name = document.createElement('span');
      name.className = 'chat-agent-name';
      var state = document.createElement('span');
      state.className = 'chat-agent-status';
      var pulse = document.createElement('span');
      pulse.className = 'p-mark is-live';
      state.appendChild(pulse);
      var stateText = document.createElement('span');
      stateText.dataset.agentStatus = '';
      state.appendChild(stateText);
      var timer = document.createElement('time');
      timer.dataset.activityTimer = '';
      row.append(name, state, timer);
      subagentList.appendChild(row);
    }
    row.dataset.startedAt = row.dataset.startedAt || data.started_at || data.queued_at || data.occurred_at || new Date().toISOString();
    if (data.finished_at) row.dataset.finishedAt = data.finished_at;
    var phase = data.phase || (terminalPhase(data.status) ? 'complete' : 'running');
    row.dataset.phase = phase;
    var nameEl = row.querySelector('.chat-agent-name');
    var statusEl = row.querySelector('[data-agent-status]');
    var name = data.subagent_name || data.name || nameEl.textContent || 'Sub-agent';
    var status = data.activity || data.status || 'Working…';
    if (status.indexOf(name + ': ') === 0) status = status.slice(name.length + 2);
    nameEl.textContent = name;
    statusEl.textContent = status.replace(/_/g, ' ');
    if (agentPanel) agentPanel.hidden = false;
    syncSubagentSummary();
    refreshActivityTimers();
  }

  function syncAgentActivity(snapshot) {
    if (!snapshot.active_turn || !subagentList) {
      if (agentPanel) agentPanel.hidden = true;
      return;
    }
    var turnId = snapshot.active_turn.id;
    var latestRoot = null;
    var latestChildren = {};
    (snapshot.activity_events || snapshot.events || []).forEach(function (event) {
      if (event.turn_id !== turnId || (event.type !== 'chat_tool_event' && event.type !== 'chat_run_state')) return;
      var payload = event.payload || {};
      if (payload.scope === 'subagent' && payload.subagent_id) latestChildren[payload.subagent_id] = payload;
      if (payload.scope === 'root' || event.type === 'chat_run_state') latestRoot = payload;
    });
    updateRootActivity(Object.assign({
      turn_id: turnId,
      started_at: snapshot.active_turn.started_at,
      status: snapshot.active_turn.status === 'stopping' ? 'Stopping…' : 'Working…'
    }, latestRoot || {}));
    subagentList.textContent = '';
    (snapshot.subagents || []).filter(function (child) {
      return child.root_turn_id === turnId;
    }).forEach(function (child) {
      var activity = latestChildren[child.id] || {};
      updateSubagentActivity(Object.assign({}, child, activity, {
        subagent_id: child.id,
        subagent_name: child.name,
        activity: activity.status,
      }));
    });
    if (agentPanel) agentPanel.hidden = !subagentList.children.length;
    syncSubagentSummary();
  }

  // Timers only update local DOM; live state remains notification-driven.
  window.setInterval(refreshActivityTimers, 1000);

  function submissionKey() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  }

  function fillReasoning(modelKey, target, selectedValue) {
    if (!catalog || !target) return;
    var model = catalog.models.find(function (item) { return item.key === modelKey; });
    target.textContent = '';
    var base = document.createElement('option');
    base.value = '';
    base.textContent = 'Model default';
    target.appendChild(base);
    (model && model.reasoning_levels || []).forEach(function (level) {
      var option = document.createElement('option');
      option.value = level;
      option.textContent = level;
      target.appendChild(option);
    });
    target.value = selectedValue || '';
  }

  function activateConversationControls(persistedModel, persistedReasoning) {
    if (!catalog || !conversationId) return;
    // Model and reasoning only mean something once a conversation exists.
    if (settingsDisclosure) settingsDisclosure.hidden = false;
    var current = document.querySelector('[data-chat-model]');
    if (current) {
      current.textContent = '';
      catalog.models.forEach(function (model) {
        var option = document.createElement('option');
        option.value = model.key;
        option.textContent = model.label + ' · ' + model.cost_tier;
        current.appendChild(option);
      });
      if (!catalog.models.some(function (item) { return item.key === persistedModel; })) {
        var unavailable = document.createElement('option');
        unavailable.value = persistedModel;
        unavailable.textContent = persistedModel + ' · unavailable (select a new model)';
        current.appendChild(unavailable);
        showError('The selected model is unavailable. Select and confirm a new model.');
      }
      current.value = persistedModel;
      current.dataset.original = current.value;
      syncModelLabel(current.value);
      if (!current.dataset.bound) {
        current.addEventListener('change', proposeModel);
        current.dataset.bound = 'true';
      }
    }
    var reasoning = document.querySelector('[data-chat-reasoning]');
    if (reasoning && current) {
      fillReasoning(current.value, reasoning, persistedReasoning || '');
      reasoning.dataset.original = reasoning.value;
      if (!reasoning.dataset.bound) {
        reasoning.addEventListener('change', function () {
          var desired = reasoning.value;
          api('/v2/api/chat/conversations/' + conversationId + '/reasoning', json('PATCH', {reasoning_level: desired || null})).then(function (result) {
            reasoning.value = result.reasoning_level || '';
            reasoning.dataset.original = reasoning.value;
            shell.dataset.reasoningLevel = reasoning.value;
          }).catch(function (error) {
            reasoning.value = reasoning.dataset.original || '';
            showError(error.message);
          });
        });
        reasoning.dataset.bound = 'true';
      }
    }
    setBusy(Boolean(activeTurn));
  }

  function loadCatalog() {
    if (mode !== 'chat') return Promise.resolve();
    return api('/v2/api/chat/catalog').then(function (data) {
      catalog = data;
      var intelligence = document.querySelector('[data-chat-new-intelligence]');
      if (intelligence) intelligence.value = data.defaults.intelligence_mode;
      var select = document.querySelector('[data-chat-new-model]');
      if (select) {
        select.textContent = '';
        data.models.forEach(function (model) {
          var option = document.createElement('option');
          option.value = model.key;
          option.textContent = model.label + ' · ' + model.cost_tier;
          option.selected = model.key === data.defaults.model_key;
          select.appendChild(option);
        });
        fillReasoning(select.value, document.querySelector('[data-chat-new-reasoning]'), data.defaults.reasoning_level);
        select.addEventListener('change', function () {
          fillReasoning(select.value, document.querySelector('[data-chat-new-reasoning]'), null);
        });
      }
      activateConversationControls(
        shell.dataset.modelKey,
        shell.dataset.reasoningLevel || ''
      );
    }).catch(function (error) { showError(error.message); });
  }

  function proposeModel(event) {
    var select = event.currentTarget;
    var target = select.value;
    api('/v2/api/chat/conversations/' + conversationId + '/model-changes', json('POST', {model_key: target})).then(function (change) {
      pendingChange = change;
      closeDisclosure(settingsDisclosure, false);
      var dialog = document.querySelector('[data-model-dialog]');
      dialog.querySelector('[data-model-warning]').textContent = change.warning;
      dialog.hidden = false;
    }).catch(function (error) {
      select.value = select.dataset.original;
      showError(error.message);
    });
  }

  var dialog = document.querySelector('[data-model-dialog]');
  if (dialog) {
    dialog.querySelector('[data-model-cancel]').addEventListener('click', function () {
      var select = document.querySelector('[data-chat-model]');
      if (select) select.value = select.dataset.original;
      dialog.hidden = true;
      pendingChange = null;
    });
    dialog.querySelector('[data-model-confirm]').addEventListener('click', function () {
      if (!pendingChange) return;
      api('/v2/api/chat/conversations/' + conversationId + '/model-changes/' + pendingChange.id + '/confirm', json('POST', {})).then(function (data) {
        var select = document.querySelector('[data-chat-model]');
        select.dataset.original = data.model_key;
        select.value = data.model_key;
        syncModelLabel(data.model_key);
        shell.dataset.modelKey = data.model_key;
        shell.dataset.reasoningLevel = data.reasoning_level || '';
        fillReasoning(data.model_key, document.querySelector('[data-chat-reasoning]'), data.reasoning_level);
        dialog.hidden = true;
        pendingChange = null;
      }).catch(function (error) {
        var select = document.querySelector('[data-chat-model]');
        if (select) select.value = select.dataset.original;
        dialog.hidden = true;
        pendingChange = null;
        showError(error.message);
      });
    });
  }

  function createConversation() {
    if (conversationId) return Promise.resolve({id: conversationId});
    if (conversationPromise) return conversationPromise;
    var intelligence = document.querySelector('[data-chat-new-intelligence]');
    var model = document.querySelector('[data-chat-new-model]');
    var reasoning = document.querySelector('[data-chat-new-reasoning]');
    conversationPromise = api('/v2/api/chat/conversations', json('POST', {
      intelligence_mode: intelligence.value,
      model_key: model.value,
      reasoning_level: reasoning.value || null,
    })).then(function (data) {
      conversationId = data.id;
      shell.dataset.conversationId = data.id;
      shell.dataset.modelKey = model.value;
      shell.dataset.reasoningLevel = reasoning.value || '';
      history.replaceState({}, '', data.url);
      activateConversationControls(model.value, reasoning.value || '');
      return data;
    }).catch(function (error) {
      conversationPromise = null;
      throw error;
    });
    return conversationPromise;
  }

  function renderAttachment(item) {
    var chip = document.createElement('span');
    chip.className = 'chat-attachment';
    chip.dataset.attachmentId = item.id;
    var name = document.createElement('span');
    name.textContent = item.name;
    var remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.setAttribute('aria-label', 'Remove ' + item.name);
    remove.addEventListener('click', function () {
      api('/v2/api/chat/conversations/' + conversationId + '/attachments/' + item.id, {method: 'DELETE'}).then(function () {
        pendingAttachments = pendingAttachments.filter(function (entry) { return entry.id !== item.id; });
        chip.remove();
        syncMediaInstruction();
      }).catch(function (error) { showError(error.message); });
    });
    chip.append(name, remove);
    document.querySelector('[data-attachments]').appendChild(chip);
  }

  function syncPendingAttachments(items) {
    (items || []).forEach(function (item) {
      if (pendingAttachments.some(function (existing) { return existing.id === item.id; })) return;
      pendingAttachments.push(item);
      renderAttachment(item);
    });
    syncMediaInstruction();
  }

  var files = document.querySelector('[data-chat-files]');
  if (files) files.addEventListener('change', function () {
    var chosen = Array.prototype.slice.call(files.files || []);
    if (pendingAttachments.length + uploadCount + chosen.length > 5) {
      showError('A turn may include at most 5 attachments.');
      files.value = '';
      return;
    }
    var chosenImages = chosen.filter(function (file) {
      return String(file.type || '').indexOf('image/') === 0;
    }).length;
    uploadCount += chosen.length;
    imageUploads += chosenImages;
    syncMediaInstruction();
    setBusy(Boolean(activeTurn));
    var ready = conversationId ? Promise.resolve() : createConversation();
    ready.then(function () {
      chosen.forEach(function (file) {
        var isImage = String(file.type || '').indexOf('image/') === 0;
        if (file.size > 10 * 1024 * 1024) {
          showError(file.name + ' exceeds 10 MB.');
          uploadCount -= 1;
          if (isImage) imageUploads -= 1;
          syncMediaInstruction();
          setBusy(Boolean(activeTurn));
          return;
        }
        var body = new FormData();
        body.append('file', file);
        var instruction = document.querySelector('[data-media-instruction]');
        if (instruction && instruction.value.trim()) body.append('summarization_instruction', instruction.value.trim());
        api('/v2/api/chat/conversations/' + conversationId + '/attachments', {method: 'POST', body: body}).then(function (item) {
          pendingAttachments.push(item);
          renderAttachment(item);
        }).catch(function (error) {
          showError(error.message);
        }).finally(function () {
          uploadCount -= 1;
          if (isImage) imageUploads -= 1;
          syncMediaInstruction();
          setBusy(Boolean(activeTurn));
        });
      });
    }).catch(function (error) {
      uploadCount -= chosen.length;
      imageUploads -= chosenImages;
      syncMediaInstruction();
      setBusy(Boolean(activeTurn));
      showError(error.message);
    });
    files.value = '';
  });

  function submitChat(text) {
    function send() {
      return api('/v2/api/chat/conversations/' + conversationId + '/turns', json('POST', {
        content: text,
        submission_key: submissionKey(),
        attachment_ids: pendingAttachments.map(function (item) { return item.id; }),
      }));
    }
    return (conversationId ? Promise.resolve() : createConversation()).then(send);
  }

  function submitResources(text) {
    return api('/v2/api/agent/conversations/' + conversationId + '/reply', json('POST', {
      question: text,
      submission_key: submissionKey(),
    }));
  }

  // One send path for both entry points: the composer, and the quote box that
  // appears when you select text in a document. Returns false when the draft is
  // rejected, so a caller can leave the reader's text where it is.
  function sendMessage(text, build) {
    showError('');
    if (uploadCount) { showError('Wait for attachments to finish uploading.'); return false; }
    text = (text || '').trim();
    if (!text) { showError('Type a message first.'); return false; }
    if (text.length > 5000) { showError('Keep it under 5000 characters.'); return false; }
    // The invitation is answered — drop it now rather than leaving it centred
    // above the first exchange until the turn finishes and reconcile runs.
    var empty = thread.querySelector('.chat-empty');
    if (empty) empty.remove();
    var user = bubble('user', text, null, build);
    var assistant = bubble('assistant', '');
    thread.insertBefore(user, statusEl);
    thread.insertBefore(assistant, statusEl);
    // The reader just acted, so follow their own message down unconditionally.
    scrollToBottom(false);
    setBusy(true);
    setStatus('Starting…');
    (mode === 'chat' ? submitChat(text) : submitResources(text)).then(function (result) {
      activeTurn = result.turn_id || null;
      if (mode === 'resources') resourceRunning = true;
      assistant.dataset.pendingTurn = activeTurn || '';
      assistant.dataset.turnId = activeTurn || '';
      flushPendingDocuments();
      updateRootActivity({
        turn_id: activeTurn,
        status: ['disconnected', 'suspended', 'reconnecting'].indexOf(notificationStreamStatus) !== -1 ? 'Connection lost; reconnecting…' : 'Working…'
      });
      setBusy(true);
      pendingAttachments = [];
      var box = document.querySelector('[data-attachments]');
      if (box) box.textContent = '';
      syncMediaInstruction();
      if (mode === 'resources') setStatus('Resource Agent is working…');
    }).catch(function (error) {
      assistant.querySelector('.chat-content').textContent = error.message;
      setStatus('');
      showError(error.message);
      setBusy(false);
    });
    return true;
  }

  if (form) form.addEventListener('submit', function (event) {
    event.preventDefault();
    if (!sendMessage(input.value)) return;
    input.value = '';
    autoGrow();
  });

  if (stopBtn) stopBtn.addEventListener('click', function () {
    if (!activeTurn) return;
    stopBtn.disabled = true;
    api('/v2/api/chat/conversations/' + conversationId + '/turns/' + activeTurn + '/stop', json('POST', {})).then(function (result) {
      setStatus(result.status === 'stopped' ? '' : 'Stopping…');
      if (result.status === 'stopped') finishTurn();
    }).catch(function (error) {
      stopBtn.disabled = false;
      showError(error.message);
    });
  });

  document.addEventListener('click', function (event) {
    // An inline card's Preview opens the same panel the dock list does, so a
    // document is only ever read in one place.
    var documentButton = event.target.closest('[data-open-document]');
    if (documentButton) {
      openDocument(documentButton.dataset.documentId, documentButton);
      return;
    }
    var button = event.target.closest('[data-regenerate]');
    if (!button) return;
    setBusy(true);
    api('/v2/api/chat/conversations/' + conversationId + '/turns/' + button.dataset.turnId + '/regenerate', json('POST', {})).then(function (data) {
      activeTurn = data.turn_id;
      var wasAtBottom = atBottom();
      var placeholder = bubble('assistant', '', data.turn_id);
      placeholder.dataset.pendingTurn = data.turn_id;
      button.closest('.chat-message').after(placeholder);
      stick(wasAtBottom);
      updateRootActivity({turn_id: data.turn_id, status: 'Regenerating…'});
      setBusy(true);
    }).catch(function (error) {
      setBusy(false);
      showError(error.message);
    });
  });

  document.addEventListener('change', function (event) {
    var select = event.target.closest('[data-version-group]');
    if (!select) return;
    var article = select.closest('[data-turn-id]');
    var turnId = article && article.dataset.turnId;
    if (!turnId) return;
    api('/v2/api/chat/conversations/' + conversationId + '/turns/' + turnId + '/selected-version', json('PATCH', {message_id: select.value})).then(function () {
      reconcile();
    }).catch(function (error) { showError(error.message); });
  });

  function finishTurn() {
    setStatus('');
    activeTurn = null;
    if (stopBtn) {
      stopBtn.hidden = true;
      stopBtn.disabled = false;
    }
    setBusy(false);
    if (agentPanel) {
      agentPanel.hidden = true;
      agentPanel.open = false;
    }
    refreshUsage();
  }

  function handleTerminal(data, type) {
    var wasAtBottom = atBottom();
    var pending = thread.querySelector('[data-pending-turn="' + data.turn_id + '"] .chat-content');
    if (pending && data.content !== undefined) pending.textContent = data.content || '';
    stick(wasAtBottom);
    if (type === 'chat_turn_error' || type === 'agent_run_error') {
      if (pending) pending.textContent = data.detail || 'The run failed.';
      showError(data.detail || 'The run failed.');
    }
    finishTurn();
    if (mode === 'chat') {
      // Reconcile against the durable snapshot instead of reloading. The
      // conversation GET ships rendered markdown, version groups, and documents,
      // so placeholders, alternatives, and controls consolidate without losing
      // scroll position, composer text, or focus.
      reconcile();
      return;
    }
    if (type === 'agent_run_error') {
      // Resources answers are enriched server-side (sdanswer blocks), so their
      // terminal rendering still comes from a fresh page render.
      window.setTimeout(function () { location.reload(); }, 50);
    }
  }

  function notification(event) {
    // Skrift ≥0.2.0a17 nests event fields under detail.payload; the envelope
    // keeps only {type, id, mode, created_at, group}.
    var envelope = event.detail || {};
    var type = envelope.type;
    var data = envelope.payload || {};
    if (data.conversation_id !== conversationId) return;
    if (mode === 'chat' && activeTurn && data.turn_id && data.turn_id !== activeTurn) return;
    if (type === 'chat_tool_event') {
      if (data.scope === 'subagent') updateSubagentActivity(data);
      else updateRootActivity(data);
    }
    if (type === 'chat_run_state') updateRootActivity(data);
    if (type === 'chat_subagent_state') updateSubagentActivity(data);
    if (type === 'chat_document_created') syncDocuments([data]);
    if (type === 'chat_output_delta') {
      var wasAtBottom = atBottom();
      var pending = thread.querySelector('[data-pending-turn="' + data.turn_id + '"] .chat-content') || thread.querySelector('[data-turn-id="' + data.turn_id + '"] .chat-content');
      if (pending) pending.textContent = data.content || '';
      updateRootActivity({turn_id: data.turn_id, status: 'Writing response…'});
      stick(wasAtBottom);
    }
    if (type === 'chat_usage_updated') refreshUsage();
    if (type === 'agent_run_complete' && mode === 'resources') {
      window.location.reload();
      return;
    }
    if (type === 'chat_turn_complete' || type === 'chat_turn_stopped' || type === 'chat_turn_error' || type === 'agent_run_error') {
      handleTerminal(data, type);
    }
    if (type === 'agent_tool_event') setStatus(data.label || data.status || 'Resource Agent is working…');
  }

  document.addEventListener('sk:notification', notification);
  window.addEventListener('sk:notification', notification);

  function refreshUsage() {
    if (mode !== 'chat' || !conversationId) return Promise.resolve();
    return api('/v2/api/chat/conversations/' + conversationId + '/usage').then(function (metrics) {
      document.querySelector('[data-context-tokens]').textContent = metrics.current_context_tokens;
      document.querySelector('[data-subagent-tokens]').textContent = metrics.subagent_tokens;
      document.querySelector('[data-total-tokens]').textContent = metrics.total_tokens;
      var conversationPercent = metrics.four_hour_percent_conversation.toFixed(1);
      document.querySelector('[data-conversation-percent]').textContent = conversationPercent;
      var echo = document.querySelector('[data-conversation-percent-echo]');
      if (echo) echo.textContent = conversationPercent;
      document.querySelector('[data-all-percent]').textContent = metrics.four_hour_percent_all_chat.toFixed(1);
      // The meter reads the same number as the label beside it. It changes
      // colour before it fills, so the warning arrives before the wall does.
      var meter = document.querySelector('[data-context-meter]');
      if (meter) {
        var used = Math.max(0, Math.min(100, metrics.four_hour_percent_conversation));
        meter.firstElementChild.style.width = used + '%';
        meter.classList.toggle('is-warn', used >= 60 && used < 85);
        meter.classList.toggle('is-bad', used >= 85);
      }
    }).catch(function () {});
  }

  function reconcile() {
    if (!conversationId) return;
    if (mode === 'resources') {
      api('/v2/api/agent/conversations/' + conversationId + '/status').then(function (snapshot) {
        if (snapshot.active) {
          resourceRunning = true;
          setStatus('Resource Agent is working…');
        } else if (resourceRunning) {
          resourceRunning = false;
          window.location.reload();
        }
      }).catch(function () {});
      return;
    }
    api('/v2/api/chat/conversations/' + conversationId).then(function (snapshot) {
      var scroll = captureScroll();
      var active = snapshot.active_turn;
      syncThread(snapshot);
      if (active) {
        activeTurn = active.id;
        setStatus(active.status === 'stopping' ? 'Stopping…' : 'Working…');
        var target = thread.querySelector('[data-pending-turn="' + active.id + '"] .chat-content') || thread.querySelector('[data-turn-id="' + active.id + '"] .chat-content');
        if (target && active.partial) target.textContent = active.partial;
        setBusy(true);
      } else if (activeTurn) {
        finishTurn();
      } else {
        setBusy(false);
      }
      syncPendingAttachments(snapshot.pending_attachments);
      syncDocuments(visibleDocuments(snapshot));
      syncAgentActivity(snapshot);
      restoreScroll(scroll);
    }).catch(function () {});
    refreshUsage();
  }

  // Skrift notifications drive live output and terminal state. Reconcile only
  // once on load and whenever the notification stream reconnects, so a missed
  // ephemeral event cannot strand the UI without creating a polling loop.
  var notificationHasConnected = false;
  var notificationNeedsReconcile = false;
  document.addEventListener('sk:notification-status', function (event) {
    var status = event.detail && event.detail.status;
    notificationStreamStatus = status;
    syncConnection(status);
    if (status === 'connected') {
      if (notificationNeedsReconcile) reconcile();
      notificationHasConnected = true;
      notificationNeedsReconcile = false;
    } else if (status === 'disconnected' || status === 'suspended' || status === 'reconnecting') {
      notificationNeedsReconcile = notificationHasConnected;
      if (activeTurn) updateRootActivity({turn_id: activeTurn, status: 'Connection lost; reconnecting…'});
    }
  });

  // Adopt the server-rendered rows so the count and empty state are right
  // before any fetch resolves, then restore the reader's saved layout.
  if (dockList) {
    Array.prototype.forEach.call(dockList.querySelectorAll('[data-dock-open]'), function (row) {
      dockDocuments.push({id: row.dataset.documentId});
    });
    syncDockList([]);
  }
  if (dock) {
    var savedRail = stored('chat.rail');
    if (savedRail) {
      railUserSet = true;
      setRail(savedRail !== 'collapsed', null, true);
    }
    // Only restore an open dock when there is something in it — otherwise a
    // brand-new conversation opens onto an empty panel.
    var wantDock = stored('chat.dock') === 'open' && dockDocuments.length > 0;
    setDock(wantDock);
    showDockList();
    // Only a stored width overrides the stylesheet's default. Measuring the
    // dock here would read 0 while it is shut and pin it to the minimum.
    var width = savedDockWidth();
    if (width) applyDockWidth(width, false);
    else syncResizeRange();
  }

  loadCatalog().then(function () {
    refreshUsage();
    if (conversationId) reconcile();
    if (activeTurn || resourceRunning) setBusy(true);
  });
})();
