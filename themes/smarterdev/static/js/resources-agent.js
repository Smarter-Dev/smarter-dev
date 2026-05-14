/**
 * /resources — Resource Agent ASK block.
 *
 * - Submits the question to /v2/api/resources/ask, then redirects to
 *   /ai/answer/{id} on success.
 * - OR-TRY cards prefill the textarea (no auto-submit).
 */
(function () {
  'use strict';

  function $(id) { return document.getElementById(id); }

  function init() {
    var form = $('rsa-form');
    if (!form) return;

    var textarea = $('rsa-input');
    var submitBtn = $('rsa-submit');
    var hint = $('rsa-hint');

    // Auto-grow: shrink to baseline (the CSS min-height handles the 3-line
    // floor), then expand to fit content. Runs on every input event and
    // after a prefill from the OR-TRY cards.
    function autoGrow() {
      if (!textarea) return;
      textarea.style.height = 'auto';
      textarea.style.height = textarea.scrollHeight + 'px';
    }
    if (textarea) textarea.addEventListener('input', autoGrow);

    document.querySelectorAll('[data-prefill]').forEach(function (card) {
      card.addEventListener('click', function () {
        if (!textarea) return;
        textarea.value = card.getAttribute('data-prefill') || '';
        autoGrow();
        textarea.focus();
        textarea.setSelectionRange(textarea.value.length, textarea.value.length);
        textarea.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
    });

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
        setHint('error', 'Type a question first.');
        textarea && textarea.focus();
        return;
      }
      if (question.length > 1000) {
        setHint('error', 'Keep it under 1000 characters.');
        return;
      }

      submitBtn && (submitBtn.disabled = true);
      setHint('loading', 'Thinking…');

      fetch('/v2/api/resources/ask', {
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
          if (resp.ok && resp.body && resp.body.url) {
            window.location.assign(resp.body.url);
            return;
          }
          var detail = (resp.body && (resp.body.detail || resp.body.message)) || '';
          if (resp.status === 401) {
            window.location.assign('/auth/login?next=/resources');
            return;
          }
          if (resp.status === 429) {
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
