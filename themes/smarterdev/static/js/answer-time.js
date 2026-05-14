/**
 * /ai/answer/{id} — localize turn timestamps to the browser's clock.
 *
 * The server renders each turn header with a UTC fallback like `7:11 PM` plus
 * a machine-readable `datetime="…+00:00"` on the <time> element. This script
 * rewrites the visible text to the user's locale and timezone via
 * `Date.toLocaleTimeString` so a reader in California sees their local clock,
 * not the pod's UTC.
 */
(function () {
  'use strict';

  function format(date) {
    if (!date || isNaN(date.getTime())) return '';
    return date.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      hour12: true,
    });
  }

  function hydrate(root) {
    var nodes = (root || document).querySelectorAll('time.ai-turn-time[datetime]');
    for (var i = 0; i < nodes.length; i++) {
      var iso = nodes[i].getAttribute('datetime');
      if (!iso) continue;
      var text = format(new Date(iso));
      if (text) nodes[i].textContent = text;
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { hydrate(); });
  } else {
    hydrate();
  }

  window.AIAnswerTime = { hydrate: hydrate };
})();
