/**
 * /resources — Resource Agent ASK block.
 *
 * Clicking ASK never blocks on the form. We:
 *   1. preventDefault, fire the POST.
 *   2. Immediately morph the /resources DOM into an /ai/answer/{id}-shaped
 *      thread: hero collapses to a placeholder with a blinking title caret,
 *      the textarea text animates (FLIP) into a chat user-turn, and an
 *      assistant-turn placeholder appears with the model badge and an empty
 *      tools-stream container.
 *   3. On the API ack, history.pushState to /ai/answer/{id} and stamp the
 *      conversation id onto .ai-thread so the global sk:notification
 *      listeners (in answer-time.js) can match incoming agent events:
 *         - agent_title_updated → typewrites the title into the caret slot
 *         - agent_tool_event → renders a tool chip
 *         - agent_run_complete → swaps the chip stream for the final markdown
 *         - agent_run_error → renders an error in the assistant slot
 *   4. popstate falls back to a reload — no attempt to reverse the morph.
 *
 * OR-TRY card prefill behavior is unchanged.
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

  function fade(el, opts) {
    if (!el) return;
    var dy = (opts && opts.dy) || 12;
    el.style.transition = 'opacity .35s ease, transform .35s ease';
    el.style.opacity = '0';
    el.style.transform = 'translateY(' + dy + 'px)';
    setTimeout(function () {
      if (el && el.parentNode) {
        el.style.display = 'none';
      }
    }, 360);
  }

  function buildAnswerScaffold(question, askerName) {
    var askerLabel = (askerName || 'YOU').toUpperCase();
    var wrap = document.createElement('div');
    wrap.className = 'ai-answer-live';
    // NB: no `.reveal` on any of these — that class is `opacity: 0` until
    // the global IntersectionObserver flips it to `.visible`, but the
    // observer only watches elements present at page-load. Anything we
    // inject here would otherwise stay invisible forever.
    wrap.innerHTML =
      '<header class="vc-hero ai-answer-hero" data-ai-live-hero>' +
        '<p class="ai-answer-eyebrow">// smarter dev · gemini 3.1 flash lite</p>' +
        '<h1 class="vc-hero-title ai-answer-title">' +
          '<span class="ai-title-text"></span>' +
          '<span class="ai-title-caret" aria-hidden="true"></span>' +
        '</h1>' +
      '</header>' +
      '<section class="ai-thread" data-ai-live-thread>' +
        '<article class="ai-turn ai-turn-user" data-ai-user-turn>' +
          '<header class="ai-turn-head">' +
            '<p class="ai-turn-role">' +
              '<span class="ai-turn-marker" aria-hidden="true"></span>' +
              '<span class="ai-turn-role-label">' + escapeHtml(askerLabel) + '</span>' +
            '</p>' +
            '<time class="ai-turn-time" datetime="' + escapeAttr(nowIso()) + '">' + clockNow() + '</time>' +
          '</header>' +
          '<div class="ai-turn-body ai-turn-question markdown-body" data-ai-user-body>' +
            paragraphsFromText(question) +
          '</div>' +
        '</article>' +
        '<article class="ai-turn ai-turn-assistant" data-ai-assistant-turn data-status="running">' +
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
          '</div>' +
        '</article>' +
      '</section>';
    return wrap;
  }

  function flipQuestionInto(targetBody, sourceTextarea, fullText) {
    if (!sourceTextarea || !targetBody) return;
    var src = sourceTextarea.getBoundingClientRect();
    var dst = targetBody.getBoundingClientRect();
    if (src.width === 0 || dst.width === 0) return;

    // Render the final paragraphs in the destination, but temporarily
    // transform it back to where the textarea was sitting, then animate
    // home. Pure FLIP — no clones — so the destination is the source of
    // truth from the first frame.
    var dx = src.left - dst.left;
    var dy = src.top - dst.top;
    var sx = src.width / dst.width;

    targetBody.style.transformOrigin = '0 0';
    targetBody.style.transition = 'none';
    targetBody.style.transform = 'translate(' + dx + 'px, ' + dy + 'px) scale(' + sx + ')';
    targetBody.style.opacity = '.7';

    // Force layout, then animate to identity.
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

  function appendBreadcrumb() {
    var crumb = document.querySelector('.vc-breadcrumb ol');
    if (!crumb) return;
    var lastLi = crumb.querySelector('li[aria-current]');
    if (lastLi) lastLi.removeAttribute('aria-current');
    if (lastLi) {
      lastLi.innerHTML = '<a href="/resources">Resources</a>';
    }
    var li = document.createElement('li');
    li.setAttribute('aria-current', 'page');
    li.textContent = 'Answer';
    crumb.appendChild(li);
  }

  function init() {
    var form = $('rsa-form');
    if (!form) return;

    var rsa = document.querySelector('[data-rsa-form]');
    var askerName = rsa && rsa.getAttribute('data-asker-name') || 'You';

    var textarea = $('rsa-input');
    var submitBtn = $('rsa-submit');
    var hint = $('rsa-hint');

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

    var morphing = false;

    function startMorph(question) {
      morphing = true;
      document.body.classList.add('ai-live');

      var wrap = document.querySelector('.vc-wrap.container');
      if (!wrap) return null;

      // Fade out the page furniture (hero, or-try, directory grid). The
      // history sidebar stays visible — it's still useful context on the
      // live answer view too.
      var faders = wrap.querySelectorAll(
        '.vc-hero, .rsa-or-try, .vc-section'
      );
      faders.forEach(function (el, i) {
        el.style.transition = 'opacity .3s ease, transform .3s ease';
        el.style.transitionDelay = (i * 40) + 'ms';
        el.style.opacity = '0';
        el.style.transform = 'translateY(-12px)';
        setTimeout(function () {
          if (el && el.parentNode) el.style.display = 'none';
        }, 350 + i * 40);
      });

      // Fade the ASK box itself last (it's the visual anchor).
      var ask = document.querySelector('.rsa');
      if (ask) {
        ask.style.transition = 'opacity .3s ease, transform .3s ease';
        ask.style.transitionDelay = '120ms';
        ask.style.opacity = '0';
        ask.style.transform = 'scale(.985) translateY(-6px)';
        setTimeout(function () {
          if (ask && ask.parentNode) ask.style.display = 'none';
        }, 430);
      }

      appendBreadcrumb();

      var scaffold = buildAnswerScaffold(question, askerName);
      var breadcrumb = wrap.querySelector('.vc-breadcrumb');
      if (breadcrumb && breadcrumb.nextSibling) {
        wrap.insertBefore(scaffold, breadcrumb.nextSibling);
      } else {
        wrap.appendChild(scaffold);
      }

      // FLIP-animate the question text from the textarea into its chat slot.
      requestAnimationFrame(function () {
        var body = scaffold.querySelector('[data-ai-user-body]');
        flipQuestionInto(body, textarea, question);
      });

      return scaffold;
    }

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      if (morphing) return;
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
      setHint(null, '');

      var scaffold = startMorph(question);

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
          if (resp.ok && resp.body && resp.body.url && resp.body.id) {
            try {
              history.pushState({ aiAnswer: true }, '', resp.body.url);
            } catch (e) { /* swallow — some browsers block pushState */ }

            var thread = scaffold && scaffold.querySelector('[data-ai-live-thread]');
            if (thread) {
              thread.setAttribute('data-conversation-id', resp.body.id);
              thread.setAttribute('data-asker-name', askerName);
              thread.setAttribute('data-answer-url', resp.body.url);
              var userTurn = thread.querySelector('[data-ai-user-turn]');
              if (userTurn && resp.body.user_message && resp.body.user_message.id) {
                userTurn.id = 'turn-' + resp.body.user_message.id;
              }
            }
            // Tell answer-time.js this conversation is mine — only events
            // with this conversation_id are allowed to claim an unstamped
            // thread (defeats the queued-replay race for old events).
            if (window.AIAnswerTime && window.AIAnswerTime.registerLiveConversation) {
              window.AIAnswerTime.registerLiveConversation(resp.body.id);
            }
            return;
          }
          var detail = (resp.body && (resp.body.detail || resp.body.message)) || '';
          if (resp.status === 401) {
            window.location.assign('/auth/login?next=/resources');
            return;
          }
          // Surface error in the assistant slot if we already morphed.
          handleLiveError(scaffold, detail || 'Something went wrong. Try again.');
        })
        .catch(function () {
          handleLiveError(scaffold, 'Network error. Check your connection and retry.');
        });
    });

    function handleLiveError(scaffold, detail) {
      var assistantBody = scaffold && scaffold.querySelector('[data-ai-assistant-body]');
      if (!assistantBody) return;
      assistantBody.innerHTML =
        '<p class="ai-live-error">' + escapeHtml(detail) + ' ' +
        '<a href="/resources">Try again on /resources →</a></p>';
    }

    // If the user hits back to /resources, just reload — easier than
    // trying to reverse the morph cleanly.
    window.addEventListener('popstate', function () {
      var path = window.location.pathname;
      if (document.body.classList.contains('ai-live') && path === '/resources') {
        window.location.reload();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
