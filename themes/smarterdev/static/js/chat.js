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
  var documentDialog = document.querySelector('[data-chat-document-dialog]');
  var documentDialogTrigger = null;
  var pendingDocuments = [];

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
    if (submitBtn) submitBtn.disabled = Boolean(locked || uploadCount);
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

  function bubble(role, text, turnId) {
    var article = document.createElement('article');
    article.className = 'chat-message chat-message-' + role;
    if (turnId) article.dataset.turnId = turnId;
    var label = document.createElement('div');
    label.className = 'chat-role';
    label.textContent = role === 'user' ? 'You' : 'Smarter Dev';
    var content = document.createElement('div');
    content.className = 'chat-content';
    content.textContent = text;
    article.appendChild(label);
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

  function closeDocumentDialog() {
    if (!documentDialog) return;
    documentDialog.hidden = true;
    document.body.classList.remove('chat-document-open');
    documentDialog.querySelector('[data-document-content]').textContent = '';
    if (documentDialogTrigger) documentDialogTrigger.focus();
    documentDialogTrigger = null;
  }

  function openDocumentDialog(documentId, trigger) {
    if (!documentDialog || !documentId) return;
    documentDialogTrigger = trigger || null;
    var title = documentDialog.querySelector('[data-document-title]');
    var filename = documentDialog.querySelector('[data-document-filename]');
    var content = documentDialog.querySelector('[data-document-content]');
    var download = documentDialog.querySelector('[data-document-download]');
    title.textContent = 'Loading…';
    filename.textContent = '';
    content.textContent = 'Loading document…';
    download.removeAttribute('href');
    documentDialog.hidden = false;
    document.body.classList.add('chat-document-open');
    documentDialog.querySelector('[data-close-document]:not(.chat-document-backdrop)').focus();
    api('/v2/api/chat/conversations/' + conversationId + '/documents/' + encodeURIComponent(documentId)).then(function (documentData) {
      title.textContent = documentData.title;
      filename.textContent = documentData.filename + ' · ' + formatDocumentSize(documentData.size_bytes);
      content.innerHTML = documentData.content_html;
      download.href = documentDownloadUrl(documentId);
      download.download = documentData.filename;
    }).catch(function (error) {
      title.textContent = 'Document unavailable';
      content.textContent = error.message;
    });
  }

  if (documentDialog) {
    documentDialog.addEventListener('click', function (event) {
      if (event.target.closest('[data-close-document]')) closeDocumentDialog();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !documentDialog.hidden) closeDocumentDialog();
    });
  }

  function createActivity(startedAt, label) {
    var activity = document.createElement('div');
    activity.className = 'chat-agent-activity';
    activity.dataset.rootActivity = '';
    activity.dataset.startedAt = startedAt || new Date().toISOString();
    var pulse = document.createElement('span');
    pulse.className = 'chat-activity-pulse';
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
      pulse.className = 'chat-activity-pulse';
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
  }

  var files = document.querySelector('[data-chat-files]');
  if (files) files.addEventListener('change', function () {
    var chosen = Array.prototype.slice.call(files.files || []);
    if (pendingAttachments.length + uploadCount + chosen.length > 5) {
      showError('A turn may include at most 5 attachments.');
      files.value = '';
      return;
    }
    uploadCount += chosen.length;
    setBusy(Boolean(activeTurn));
    var ready = conversationId ? Promise.resolve() : createConversation();
    ready.then(function () {
      chosen.forEach(function (file) {
        if (file.size > 10 * 1024 * 1024) {
          showError(file.name + ' exceeds 10 MB.');
          uploadCount -= 1;
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
          setBusy(Boolean(activeTurn));
        });
      });
    }).catch(function (error) {
      uploadCount -= chosen.length;
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

  if (form) form.addEventListener('submit', function (event) {
    event.preventDefault();
    showError('');
    if (uploadCount) return showError('Wait for attachments to finish uploading.');
    var text = (input.value || '').trim();
    if (!text) return showError('Type a message first.');
    if (text.length > 5000) return showError('Keep it under 5000 characters.');
    var user = bubble('user', text);
    var assistant = bubble('assistant', '');
    thread.insertBefore(user, statusEl);
    thread.insertBefore(assistant, statusEl);
    input.value = '';
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
      if (mode === 'resources') setStatus('Resource Agent is working…');
    }).catch(function (error) {
      assistant.querySelector('.chat-content').textContent = error.message;
      setStatus('');
      showError(error.message);
      setBusy(false);
    });
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
    var documentButton = event.target.closest('[data-open-document]');
    if (documentButton) {
      openDocumentDialog(documentButton.dataset.documentId, documentButton);
      return;
    }
    var button = event.target.closest('[data-regenerate]');
    if (!button) return;
    setBusy(true);
    api('/v2/api/chat/conversations/' + conversationId + '/turns/' + button.dataset.turnId + '/regenerate', json('POST', {})).then(function (data) {
      activeTurn = data.turn_id;
      var placeholder = bubble('assistant', '', data.turn_id);
      placeholder.dataset.pendingTurn = data.turn_id;
      button.closest('.chat-message').after(placeholder);
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
      location.reload();
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
    if (agentPanel) agentPanel.hidden = true;
    refreshUsage();
  }

  function handleTerminal(data, type) {
    var pending = thread.querySelector('[data-pending-turn="' + data.turn_id + '"] .chat-content');
    if (pending && data.content !== undefined) pending.textContent = data.content || '';
    if (type === 'chat_turn_error' || type === 'agent_run_error') {
      if (pending) pending.textContent = data.detail || 'The run failed.';
      showError(data.detail || 'The run failed.');
    }
    finishTurn();
    if (mode === 'chat' || type === 'agent_run_error') {
      // Server rendering consolidates placeholders, alternatives, restored
      // controls, and terminal errors after every durable terminal transition.
      window.setTimeout(function () { location.reload(); }, 50);
    }
  }

  function notification(event) {
    var envelope = event.detail || {};
    var payload = envelope.payload || envelope;
    var type = envelope.type || payload.type || event.type;
    var data = Object.assign({}, payload, {
      conversation_id: payload.conversation_id || envelope.conversation_id,
      turn_id: payload.turn_id || envelope.turn_id,
    });
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
      var pending = thread.querySelector('[data-pending-turn="' + data.turn_id + '"] .chat-content') || thread.querySelector('[data-turn-id="' + data.turn_id + '"] .chat-content');
      if (pending) pending.textContent = data.content || '';
      updateRootActivity({turn_id: data.turn_id, status: 'Writing response…'});
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
      document.querySelector('[data-conversation-percent]').textContent = metrics.four_hour_percent_conversation.toFixed(1);
      document.querySelector('[data-all-percent]').textContent = metrics.four_hour_percent_all_chat.toFixed(1);
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
      var active = snapshot.active_turn;
      if (active) {
        activeTurn = active.id;
        setStatus(active.status === 'stopping' ? 'Stopping…' : 'Working…');
        var target = thread.querySelector('[data-pending-turn="' + active.id + '"] .chat-content') || thread.querySelector('[data-turn-id="' + active.id + '"] .chat-content');
        if (target && active.partial) target.textContent = active.partial;
        setBusy(true);
      } else if (activeTurn) {
        window.location.reload();
      }
      syncPendingAttachments(snapshot.pending_attachments);
      syncDocuments(snapshot.documents);
      syncAgentActivity(snapshot);
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
    if (status === 'connected') {
      if (notificationNeedsReconcile) reconcile();
      notificationHasConnected = true;
      notificationNeedsReconcile = false;
    } else if (status === 'disconnected' || status === 'suspended' || status === 'reconnecting') {
      notificationNeedsReconcile = notificationHasConnected;
      if (activeTurn) updateRootActivity({turn_id: activeTurn, status: 'Connection lost; reconnecting…'});
    }
  });

  loadCatalog().then(function () {
    refreshUsage();
    if (conversationId) reconcile();
    if (activeTurn || resourceRunning) setBusy(true);
  });
})();
