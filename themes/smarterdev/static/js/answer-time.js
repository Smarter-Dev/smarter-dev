/**
 * /ai/answer/{id} — localize turn timestamps to the browser's clock and add
 * date prefixes for older messages.
 *
 * Format scheme:
 *   Today     -> "8:20am"
 *   Yesterday -> "Yesterday @ 8:20am"
 *   < 7 days  -> "Monday @ 8:20am"
 *   older     -> "5/7 @ 8:20am"
 *
 * Also wires a delegated click handler for `[data-share-url]` buttons that
 * copy a URL to the clipboard and briefly swap a "COPIED ✓" label.
 */
(function () {
  'use strict';

  function formatTime(date) {
    // Force the en-US "h:mm AM/PM" shape so we can compress to "8:20am"
    // regardless of the visitor's locale. The timezone still follows the
    // browser, which is what the user actually cares about.
    var s = date.toLocaleTimeString('en-US', {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
    return s.replace(' ', '').toLowerCase();
  }

  function startOfDay(d) {
    return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  }

  function shortDate(d) {
    return (d.getMonth() + 1) + '/' + d.getDate();
  }

  function formatStamp(date) {
    if (!date || isNaN(date.getTime())) return '';
    var time = formatTime(date);
    var today = startOfDay(new Date());
    var that = startOfDay(date);
    var diffDays = Math.round((today - that) / 86400000);
    if (diffDays <= 0) return time;
    if (diffDays === 1) return 'Yesterday @ ' + time;
    if (diffDays < 7) {
      var day = date.toLocaleDateString(undefined, { weekday: 'long' });
      return day + ' @ ' + time;
    }
    return shortDate(date) + ' @ ' + time;
  }

  function hydrate(root) {
    var nodes = (root || document).querySelectorAll('time.ai-turn-time[datetime]');
    for (var i = 0; i < nodes.length; i++) {
      var iso = nodes[i].getAttribute('datetime');
      if (!iso) continue;
      var text = formatStamp(new Date(iso));
      if (text) nodes[i].textContent = text;
    }
  }

  function writeClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        resolve();
      } catch (err) { reject(err); }
    });
  }

  function flashCopied(btn) {
    var def = btn.querySelector('[data-default]');
    var ok = btn.querySelector('[data-copied]');
    if (def && ok) {
      def.setAttribute('hidden', '');
      ok.removeAttribute('hidden');
      btn.classList.add('is-copied');
      setTimeout(function () {
        def.removeAttribute('hidden');
        ok.setAttribute('hidden', '');
        btn.classList.remove('is-copied');
      }, 1600);
    } else {
      var prev = btn.textContent;
      btn.textContent = 'COPIED ✓';
      btn.classList.add('is-copied');
      setTimeout(function () {
        btn.textContent = prev;
        btn.classList.remove('is-copied');
      }, 1600);
    }
  }

  document.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest && e.target.closest('[data-share-url]');
    if (!btn) return;
    e.preventDefault();
    var url = btn.getAttribute('data-share-url');
    if (!url) return;
    writeClipboard(url).then(function () { flashCopied(btn); });
  });

  // ---------------------------------------------------------------------
  // Helpers for real-time answer events (sk:notification → DOM updates).
  // The conversation-id match is required for every event so background
  // notifications from a different open tab don't bleed in.
  // ---------------------------------------------------------------------

  function matchThread(data) {
    var thread = document.querySelector('.ai-thread[data-conversation-id]');
    if (!thread) return null;
    if (thread.getAttribute('data-conversation-id') !== data.conversation_id) {
      return null;
    }
    return thread;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ── Title typewriter ─────────────────────────────────────────────────
  function typewriteTitle(text) {
    var h1 = document.querySelector('.ai-answer-title');
    if (!h1) return;
    var slot = h1.querySelector('.ai-title-text');
    var caret = h1.querySelector('.ai-title-caret');
    if (!slot) {
      // No live scaffold — instant swap (e.g. an already-loaded answer page).
      h1.textContent = text;
      return;
    }
    var i = 0;
    function step() {
      if (i >= text.length) {
        if (caret) {
          caret.style.transition = 'opacity .3s ease';
          caret.style.opacity = '0';
          setTimeout(function () { caret.remove(); }, 320);
        }
        return;
      }
      slot.textContent += text.charAt(i++);
      setTimeout(step, 28);
    }
    slot.textContent = '';
    step();
  }

  // ── Tool chips (one per agent_tool_event) ────────────────────────────
  function toolIcon(tool) {
    if (tool === 'search_resources') return '⌬';
    if (tool === 'read_source') return '▤';
    return '·';
  }

  function renderToolChip(stream, payload) {
    if (!stream) return;
    var prev = stream.querySelector('[data-status="active"]');
    if (prev) prev.setAttribute('data-status', 'done');
    var chip = document.createElement('div');
    chip.className = 'ai-tool-chip';
    chip.setAttribute('data-status', 'active');
    chip.setAttribute('data-tool', payload.tool || '');
    chip.innerHTML =
      '<span class="ai-tool-chip-icon" aria-hidden="true">' + escapeHtml(toolIcon(payload.tool)) + '</span>' +
      '<span class="ai-tool-chip-label">' + escapeHtml(payload.tool || 'tool') + '</span>' +
      '<span class="ai-tool-chip-arg">' + escapeHtml(payload.label || '') + '</span>' +
      '<span class="ai-tool-chip-summary">' + escapeHtml(payload.summary || '') + '</span>';
    stream.appendChild(chip);
  }

  // ── Final answer reveal ──────────────────────────────────────────────
  function revealAnswer(thread, data) {
    var assistant = thread.querySelector('.ai-turn-assistant[data-ai-assistant-turn]')
                 || thread.querySelector('.ai-turn-assistant');
    if (!assistant) return;
    if (data.assistant_message_id) {
      assistant.id = 'turn-' + data.assistant_message_id;
      assistant.setAttribute('data-turn-id', data.assistant_message_id);
    }
    var stream = assistant.querySelector('[data-ai-tools-stream]');
    var prose = assistant.querySelector('[data-ai-answer-prose]');
    if (stream) {
      stream.setAttribute('data-collapsing', '1');
      stream.style.transition = 'opacity .35s ease, max-height .35s ease, margin .35s ease, padding .35s ease';
      stream.style.opacity = '0';
      stream.style.maxHeight = '0';
      stream.style.marginTop = '0';
      stream.style.marginBottom = '0';
      stream.style.paddingTop = '0';
      stream.style.paddingBottom = '0';
      setTimeout(function () { if (stream.parentNode) stream.remove(); }, 380);
    }
    var content = data.content_html || '';
    var hasBlocks = Array.isArray(data.sdanswer_blocks) && data.sdanswer_blocks.length > 0;

    if (prose) {
      prose.classList.add('markdown-body');
      prose.classList.add('ai-turn-answer-prose');
      prose.innerHTML = content;
      prose.hidden = false;
      prose.style.opacity = '0';
      prose.style.transform = 'translateY(6px)';
      prose.style.transition = 'opacity .45s ease, transform .45s ease';
      // Force layout before animating in.
      void prose.offsetWidth;
      prose.style.opacity = '1';
      prose.style.transform = 'translateY(0)';
    }

    if (hasBlocks) {
      assistant.setAttribute('data-has-sdanswer', '1');
      var script = document.createElement('script');
      script.type = 'application/json';
      script.className = 'sdanswer-payload';
      script.textContent = JSON.stringify(data.sdanswer_blocks);
      assistant.appendChild(script);
      if (window.SDAnswer && typeof window.SDAnswer.hydrate === 'function') {
        window.SDAnswer.hydrate(assistant);
      }
    }

    // Copy/Share action footer.
    var answerUrl = thread.getAttribute('data-answer-url') || (window.location.origin + window.location.pathname);
    var turnFragment = data.assistant_message_id ? '#turn-' + data.assistant_message_id : '';
    var footer = document.createElement('footer');
    footer.className = 'ai-turn-actions';
    footer.style.opacity = '0';
    footer.style.transform = 'translateY(6px)';
    footer.innerHTML =
      '<button type="button" class="ai-turn-action" data-ai-copy aria-label="Copy answer as markdown">' +
        '<span class="ai-turn-action-label" data-default>COPY</span>' +
        '<span class="ai-turn-action-label" data-copied hidden>COPIED &check;</span>' +
      '</button>' +
      '<button type="button" class="ai-turn-action" data-share-url="' + escapeHtml(answerUrl + turnFragment) + '" aria-label="Copy a link to this message">' +
        '<span class="ai-turn-action-label" data-default>SHARE</span>' +
        '<span class="ai-turn-action-label" data-copied hidden>COPIED &check;</span>' +
      '</button>';
    assistant.appendChild(footer);
    setTimeout(function () {
      footer.style.transition = 'opacity .4s ease .2s, transform .4s ease .2s';
      footer.style.opacity = '1';
      footer.style.transform = 'translateY(0)';
    }, 50);

    assistant.setAttribute('data-status', 'done');
  }

  function showError(thread, detail) {
    var assistant = thread.querySelector('.ai-turn-assistant[data-ai-assistant-turn]')
                 || thread.querySelector('.ai-turn-assistant');
    if (!assistant) return;
    var stream = assistant.querySelector('[data-ai-tools-stream]');
    if (stream) stream.remove();
    var prose = assistant.querySelector('[data-ai-answer-prose]');
    if (prose) {
      prose.hidden = false;
      prose.innerHTML =
        '<p class="ai-live-error">' + escapeHtml(detail || 'Agent failed to respond.') + ' ' +
        '<a href="/resources">Try again on /resources →</a></p>';
    }
    assistant.setAttribute('data-status', 'error');
  }

  document.addEventListener('sk:notification', function (e) {
    var data = (e && e.detail) || {};
    if (!data || !data.type) return;

    if (data.type === 'agent_title_updated') {
      var thread = matchThread(data);
      if (!thread) return;
      var newTitle = (data.title || '').trim();
      if (!newTitle) return;
      typewriteTitle(newTitle);
      document.title = newTitle + ' · Smarter Dev';
      e.preventDefault();
      return;
    }

    if (data.type === 'agent_tool_event') {
      var thread2 = matchThread(data);
      if (!thread2) return;
      var stream = thread2.querySelector('[data-ai-tools-stream]');
      renderToolChip(stream, data);
      e.preventDefault();
      return;
    }

    if (data.type === 'agent_run_complete') {
      var thread3 = matchThread(data);
      if (!thread3) return;
      revealAnswer(thread3, data);
      e.preventDefault();
      return;
    }

    if (data.type === 'agent_run_error') {
      var thread4 = matchThread(data);
      if (!thread4) return;
      showError(thread4, data.detail);
      e.preventDefault();
      return;
    }
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { hydrate(); });
  } else {
    hydrate();
  }

  window.AIAnswerTime = { hydrate: hydrate, format: formatStamp };
})();
