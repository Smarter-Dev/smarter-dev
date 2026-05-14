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

  // When the Gemini-generated title lands (fired from the /v2/api/resources/ask
  // background task via notify_user), swap the placeholder in place — both in
  // the visible <h1> and the document <title>. Only the owner's sessions
  // receive this notification, so read-only viewers are unaffected.
  document.addEventListener('sk:notification', function (e) {
    var data = (e && e.detail) || {};
    if (data.type !== 'agent_title_updated') return;
    var thread = document.querySelector('.ai-thread[data-conversation-id]');
    if (!thread) return;
    if (thread.getAttribute('data-conversation-id') !== data.conversation_id) return;
    var newTitle = (data.title || '').trim();
    if (!newTitle) return;
    var h1 = document.querySelector('.ai-answer-title');
    if (h1) h1.textContent = newTitle;
    document.title = newTitle + ' · Smarter Dev';
    e.preventDefault();  // skip the default generic toast
  });

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { hydrate(); });
  } else {
    hydrate();
  }

  window.AIAnswerTime = { hydrate: hydrate, format: formatStamp };
})();
