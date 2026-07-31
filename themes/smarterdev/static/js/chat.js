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
  var pollTimer = null;

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
    article.append(label, content);
    return article;
  }

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
    var assistant = bubble('assistant', 'Working…');
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
      setBusy(true);
      pendingAttachments = [];
      var box = document.querySelector('[data-attachments]');
      if (box) box.textContent = '';
      if (mode === 'resources') setStatus('Resource Agent is working…');
      startPolling();
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
    var button = event.target.closest('[data-regenerate]');
    if (!button) return;
    setBusy(true);
    api('/v2/api/chat/conversations/' + conversationId + '/turns/' + button.dataset.turnId + '/regenerate', json('POST', {})).then(function (data) {
      activeTurn = data.turn_id;
      var placeholder = bubble('assistant', 'Regenerating…', data.turn_id);
      placeholder.dataset.pendingTurn = data.turn_id;
      button.closest('.chat-message').after(placeholder);
      setStatus('Regenerating…');
      setBusy(true);
      startPolling();
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
    refreshUsage();
    stopPolling();
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
    if (type === 'chat_tool_event' || type === 'chat_run_state' || type === 'chat_subagent_state') {
      setStatus(data.status || 'Working…');
    }
    if (type === 'chat_output_delta') {
      var pending = thread.querySelector('[data-pending-turn="' + data.turn_id + '"] .chat-content') || thread.querySelector('[data-turn-id="' + data.turn_id + '"] .chat-content');
      if (pending) pending.textContent = data.content || '';
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
      var childBox = document.querySelector('[data-chat-subagents]');
      if (childBox && snapshot.subagents && snapshot.subagents.length) {
        childBox.hidden = false;
        childBox.textContent = snapshot.subagents.map(function (child) {
          return child.name + ': ' + child.status;
        }).join(' · ');
      }
    }).catch(function () {});
    refreshUsage();
  }

  function startPolling() {
    if (pollTimer || !conversationId) return;
    pollTimer = window.setInterval(reconcile, 1500);
  }

  function stopPolling() {
    if (pollTimer) window.clearInterval(pollTimer);
    pollTimer = null;
  }

  loadCatalog().then(function () {
    refreshUsage();
    if (mode === 'chat' && conversationId && !activeTurn) reconcile();
    if (activeTurn || resourceRunning) {
      setBusy(true);
      startPolling();
      reconcile();
    }
  });
})();
