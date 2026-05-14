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

  function renderUserTurn(msg) {
    var art = document.createElement('article');
    art.className = 'ai-turn ai-turn-user';
    art.innerHTML =
      '<p class="ai-turn-label">// asked</p>' +
      '<div class="ai-turn-body ai-turn-question markdown-body">' +
      (msg.content_html || escapeHtml(msg.content || '')) +
      '</div>';
    return art;
  }

  function renderCitations(citations) {
    if (!citations || !citations.length) return '';
    var cards = citations.map(function (c) {
      var meta = '';
      if (c.learning_type) meta += '<span class="ai-citation-type">' + escapeHtml(c.learning_type) + '</span>';
      if (c.byline) meta += '<span class="ai-citation-byline">' + escapeHtml(c.byline) + '</span>';
      var trackAttr = c.track_key ? ' data-track-key="' + escapeHtml(c.track_key) + '"' : '';
      return (
        '<article class="ai-citation-card">' +
        '<a class="ai-citation-link" href="' + escapeHtml(c.url) + '"' + trackAttr +
        ' target="_blank" rel="noopener" data-sk-no-spa>' +
        '<h3 class="ai-citation-title">' + escapeHtml(c.title) + '</h3>' +
        '</a>' +
        '<p class="ai-citation-meta">' + meta + '</p>' +
        (c.blurb ? '<p class="ai-citation-blurb">' + escapeHtml(c.blurb) + '</p>' : '') +
        '</article>'
      );
    }).join('');
    return (
      '<div class="ai-citations">' +
      '<p class="ai-citations-label">// sources</p>' +
      '<div class="ai-citation-grid">' + cards + '</div>' +
      '</div>'
    );
  }

  function renderAssistantTurn(msg) {
    var body = msg.content_html || escapeHtml(msg.content || '');
    var hasBlocks = Array.isArray(msg.sdanswer_blocks) && msg.sdanswer_blocks.length > 0;
    var art = document.createElement('article');
    art.className = 'ai-turn ai-turn-assistant';
    if (hasBlocks) art.setAttribute('data-has-sdanswer', '1');
    var payloadScript = hasBlocks
      ? '<script type="application/json" class="sdanswer-payload">' +
        JSON.stringify(msg.sdanswer_blocks).replace(/</g, '\\u003c') +
        '<\/script>'
      : '';
    var citationsHtml = hasBlocks ? '' : renderCitations(msg.citations || []);
    art.innerHTML =
      '<button type="button" class="ai-turn-copy" data-ai-copy aria-label="Copy answer as markdown">' +
        '<span class="ai-turn-copy-label" data-default>COPY</span>' +
        '<span class="ai-turn-copy-label" data-copied hidden>COPIED ✓</span>' +
      '</button>' +
      '<p class="ai-turn-label">// resource agent</p>' +
      '<div class="ai-turn-body ai-turn-answer markdown-body">' + body + '</div>' +
      payloadScript +
      citationsHtml;
    // Stash the raw markdown in a <template> so the copy button can read it
    // back verbatim without HTML escaping issues.
    var tpl = document.createElement('template');
    tpl.className = 'ai-turn-markdown';
    tpl.textContent = msg.content || '';
    var bodyEl = art.querySelector('.ai-turn-body');
    if (bodyEl && bodyEl.parentNode) {
      bodyEl.parentNode.insertBefore(tpl, bodyEl.nextSibling);
    } else {
      art.appendChild(tpl);
    }
    return art;
  }

  function init() {
    var form = $('ai-followup-form');
    if (!form) return;

    var thread = form.parentElement;
    var conversationId = thread && thread.getAttribute('data-conversation-id');
    if (!conversationId) return;

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
            thread.insertBefore(renderUserTurn(resp.body.user_message), form);
            var assistantNode = renderAssistantTurn(resp.body.assistant_message);
            thread.insertBefore(assistantNode, form);
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
