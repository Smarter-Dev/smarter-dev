/**
 * /ai/answer/{id} — Owner-only follow-up form.
 *
 * Same shape as the initial ASK on /resources: clicking ASK never blocks.
 * We:
 *   1. preventDefault, kick off the POST.
 *   2. Immediately morph the textarea text into a fresh user-turn bubble
 *      (FLIP animated from the textarea's bounding rect) and append an
 *      assistant placeholder with a SMARTER DEV header + empty tool stream.
 *      `answer-time.js` already owns the sk:notification handlers, so its
 *      `revealAnswer` will collapse the stream and fade the answer in when
 *      `agent_run_complete` arrives.
 *   3. On 201, stamp the user-turn id, decrement the counter; when the
 *      counter hits 0, fade the form out and replace with the same
 *      "used all N follow-ups" CTA the server renders.
 */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function escapeAttr(s) { return escapeHtml(s); }

  function nowIso() { return new Date().toISOString(); }

  function clockNow() {
    return new Date().toLocaleTimeString('en-US', {
      hour: 'numeric', minute: '2-digit', hour12: true,
    }).replace(' ', '').toLowerCase();
  }

  function paragraphsFromText(text) {
    return text
      .split(/\n{2,}/)
      .map(function (p) {
        return '<p>' + escapeHtml(p).replace(/\n/g, '<br>') + '</p>';
      })
      .join('');
  }

  function buildUserTurn(question, askerName) {
    var label = (askerName || 'YOU').toUpperCase();
    var art = document.createElement('article');
    art.className = 'ai-turn ai-turn-user';
    art.innerHTML =
      '<header class="ai-turn-head">' +
        '<p class="ai-turn-role">' +
          '<span class="ai-turn-marker" aria-hidden="true"></span>' +
          '<span class="ai-turn-role-label">' + escapeHtml(label) + '</span>' +
        '</p>' +
        '<time class="ai-turn-time" datetime="' + escapeAttr(nowIso()) + '">' + clockNow() + '</time>' +
      '</header>' +
      '<div class="ai-turn-body ai-turn-question markdown-body" data-ai-user-body>' +
        paragraphsFromText(question) +
      '</div>';
    return art;
  }

  function buildAssistantPlaceholder() {
    var art = document.createElement('article');
    art.className = 'ai-turn ai-turn-assistant';
    art.setAttribute('data-ai-assistant-turn', '');
    art.setAttribute('data-status', 'running');
    art.innerHTML =
      '<header class="ai-turn-head">' +
        '<p class="ai-turn-role">' +
          '<span class="ai-turn-marker" aria-hidden="true"></span>' +
          '<span class="ai-turn-role-label">SMARTER DEV</span>' +
          '<span class="ai-turn-model" aria-hidden="true">/+/</span>' +
          '<span class="ai-turn-model-name">gemini 3 flash</span>' +
        '</p>' +
        '<time class="ai-turn-time" datetime="' + escapeAttr(nowIso()) + '">' + clockNow() + '</time>' +
      '</header>' +
      '<div class="ai-turn-body ai-turn-answer" data-ai-assistant-body>' +
        '<div class="ai-tools-stream" data-ai-tools-stream></div>' +
        '<div class="ai-answer-prose" data-ai-answer-prose hidden></div>' +
      '</div>';
    return art;
  }

  function flipUserBody(targetBody, sourceTextarea) {
    if (!sourceTextarea || !targetBody) return;
    var src = sourceTextarea.getBoundingClientRect();
    var dst = targetBody.getBoundingClientRect();
    if (src.width === 0 || dst.width === 0) return;
    var dx = src.left - dst.left;
    var dy = src.top - dst.top;
    var sx = src.width / dst.width;
    targetBody.style.transformOrigin = '0 0';
    targetBody.style.transition = 'none';
    targetBody.style.transform = 'translate(' + dx + 'px, ' + dy + 'px) scale(' + sx + ')';
    targetBody.style.opacity = '.7';
    void targetBody.offsetWidth;
    targetBody.style.transition = 'transform .55s cubic-bezier(.4,.0,.2,1), opacity .35s ease';
    targetBody.style.transform = 'translate(0, 0) scale(1)';
    targetBody.style.opacity = '1';
    setTimeout(function () {
      targetBody.style.transition = '';
      targetBody.style.transform = '';
      targetBody.style.transformOrigin = '';
    }, 600);
  }

  function fadeOutAndRemove(el, after) {
    if (!el) { if (after) after(); return; }
    el.style.transition = 'opacity .3s ease, transform .3s ease';
    el.style.opacity = '0';
    el.style.transform = 'translateY(-6px)';
    setTimeout(function () {
      if (el.parentNode) el.parentNode.removeChild(el);
      if (after) after();
    }, 320);
  }

  function showExhausted(form, max) {
    var thread = form.parentElement;
    if (!thread) return;
    var msg = document.createElement('p');
    msg.className = 'ai-followup-closed';
    msg.style.opacity = '0';
    msg.innerHTML =
      "You've used all " + (max || 'your') + " follow-ups on this answer. " +
      '<a href="/resources">Start a fresh question on /resources →</a>';
    fadeOutAndRemove(form, function () {
      thread.appendChild(msg);
      requestAnimationFrame(function () {
        msg.style.transition = 'opacity .3s ease';
        msg.style.opacity = '1';
      });
    });
  }

  function updateCounter(form, remaining, max) {
    var counter = form.querySelector('[data-ai-followup-counter]');
    if (!counter) return;
    counter.setAttribute('data-remaining', String(remaining));
    if (max != null) counter.setAttribute('data-max', String(max));
    counter.textContent = remaining + '/' + (max != null ? max : counter.getAttribute('data-max')) + ' follow-ups left';
  }

  function init() {
    var form = $('ai-followup-form');
    if (!form) return;

    var thread = form.parentElement;
    var conversationId = thread && thread.getAttribute('data-conversation-id');
    if (!conversationId) return;
    var askerName = thread.getAttribute('data-asker-name') || '';

    var textarea = $('ai-followup-input');
    var submitBtn = form.querySelector('.ai-followup-btn');
    var hint = form.querySelector('[data-error]');
    var submitting = false;

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
      if (submitting) return;
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

      submitting = true;
      submitBtn && (submitBtn.disabled = true);
      setHint(null, '');

      // Morph the new turn pair into the thread right above the form,
      // BEFORE the fetch resolves — same UX as the initial ask.
      var userTurn = buildUserTurn(question, askerName);
      var assistantTurn = buildAssistantPlaceholder();
      thread.insertBefore(userTurn, form);
      thread.insertBefore(assistantTurn, form);

      // FLIP the question text from the textarea into the user-turn slot.
      requestAnimationFrame(function () {
        var body = userTurn.querySelector('[data-ai-user-body]');
        flipUserBody(body, textarea);
      });

      // Reset the textarea so the user can keep typing the next follow-up
      // (it stays in place until they hit the cap).
      if (textarea) {
        textarea.value = '';
        textarea.style.height = '';
      }
      assistantTurn.scrollIntoView({ behavior: 'smooth', block: 'center' });

      fetch('/v2/api/agent/conversations/' + encodeURIComponent(conversationId) + '/reply', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ question: question }),
        credentials: 'same-origin',
      })
        .then(function (res) {
          return res.json().then(function (body) {
            return { ok: res.ok, status: res.status, body: body };
          }).catch(function () {
            return { ok: res.ok, status: res.status, body: null };
          });
        })
        .then(function (resp) {
          if (resp.ok && resp.body && resp.body.user_message) {
            var uid = resp.body.user_message.id;
            if (uid) userTurn.id = 'turn-' + uid;
            submitting = false;
            submitBtn && (submitBtn.disabled = false);

            var remaining = resp.body.followups_remaining;
            var max = resp.body.max_followups;
            if (typeof remaining === 'number') {
              if (remaining <= 0) {
                showExhausted(form, max);
              } else {
                updateCounter(form, remaining, max);
              }
            }
            return;
          }
          submitting = false;
          submitBtn && (submitBtn.disabled = false);
          var detail = (resp.body && (resp.body.detail || resp.body.message)) || '';
          if (resp.status === 401) {
            window.location.assign('/auth/login?next=' + encodeURIComponent(window.location.pathname));
            return;
          }
          // Surface the error inline; the morph stays so the user keeps context.
          showError(assistantTurn, detail, resp.status);
        })
        .catch(function () {
          submitting = false;
          submitBtn && (submitBtn.disabled = false);
          showError(assistantTurn, 'Network error. Check your connection and retry.', 0);
        });
    });

    function showError(assistant, detail, status) {
      var stream = assistant.querySelector('[data-ai-tools-stream]');
      if (stream) stream.remove();
      var prose = assistant.querySelector('[data-ai-answer-prose]');
      var msg = detail || 'Agent failed to respond.';
      if (status === 429) msg = detail || 'Slow down — too many requests.';
      if (status === 403) msg = detail || 'Only the original asker can reply here.';
      if (prose) {
        prose.hidden = false;
        prose.innerHTML =
          '<p class="ai-live-error">' + escapeHtml(msg) + ' ' +
          '<a href="/resources">Try again on /resources →</a></p>';
      }
      assistant.setAttribute('data-status', 'error');
      assistant.removeAttribute('data-ai-assistant-turn');
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
