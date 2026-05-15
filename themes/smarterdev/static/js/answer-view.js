/**
 * /ai/answer/{id} — Owner-only follow-up form.
 *
 * Posts the follow-up question to the reply endpoint, appends the new
 * user/assistant turn pair to the thread without reloading, then clears the
 * textarea. Citations are rendered inline.
 */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function clockNow() {
    return new Date().toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  }

  function renderUserTurn(msg, askerName) {
    var label = (askerName || 'USER').toUpperCase();
    var art = document.createElement('article');
    art.className = 'ai-turn ai-turn-user';
    if (msg.id) art.id = 'turn-' + msg.id;
    art.innerHTML =
      '<header class="ai-turn-head">' +
        '<p class="ai-turn-role">' +
          '<span class="ai-turn-marker" aria-hidden="true"></span>' +
          '<span class="ai-turn-role-label">' + escapeHtml(label) + '</span>' +
        '</p>' +
        '<time class="ai-turn-time" datetime="' + new Date().toISOString() + '">' + clockNow() + '</time>' +
      '</header>' +
      '<div class="ai-turn-body ai-turn-question markdown-body">' +
      (msg.content_html || escapeHtml(msg.content || '')) +
      '</div>';
    return art;
  }

  function renderAssistantTurn(msg, answerUrl) {
    var body = msg.content_html || escapeHtml(msg.content || '');
    var hasBlocks = Array.isArray(msg.sdanswer_blocks) && msg.sdanswer_blocks.length > 0;
    var art = document.createElement('article');
    art.className = 'ai-turn ai-turn-assistant';
    if (msg.id) art.id = 'turn-' + msg.id;
    if (msg.id) art.setAttribute('data-turn-id', msg.id);
    if (hasBlocks) art.setAttribute('data-has-sdanswer', '1');
    var payloadScript = hasBlocks
      ? '<script type="application/json" class="sdanswer-payload">' +
        JSON.stringify(msg.sdanswer_blocks).replace(/</g, '\\u003c') +
        '<\/script>'
      : '';
    var shareUrl = (answerUrl || '') + (msg.id ? '#turn-' + msg.id : '');
    art.innerHTML =
      '<header class="ai-turn-head">' +
        '<p class="ai-turn-role">' +
          '<span class="ai-turn-marker" aria-hidden="true"></span>' +
          '<span class="ai-turn-role-label">SMARTER DEV</span>' +
          '<span class="ai-turn-model" aria-hidden="true">/+/</span>' +
          '<span class="ai-turn-model-name">gemini 3 flash</span>' +
        '</p>' +
        '<time class="ai-turn-time" datetime="' + new Date().toISOString() + '">' + clockNow() + '</time>' +
      '</header>' +
      '<div class="ai-turn-body ai-turn-answer markdown-body">' + body + '</div>' +
      payloadScript +
      '<footer class="ai-turn-actions">' +
        '<button type="button" class="ai-turn-action" data-ai-copy aria-label="Copy answer">' +
          '<span class="ai-turn-action-label" data-default>COPY</span>' +
          '<span class="ai-turn-action-label" data-copied hidden>COPIED ✓</span>' +
        '</button>' +
        '<button type="button" class="ai-turn-action" data-share-url="' + escapeHtml(shareUrl) + '" aria-label="Copy a link to this message">' +
          '<span class="ai-turn-action-label" data-default>SHARE</span>' +
          '<span class="ai-turn-action-label" data-copied hidden>COPIED ✓</span>' +
        '</button>' +
      '</footer>';
    return art;
  }

  function init() {
    var form = $('ai-followup-form');
    if (!form) return;

    var thread = form.parentElement;
    var conversationId = thread && thread.getAttribute('data-conversation-id');
    if (!conversationId) return;
    var askerName = thread.getAttribute('data-asker-name') || '';
    var answerUrl = thread.getAttribute('data-answer-url') ||
      (window.location.origin + window.location.pathname);

    var textarea = $('ai-followup-input');
    var submitBtn = form.querySelector('.ai-followup-btn');
    var hint = form.querySelector('[data-error]');

    function setHint(state, text) {
      if (!hint) return;
      if (state) hint.setAttribute('data-state', state);
      else hint.removeAttribute('data-state');
      hint.textContent = text || '';
      if (text) hint.removeAttribute('hidden');
      else hint.setAttribute('hidden', '');
    }

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      var question = (textarea && textarea.value || '').trim();
      if (!question) {
        setHint('error', 'Type a follow-up first.');
        textarea && textarea.focus();
        return;
      }
      if (question.length > 1000) {
        setHint('error', 'Keep it under 1000 characters.');
        return;
      }

      submitBtn && (submitBtn.disabled = true);
      setHint('loading', 'Thinking…');

      fetch('/v2/api/agent/conversations/' + encodeURIComponent(conversationId) + '/reply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ question: question }),
        credentials: 'same-origin',
      })
        .then(function (res) {
          return res.json().then(function (body) {
            return { ok: res.ok, status: res.status, body: body };
          });
        })
        .then(function (resp) {
          if (resp.ok && resp.body && resp.body.user_message && resp.body.assistant_message) {
            thread.insertBefore(renderUserTurn(resp.body.user_message, askerName), form);
            var assistantNode = renderAssistantTurn(resp.body.assistant_message, answerUrl);
            thread.insertBefore(assistantNode, form);
            if (window.AIAnswerTime && typeof window.AIAnswerTime.hydrate === 'function') {
              window.AIAnswerTime.hydrate(assistantNode);
            }
            if (window.SDAnswer && typeof window.SDAnswer.hydrate === 'function') {
              window.SDAnswer.hydrate(assistantNode);
            }
            textarea.value = '';
            setHint(null, '');
            submitBtn && (submitBtn.disabled = false);
            form.scrollIntoView({ behavior: 'smooth', block: 'start' });
            return;
          }
          var detail = (resp.body && (resp.body.detail || resp.body.message)) || '';
          if (resp.status === 401) {
            window.location.assign('/auth/login?next=' + encodeURIComponent(window.location.pathname));
            return;
          }
          if (resp.status === 403) {
            setHint('error', detail || 'Only the original asker can reply here.');
          } else if (resp.status === 429) {
            setHint('error', detail || 'Too many requests. Try again shortly.');
          } else if (resp.status === 503) {
            setHint('error', detail || 'Agent is unavailable. Try again later.');
          } else {
            setHint('error', detail || 'Something went wrong. Try again.');
          }
          submitBtn && (submitBtn.disabled = false);
        })
        .catch(function () {
          setHint('error', 'Network error. Check your connection and retry.');
          submitBtn && (submitBtn.disabled = false);
        });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
