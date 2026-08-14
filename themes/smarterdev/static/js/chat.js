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
  // Live text of every document currently being written, keyed by id.
  var streams = {};
  // The panel switches itself to a file the agent starts writing, so the reader
  // watches it appear. Dismissed once per turn: a reader who closes the panel or
  // steps back to the list has said they would rather not be moved again.
  var dockFollowStream = true;
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

  // ── History rail rows ─────────────────────────────────
  // Rename, archive and delete act on a row without leaving the conversation
  // that is on screen, so the rail is edited in place rather than reloaded —
  // a reload here would throw away composer text and a running turn's scroll
  // position for the sake of a row moving 200px.
  var historyNav = document.querySelector('.chat-history');
  var rowMenu = document.querySelector('[data-row-menu]');
  var deleteDialog = document.querySelector('[data-delete-dialog]');
  var menuRow = null;
  var menuButton = null;
  var pendingDelete = null;

  function rowLink(row) { return row && row.querySelector('[data-history-title]') }

  function rowTitle(row) {
    var link = rowLink(row);
    return link ? link.textContent.trim() : '';
  }

  function closeRowMenu(restoreFocus) {
    if (!rowMenu || rowMenu.hidden) return;
    rowMenu.hidden = true;
    if (menuButton) {
      menuButton.setAttribute('aria-expanded', 'false');
      if (restoreFocus) menuButton.focus();
    }
    menuRow = null;
    menuButton = null;
  }

  function openRowMenu(button) {
    if (!rowMenu) return;
    var row = button.closest('[data-history-row]');
    if (!row) return;
    if (menuButton === button) { closeRowMenu(true); return; }
    closeRowMenu(false);
    menuRow = row;
    menuButton = button;
    var archived = row.dataset.archived === 'true';
    rowMenu.querySelector('[data-row-action="archive"]').textContent = archived ? 'Unarchive' : 'Archive';
    rowMenu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    // Measured only once it is displayed; a hidden element has no size.
    var anchor = button.getBoundingClientRect();
    var size = rowMenu.getBoundingClientRect();
    var left = Math.min(Math.max(8, anchor.right - size.width), window.innerWidth - size.width - 8);
    var below = anchor.bottom + 4;
    var top = below + size.height > window.innerHeight - 8 ? Math.max(8, anchor.top - size.height - 4) : below;
    rowMenu.style.left = left + 'px';
    rowMenu.style.top = top + 'px';
    var first = rowMenu.querySelector('button');
    if (first) first.focus();
  }

  // A row's group heading is a sibling that precedes it, so a band that has
  // just lost its last row leaves a heading over nothing.
  function pruneGroups() {
    if (!historyNav) return;
    Array.prototype.forEach.call(historyNav.querySelectorAll(':scope > .chat-history-group'), function (heading) {
      var next = heading.nextElementSibling;
      if (!next || !next.hasAttribute('data-history-row')) heading.remove();
    });
  }

  function archiveDrawer(create) {
    var drawer = historyNav && historyNav.querySelector('[data-history-archive]');
    if (drawer || !create || !historyNav) return drawer;
    drawer = document.createElement('details');
    drawer.className = 'chat-history-archive';
    drawer.setAttribute('data-history-archive', '');
    drawer.innerHTML = '<summary class="p-label chat-history-group">Archived<span class="chat-history-archive-count"></span></summary>';
    historyNav.appendChild(drawer);
    return drawer;
  }

  function syncArchiveDrawer() {
    var drawer = archiveDrawer(false);
    if (!drawer) return;
    var rows = drawer.querySelectorAll('[data-history-row]');
    if (!rows.length) { drawer.remove(); return; }
    var count = drawer.querySelector('.chat-history-archive-count');
    if (count) count.textContent = rows.length;
  }

  function setRowArchived(row, archived) {
    row.dataset.archived = archived ? 'true' : 'false';
    if (archived) {
      archiveDrawer(true).appendChild(row);
    } else if (historyNav) {
      // Back into the live list at the top: it was just touched, so the first
      // band is where the server would put it on the next load anyway — under
      // that band's heading, not above it.
      var first = historyNav.firstElementChild;
      var anchor = first && first.classList.contains('chat-history-group')
        ? first.nextElementSibling
        : first;
      historyNav.insertBefore(row, anchor);
    }
    pruneGroups();
    syncArchiveDrawer();
  }

  function applyTitle(id, title) {
    var row = historyNav && historyNav.querySelector('[data-conversation-id="' + id + '"]');
    var link = rowLink(row);
    if (link) link.textContent = title;
    if (id !== conversationId) return;
    var heading = document.querySelector('.chat-header h1');
    if (heading) heading.textContent = title;
    document.title = title + ' · Smarter Dev';
  }

  function beginRename(row) {
    var link = rowLink(row);
    if (!link || row.querySelector('.chat-history-rename')) return;
    var original = link.textContent.trim();
    var field = document.createElement('input');
    field.type = 'text';
    field.className = 'chat-history-rename';
    field.maxLength = 120;
    field.value = original;
    field.setAttribute('aria-label', 'Chat name');
    link.hidden = true;
    row.insertBefore(field, link);
    field.focus();
    field.select();
    var settled = false;

    function finish(save) {
      if (settled) return;
      settled = true;
      var wanted = field.value.trim();
      field.remove();
      link.hidden = false;
      if (!save || !wanted || wanted === original) return;
      link.textContent = wanted;
      api('/v2/api/chat/conversations/' + row.dataset.conversationId, json('PATCH', {title: wanted})).then(function (data) {
        applyTitle(row.dataset.conversationId, data.title);
      }).catch(function (error) {
        link.textContent = original;
        showError(error.message || 'That name could not be saved.');
      });
    }

    field.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); finish(true); }
      else if (event.key === 'Escape') { event.preventDefault(); finish(false); }
    });
    field.addEventListener('blur', function () { finish(true); });
  }

  function archiveRow(row) {
    var archived = row.dataset.archived !== 'true';
    api('/v2/api/chat/conversations/' + row.dataset.conversationId, json('PATCH', {archived: archived})).then(function () {
      setRowArchived(row, archived);
      var drawer = archiveDrawer(false);
      if (archived && drawer && row.dataset.conversationId === conversationId) drawer.open = true;
    }).catch(function (error) {
      showError(error.message || 'That chat could not be archived.');
    });
  }

  function askDelete(row) {
    if (!deleteDialog) return;
    pendingDelete = row;
    deleteDialog.querySelector('[data-delete-title]').textContent = rowTitle(row) || 'Untitled chat';
    deleteDialog.hidden = false;
    deleteDialog.querySelector('[data-delete-confirm]').focus();
  }

  if (rowMenu) {
    document.addEventListener('click', function (event) {
      var trigger = event.target.closest && event.target.closest('[data-history-menu]');
      if (trigger) { event.preventDefault(); openRowMenu(trigger); return; }
      if (!rowMenu.contains(event.target)) closeRowMenu(false);
    });

    document.addEventListener('keydown', function (event) {
      if (rowMenu.hidden) return;
      if (event.key === 'Escape') { event.preventDefault(); closeRowMenu(true); return; }
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      var items = Array.prototype.slice.call(rowMenu.querySelectorAll('button'));
      var at = items.indexOf(document.activeElement);
      event.preventDefault();
      items[(at + (event.key === 'ArrowDown' ? 1 : items.length - 1) + items.length) % items.length].focus();
    });

    // Anything that moves the rail moves the button the menu is pinned to.
    if (historyNav) historyNav.addEventListener('scroll', function () { closeRowMenu(false); });
    window.addEventListener('resize', function () { closeRowMenu(false); });

    rowMenu.addEventListener('click', function (event) {
      var action = event.target.closest('[data-row-action]');
      var row = menuRow;
      if (!action || !row) return;
      closeRowMenu(false);
      if (action.dataset.rowAction === 'rename') beginRename(row);
      else if (action.dataset.rowAction === 'archive') archiveRow(row);
      else askDelete(row);
    });
  }

  if (deleteDialog) {
    deleteDialog.querySelector('[data-delete-cancel]').addEventListener('click', function () {
      deleteDialog.hidden = true;
      pendingDelete = null;
    });
    deleteDialog.querySelector('[data-delete-confirm]').addEventListener('click', function () {
      if (!pendingDelete) return;
      var row = pendingDelete;
      var id = row.dataset.conversationId;
      var button = deleteDialog.querySelector('[data-delete-confirm]');
      button.disabled = true;
      api('/v2/api/chat/conversations/' + id, {method: 'DELETE'}).then(function () {
        deleteDialog.hidden = true;
        pendingDelete = null;
        row.remove();
        pruneGroups();
        syncArchiveDrawer();
        // The conversation on screen no longer exists; anywhere else is fine.
        if (id === conversationId) window.location.href = '/chat';
      }).catch(function (error) {
        deleteDialog.hidden = true;
        pendingDelete = null;
        showError(error.message || 'That chat could not be deleted.');
      }).then(function () { button.disabled = false; });
    });
  }

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

  // ── Artifacts ─────────────────────────────────────────
  // The panel holds two kinds of file: ones the agent wrote and ones the reader
  // uploaded. They read the same way and preview in the same place, but they are
  // different resources on the server, so which endpoint to ask is decided from
  // the row's own origin rather than guessed from the id.

  function isUpload(item) {
    return (item || {}).origin === 'upload';
  }

  // Mirrors attachment_kind() on the server, for rows built from an upload
  // response that predates the next snapshot.
  function uploadKind(mediaType) {
    var type = String(mediaType || '').toLowerCase();
    if (type.indexOf('image/') === 0) return 'image';
    if (type === 'application/pdf') return 'pdf';
    return 'text';
  }

  function artifactMark(item) {
    if (item.kind === 'image') return 'IMG';
    if (item.kind === 'pdf') return 'PDF';
    if (item.kind === 'text') return 'TXT';
    return 'MD';
  }

  function conversationPath(suffix) {
    return '/v2/api/chat/conversations/' + conversationId + suffix;
  }

  function artifactPreviewUrl(item) {
    var id = encodeURIComponent(item.id);
    return isUpload(item)
      ? conversationPath('/attachments/' + id + '/preview')
      : conversationPath('/documents/' + id);
  }

  function artifactDownloadUrl(item) {
    var id = encodeURIComponent(item.id);
    return isUpload(item)
      ? conversationPath('/attachments/' + id)
      : conversationPath('/documents/' + id + '/download');
  }

  function formatDocumentSize(sizeBytes) {
    var size = Number(sizeBytes) || 0;
    if (size < 1024) return size + ' bytes';
    return (size / 1024).toFixed(size < 10240 ? 1 : 0) + ' KB';
  }

  // A document appears the moment the agent asks for one and fills in as it is
  // written, so every label has to read sensibly for a file that does not exist
  // yet — and say so plainly when a write ended early.
  function documentIsWriting(item) {
    return (item || {}).status === 'streaming';
  }

  function documentMeta(item) {
    var name = item.filename || 'document.md';
    if (isUpload(item)) return name + ' · ' + formatDocumentSize(item.size_bytes);
    if (item.status === 'streaming') return name + ' · Writing…';
    if (item.status === 'failed') return name + ' · Write failed';
    var size = formatDocumentSize(item.size_bytes);
    if (item.status === 'truncated') return name + ' · ' + size + ' · Truncated';
    if (item.status === 'stopped') return name + ' · ' + size + ' · Stopped';
    return name + ' · ' + size;
  }

  // Downloading half a file is not useful, and the endpoint refuses it anyway.
  function documentIsDownloadable(item) {
    return !documentIsWriting(item) && item.status !== 'failed';
  }

  function dressDocumentCard(card, item) {
    var meta = card.querySelector('.chat-document-info span');
    if (meta) meta.textContent = documentMeta(item);
    card.dataset.documentStatus = item.status || 'complete';
    var download = card.querySelector('a[download]');
    if (download) download.hidden = !documentIsDownloadable(item);
  }

  function renderDocumentCard(item) {
    if (!item || !item.id) return true;
    var existing = document.querySelector('.chat-document[data-document-id="' + item.id + '"]');
    if (existing) {
      dressDocumentCard(existing, item);
      return true;
    }
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
    dressDocumentCard(card, item);
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
    var info = document.createElement('span');
    info.className = 'chat-dock-item-info';
    var title = document.createElement('strong');
    title.textContent = item.title || 'Markdown document';
    var meta = document.createElement('span');
    meta.className = 'p-meta';
    info.append(title, meta);
    button.append(mark, info);
    if (isUpload(item)) {
      var tag = document.createElement('span');
      tag.className = 'chat-dock-item-tag';
      tag.textContent = 'Upload';
      button.appendChild(tag);
    }
    dressDockItem(button, item);
    return button;
  }

  function dressDockItem(button, item) {
    var meta = button.querySelector('.p-meta');
    if (meta) meta.textContent = documentMeta(item);
    var mark = button.querySelector('.chat-dock-item-mark');
    if (mark) mark.textContent = artifactMark(item);
    button.dataset.documentStatus = item.status || 'complete';
    button.dataset.documentOrigin = item.origin || 'created';
    button.dataset.documentKind = item.kind || 'markdown';
  }

  // The element that asked for the preview can say what it points at, which is
  // how an uploaded file opens correctly before any snapshot has landed.
  function triggerItem(documentId, trigger) {
    if (!trigger || !trigger.dataset || !trigger.dataset.documentOrigin) return null;
    return {
      id: documentId,
      origin: trigger.dataset.documentOrigin,
      kind: trigger.dataset.documentKind || 'markdown',
      status: trigger.dataset.documentStatus || 'ready',
      title: ''
    };
  }

  // Falls back to the row itself for documents rendered by the server, which the
  // panel has no entry for until the first snapshot lands.
  function dockRowItem(documentId) {
    var row = dockList && dockList.querySelector('[data-dock-open][data-document-id="' + documentId + '"]');
    if (!row) return null;
    var title = row.querySelector('strong');
    return {
      id: documentId,
      origin: row.dataset.documentOrigin || 'created',
      kind: row.dataset.documentKind || 'markdown',
      status: row.dataset.documentStatus || 'complete',
      title: title ? title.textContent : ''
    };
  }

  // Look-up for what the panel knows about a document, so the preview can tell
  // a file being written from a finished one without another request.
  function dockDocument(documentId) {
    for (var index = 0; index < dockDocuments.length; index += 1) {
      if (dockDocuments[index].id === documentId) return dockDocuments[index];
    }
    return null;
  }

  // A file the agent overwrote stops being a file. The row and its inline card
  // go together: leaving either behind would offer the reader a download that
  // now 410s, and a name that resolves to something else.
  function dropDocument(documentId) {
    if (!documentId) return;
    dockDocuments = dockDocuments.filter(function (item) { return item.id !== documentId; });
    delete streams[documentId];
    var row = dockList && dockList.querySelector('[data-dock-open][data-document-id="' + documentId + '"]');
    if (row) row.remove();
    var card = document.querySelector('.chat-document[data-document-id="' + documentId + '"]');
    if (card) {
      var group = card.parentElement;
      card.remove();
      if (group && !group.querySelector('.chat-document')) group.remove();
    }
    // A reader looking at the retired file is moved back to the shelf rather
    // than left on a preview the server will no longer serve.
    if (dockPreviewing === documentId) showDockList();
    syncDockCount();
  }

  // The snapshot is the backstop for a missed supersede: anything the server no
  // longer lists is no longer in the conversation.
  function pruneDockList(items) {
    if (!dockList) return;
    var live = {};
    (items || []).forEach(function (item) { if (item && item.id) live[item.id] = true; });
    Array.prototype.slice.call(dockList.querySelectorAll('[data-dock-open]')).forEach(function (row) {
      if (!live[row.dataset.documentId]) dropDocument(row.dataset.documentId);
    });
  }

  function syncDockCount() {
    if (!dockList) return;
    var count = dockList.querySelectorAll('[data-dock-open]').length;
    if (dockCount) dockCount.textContent = count;
    if (dockEmpty) dockEmpty.hidden = count > 0;
    if (dockToggle) dockToggle.hidden = count === 0;
    applyDockScope();
  }

  // Appends only what is new, so the list never flickers and the row a reader
  // is pointing at cannot move out from under them mid-turn. Rows already there
  // are updated in place \u2014 a document's own row is how it reports its progress.
  function syncDockList(items) {
    if (!dockList) return;
    (items || []).forEach(function (item) {
      if (!item || !item.id) return;
      var row = dockList.querySelector('[data-dock-open][data-document-id="' + item.id + '"]');
      if (row) {
        // Rows rendered by the server arrive without an entry here, so the
        // snapshot pass is also what teaches the panel their status.
        var known = dockDocument(item.id);
        if (known) Object.assign(known, item);
        else dockDocuments.push(Object.assign({}, item));
        dressDockItem(row, known || item);
        return;
      }
      dockList.insertBefore(dockItem(item), dockEmpty);
      dockDocuments.push(Object.assign({}, item));
    });
    // The button is the only route to the panel, so it appears with the first
    // document rather than sitting there empty for the whole conversation.
    syncDockCount();
  }

  // ── Origin filter ─────────────────────────────────────
  // Written files and uploads share one shelf, which is right for reading and
  // wrong for looking something up. The filter is a view over the same rows —
  // nothing is removed, so a row updating mid-turn cannot fall out of the list.

  var DOCK_SCOPE_KEY = 'chat.dockScope';
  var dockScope = stored(DOCK_SCOPE_KEY) || 'all';
  var dockFilter = document.querySelector('[data-dock-filter]');
  var dockFilteredEmpty = document.querySelector('[data-dock-filtered-empty]');

  function applyDockScope() {
    if (!dockList) return;
    var shown = 0;
    var total = 0;
    Array.prototype.forEach.call(dockList.querySelectorAll('[data-dock-open]'), function (row) {
      var origin = row.dataset.documentOrigin || 'created';
      var matches = dockScope === 'all' || origin === dockScope;
      row.hidden = !matches;
      total += 1;
      if (matches) shown += 1;
    });
    if (dockFilter) {
      Array.prototype.forEach.call(dockFilter.querySelectorAll('[data-dock-scope]'), function (button) {
        button.setAttribute('aria-pressed', button.dataset.dockScope === dockScope ? 'true' : 'false');
      });
      dockFilter.hidden = total === 0;
    }
    // Two different silences: an empty panel, and a filter that hides everything.
    if (dockEmpty) dockEmpty.hidden = total > 0;
    if (dockFilteredEmpty) dockFilteredEmpty.hidden = total === 0 || shown > 0;
  }

  if (dockFilter) {
    dockFilter.addEventListener('click', function (event) {
      var button = event.target.closest('[data-dock-scope]');
      if (!button) return;
      dockScope = button.dataset.dockScope;
      store(DOCK_SCOPE_KEY, dockScope);
      applyDockScope();
    });
  }

  // The server renders the rows and the template defaults to "All", so a
  // remembered filter has to be applied before the first snapshot arrives.
  applyDockScope();

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
    // The filter belongs to the list, not to an open file.
    applyDockScope();
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

  // \u2500\u2500 Documents being written \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
  // A document is created empty and filled in by the model's next turn, which
  // arrives here as a stream of chunks. The live view is deliberately raw text
  // rather than rendered markdown: a half-written file flickers between block
  // types as it renders, and the server sends the rendered document the moment
  // the write finishes. Buffers are per document and dropped when it settles.

  function streamState(documentId) {
    if (!streams[documentId]) streams[documentId] = {text: '', sequence: 0, gap: false};
    return streams[documentId];
  }

  function streamPane() {
    if (!previewContent) return null;
    var pane = previewContent.querySelector('[data-preview-stream]');
    if (!pane) {
      previewContent.textContent = '';
      pane = document.createElement('pre');
      pane.className = 'chat-dock-stream';
      pane.dataset.previewStream = '';
      previewContent.appendChild(pane);
    }
    return pane;
  }

  function paintStream(documentId) {
    var pane = streamPane();
    if (!pane) return;
    var state = streamState(documentId);
    pane.textContent = state.text;
    var note = previewContent.querySelector('[data-preview-stream-note]');
    // Say so rather than presenting a text with a hole in it as the document.
    if (state.gap && !note) {
      note = document.createElement('p');
      note.className = 'p-meta';
      note.dataset.previewStreamNote = '';
      note.textContent = 'Joined this file mid-write \u2014 the complete document appears when it finishes.';
      previewContent.appendChild(note);
    } else if (!state.gap && note) {
      note.remove();
    }
  }

  function paneAtBottom() {
    if (!previewBody) return true;
    return previewBody.scrollHeight - previewBody.scrollTop - previewBody.clientHeight < 64;
  }

  function appendStreamChunk(data) {
    var documentId = data.document_id;
    if (!documentId) return;
    var state = streamState(documentId);
    var sequence = Number(data.sequence) || 0;
    // Chunks are numbered, so a dropped or reordered notification is detectable
    // instead of silently corrupting the text on screen.
    if (sequence !== state.sequence + 1) state.gap = true;
    state.sequence = sequence || state.sequence + 1;
    state.text += data.chunk || '';
    var known = dockDocument(documentId) || {id: documentId};
    known.size_bytes = data.size_bytes;
    known.status = 'streaming';
    var row = dockList && dockList.querySelector('[data-dock-open][data-document-id="' + documentId + '"]');
    if (row) dressDockItem(row, known);
    var card = document.querySelector('.chat-document[data-document-id="' + documentId + '"]');
    if (card) dressDocumentCard(card, known);
    if (dockPreviewing !== documentId) return;
    var follow = paneAtBottom();
    if (previewFilename) previewFilename.textContent = documentMeta(known);
    paintStream(documentId);
    // Follow the writing only for a reader who is already at the end of it.
    if (follow && previewBody) previewBody.scrollTop = previewBody.scrollHeight;
  }

  function settleDocumentPreview(data) {
    var item = {
      id: data.id,
      turn_id: data.turn_id,
      assistant_message_id: data.assistant_message_id,
      title: data.title,
      filename: data.filename,
      size_bytes: data.size_bytes,
      status: data.status
    };
    syncDocuments([item]);
    delete streams[item.id];
    if (dockPreviewing !== item.id) return;
    // The finished document is fetched rather than pushed: rendered markdown can
    // run to a hundred kilobytes, which does not belong in a notification. The
    // streamed text stays on screen until the rendered file replaces it.
    openDocument(item.id, dockReturnFocus);
  }

  // An uploaded image is the one artifact with no text form, so it is shown
  // rather than rendered. Everything else — written markdown, an uploaded .md, a
  // PDF's extracted text, a source file in a code block — arrives as HTML the
  // server rendered, so there is still exactly one markdown implementation.
  function renderPreviewBody(data) {
    previewContent.textContent = '';
    if (data.kind === 'image' && data.content_url) {
      var figure = document.createElement('figure');
      figure.className = 'chat-dock-image';
      var image = document.createElement('img');
      image.src = data.content_url;
      image.alt = data.filename || 'Uploaded image';
      image.loading = 'lazy';
      figure.appendChild(image);
      if (data.summarization_instruction) {
        var caption = document.createElement('figcaption');
        caption.className = 'p-meta';
        caption.textContent = 'Read with: ' + data.summarization_instruction;
        figure.appendChild(caption);
      }
      previewContent.appendChild(figure);
      return;
    }
    if (data.content_html) previewContent.innerHTML = data.content_html;
    else previewContent.textContent = 'This document has no readable contents.';
  }

  function openDocument(documentId, trigger) {
    if (!dock || !documentId) return;
    setDock(true);
    hideQuote();
    dockPreviewing = documentId;
    if (dockList) dockList.hidden = true;
    if (dockPreview) dockPreview.hidden = false;
    if (dockBack) dockBack.hidden = false;
    if (dockFilter) dockFilter.hidden = true;
    dockReturnFocus = trigger || null;
    var known = dockDocument(documentId) || dockRowItem(documentId) || triggerItem(documentId, trigger);
    if (dockTitle) dockTitle.textContent = (known && known.title) || 'Loading\u2026';
    if (previewFilename) previewFilename.textContent = known ? documentMeta(known) : '';
    if (previewDownload) {
      previewDownload.removeAttribute('href');
      previewDownload.hidden = Boolean(known) && !documentIsDownloadable(known);
      previewDownload.textContent = known && isUpload(known) ? 'Download file' : 'Download .md';
    }
    var url = artifactPreviewUrl(known || {id: documentId});
    if (known && documentIsWriting(known)) {
      paintStream(documentId);
      if (previewBody) previewBody.scrollTop = previewBody.scrollHeight;
      // Nothing buffered means this client joined the write late \u2014 a reload, or
      // a second tab. Ask the server what has been written so far.
      if (!streamState(documentId).sequence && !streamState(documentId).text) {
        api(url).then(function (data) {
          var state = streamState(documentId);
          if (dockPreviewing !== documentId || data.content_text == null) return;
          if (state.sequence) {
            // Deltas overtook the seed, so the prefix is unaccounted for.
            state.gap = true;
          } else {
            state.text = data.content_text;
          }
          paintStream(documentId);
          if (previewBody) previewBody.scrollTop = previewBody.scrollHeight;
        }).catch(function () {});
      }
      return;
    }
    // Text already on screen \u2014 the stream that just finished \u2014 stands in for the
    // loading state, so a settling document does not blink through empty.
    if (previewContent && !previewContent.querySelector('[data-preview-stream]')) {
      previewContent.textContent = 'Loading document\u2026';
    }
    api(url).then(function (data) {
      // A second click while this one was in flight wins; drop the stale reply.
      if (dockPreviewing !== documentId) return;
      if (dockTitle) dockTitle.textContent = data.title;
      if (previewFilename) previewFilename.textContent = documentMeta(data);
      if (previewContent) renderPreviewBody(data);
      if (previewDownload) {
        previewDownload.hidden = !documentIsDownloadable(data);
        previewDownload.href = artifactDownloadUrl(data);
        previewDownload.download = data.filename;
        previewDownload.textContent = isUpload(data) ? 'Download file' : 'Download .md';
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
    dockFollowStream = false;
    setDock(false);
    if (dockToggle) dockToggle.focus();
  });

  if (dockBack) dockBack.addEventListener('click', function () {
    dockFollowStream = false;
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
      // A sent file opens in the previewer rather than downloading: it is an
      // artifact of the conversation now, and the previewer is where one is read
      // (with the download a button away).
      var chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chat-sent-attachment';
      chip.dataset.openDocument = '';
      chip.dataset.documentId = item.id;
      chip.dataset.documentOrigin = 'upload';
      chip.dataset.documentKind = item.kind || uploadKind(item.media_type);
      chip.appendChild(fileGlyph());
      chip.appendChild(document.createTextNode(item.name));
      box.appendChild(chip);
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

  function activeAssistantMessages(snapshot) {
    var visible = {};
    (snapshot.messages || []).forEach(function (message) {
      if (message.role === 'assistant' && message.is_active) visible[message.id] = true;
    });
    return visible;
  }

  function visibleDocuments(snapshot) {
    var visible = activeAssistantMessages(snapshot);
    return (snapshot.documents || []).filter(function (item) {
      return !item.assistant_message_id || visible[item.assistant_message_id];
    });
  }

  // Regenerating a turn supersedes the response it replaces, and the documents
  // that response wrote go with it. An upload belongs to the reader's own
  // message, so nothing can supersede it and it always stays on the shelf.
  function visibleArtifacts(snapshot) {
    var visible = activeAssistantMessages(snapshot);
    return (snapshot.artifacts || []).filter(function (item) {
      return item.origin === 'upload' || !item.assistant_message_id || visible[item.assistant_message_id];
    });
  }

  // A Quick chat's thread boundaries, keyed by the message sequence each one is
  // drawn above. Every stored boundary gets a divider — the opening thread has
  // no row at all — which is the same rule the page template applies. A
  // boundary dropped here is swept out of the stream by the reconcile below,
  // live divider included.
  function threadBreaks(snapshot) {
    var breaks = {};
    (snapshot.threads || []).forEach(function (boundary) {
      breaks[boundary.start_sequence] = boundary;
    });
    return breaks;
  }

  function threadBreakElement(boundary, existing) {
    var divider = existing;
    if (!divider) {
      divider = document.createElement('div');
      divider.className = 'chat-thread-break';
      divider.dataset.threadBreak = '';
      divider.setAttribute('role', 'separator');
      var label = document.createElement('span');
      label.className = 'chat-thread-break-label';
      divider.appendChild(label);
    }
    divider.dataset.threadId = boundary.id;
    divider.dataset.startSequence = boundary.start_sequence;
    divider.querySelector('.chat-thread-break-label').textContent =
      boundary.title || 'New thread';
    return divider;
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
    var keptBreaks = [];
    var adopted = [];
    var breaks = threadBreaks(snapshot);
    messages.filter(function (message) {
      return message.role !== 'assistant' || message.is_active;
    }).forEach(function (message) {
      var boundary = breaks[message.sequence];
      if (boundary) {
        var divider = threadBreakElement(
          boundary,
          thread.querySelector('[data-thread-break][data-thread-id="' + boundary.id + '"]')
        );
        thread.insertBefore(divider, statusEl || null);
        keptBreaks.push(divider);
      }
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
    // Dividers need their own sweep: a `.chat-thread-break` is not a
    // `.chat-message`, so the sweep above would leave a boundary that has moved
    // or been withdrawn sitting in the stream for the rest of the session.
    Array.prototype.forEach.call(thread.querySelectorAll('[data-thread-break]'), function (divider) {
      if (keptBreaks.indexOf(divider) === -1) divider.remove();
    });
  }

  // Which user article the live divider goes above. The turn is the precise
  // answer, but two common cases have no user article carrying it: the sender's
  // own optimistic bubble is only named once the POST returns, and a regenerate
  // runs under a turn id the user message never had. The line always belongs in
  // front of the newest message in the stream, so fall back to that rather than
  // dropping the divider until the turn finishes.
  function liveThreadBreakAnchor(turnId) {
    var named = turnId
      ? thread.querySelector('.chat-message-user[data-turn-id="' + turnId + '"]')
      : null;
    if (named) return named;
    var userArticles = thread.querySelectorAll('.chat-message-user');
    return userArticles.length ? userArticles[userArticles.length - 1] : null;
  }

  // The agent drew a line mid-turn. Put the divider above the message being
  // answered right away instead of waiting for the turn to finish, and build it
  // with the reconcile's own builder so the element the browser shows now is the
  // element the reconcile adopts later — matched on data-thread-id.
  function insertLiveThreadBreak(data) {
    if (mode !== 'chat' || !thread || !data.thread_id) return;
    if (thread.querySelector('[data-thread-break][data-thread-id="' + data.thread_id + '"]')) return;
    var anchor = liveThreadBreakAnchor(data.turn_id);
    if (!anchor) return;
    thread.insertBefore(threadBreakElement({
      id: data.thread_id,
      start_sequence: data.start_sequence,
      title: data.title
    }, null), anchor);
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
      // These values were just set programmatically, which fires no change
      // event — snapshot them or the first conversation would be created with
      // whatever the server-rendered defaults happened to be.
      captureStartSettings();
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

  // The start-settings selects live inside the empty-state block, and sending
  // the first message removes that block before the conversation is created —
  // reading them at POST time found a detached DOM and threw, taking the whole
  // send path down with it. They are snapshotted while they exist instead, so
  // starting a conversation no longer depends on that ordering.
  var startSettings = {
    intelligence_mode: null,
    model_key: null,
    reasoning_level: '',
  };

  function captureStartSettings() {
    var intelligence = document.querySelector('[data-chat-new-intelligence]');
    var model = document.querySelector('[data-chat-new-model]');
    var reasoning = document.querySelector('[data-chat-new-reasoning]');
    if (intelligence) startSettings.intelligence_mode = intelligence.value;
    if (model) startSettings.model_key = model.value;
    if (reasoning) startSettings.reasoning_level = reasoning.value || '';
    return startSettings;
  }

  captureStartSettings();

  document.addEventListener('change', function (event) {
    if (!event.target.closest) return;
    if (event.target.closest('[data-chat-new-intelligence], [data-chat-new-model], [data-chat-new-reasoning]')) {
      captureStartSettings();
    }
  });

  function createConversation() {
    if (conversationId) return Promise.resolve({id: conversationId});
    if (conversationPromise) return conversationPromise;
    // Re-reads the selects when they are still on the page, and otherwise uses
    // the last values they held.
    var chosen = captureStartSettings();
    conversationPromise = api('/v2/api/chat/conversations', json('POST', {
      intelligence_mode: chosen.intelligence_mode,
      model_key: chosen.model_key,
      reasoning_level: chosen.reasoning_level || null,
    })).then(function (data) {
      conversationId = data.id;
      shell.dataset.conversationId = data.id;
      shell.dataset.modelKey = chosen.model_key;
      shell.dataset.reasoningLevel = chosen.reasoning_level || '';
      history.replaceState({}, '', data.url);
      activateConversationControls(chosen.model_key, chosen.reasoning_level || '');
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
    // A synchronous throw in the submit path used to escape as an uncaught
    // error: the composer went dead and the reader was told nothing. Route it
    // into the same failure handling a rejected request gets.
    var pending;
    try {
      pending = mode === 'chat' ? submitChat(text) : submitResources(text);
    } catch (error) {
      pending = Promise.reject(error);
    }
    pending.then(function (result) {
      activeTurn = result.turn_id || null;
      // A new turn re-earns the right to move the panel to what it writes.
      dockFollowStream = true;
      if (mode === 'resources') resourceRunning = true;
      assistant.dataset.pendingTurn = activeTurn || '';
      assistant.dataset.turnId = activeTurn || '';
      // Name the reader's own bubble too. It went in before the turn existed, and
      // a mid-turn divider anchors on the turn it belongs to.
      if (activeTurn) user.dataset.turnId = activeTurn;
      flushPendingDocuments();
      updateRootActivity({
        turn_id: activeTurn,
        status: ['disconnected', 'suspended', 'reconnecting'].indexOf(notificationStreamStatus) !== -1 ? 'Connection lost; reconnecting…' : 'Working…'
      });
      setBusy(true);
      // Sending is what turns a staged upload into part of the conversation, so
      // that is when it joins the panel — not when the turn finishes.
      syncDockList(pendingAttachments.map(function (item) {
        return {
          id: item.id,
          origin: 'upload',
          kind: uploadKind(item.media_type),
          title: item.name,
          filename: item.name,
          size_bytes: item.size_bytes,
          status: 'ready',
          turn_id: activeTurn
        };
      }));
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
      dockFollowStream = true;
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
    if (type === 'chat_document_created') {
      syncDocuments([data]);
      // Switch the panel to the file as the agent starts writing it.
      if (documentIsWriting(data) && dockFollowStream) openDocument(data.id, null);
    }
    if (type === 'chat_document_delta') appendStreamChunk(data);
    // An edit settles the same way a write does: the row takes the new size and
    // an open preview refetches the rendered file.
    if (type === 'chat_document_written' || type === 'chat_document_edited') settleDocumentPreview(data);
    if (type === 'chat_document_superseded') dropDocument(data.id);
    if (type === 'chat_output_delta') {
      var wasAtBottom = atBottom();
      var pending = thread.querySelector('[data-pending-turn="' + data.turn_id + '"] .chat-content') || thread.querySelector('[data-turn-id="' + data.turn_id + '"] .chat-content');
      if (pending) pending.innerHTML = data.content_html || '';
      updateRootActivity({turn_id: data.turn_id, status: 'Writing response…'});
      stick(wasAtBottom);
    }
    if (type === 'chat_title_changed' && data.title) applyTitle(data.conversation_id, data.title);
    if (type === 'chat_thread_started') insertLiveThreadBreak(data);
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
      // The agent may have named the conversation while the notification that
      // said so was missed; the snapshot is the one that always arrives.
      if (snapshot.title) applyTitle(snapshot.id, snapshot.title);
      syncThread(snapshot);
      if (active) {
        activeTurn = active.id;
        setStatus(active.status === 'stopping' ? 'Stopping…' : 'Working…');
        var target = thread.querySelector('[data-pending-turn="' + active.id + '"] .chat-content') || thread.querySelector('[data-turn-id="' + active.id + '"] .chat-content');
        if (target && active.partial) target.innerHTML = active.partial_html || '';
        setBusy(true);
      } else if (activeTurn) {
        finishTurn();
      } else {
        setBusy(false);
      }
      syncPendingAttachments(snapshot.pending_attachments);
      syncDocuments(visibleDocuments(snapshot));
      syncDockList(visibleArtifacts(snapshot));
      pruneDockList(visibleArtifacts(snapshot));
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
