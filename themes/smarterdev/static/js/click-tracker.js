/**
 * Site-wide outbound-link click tracker.
 *
 * Any anchor decorated with `data-track-key="..."` fires a sendBeacon to
 * /v2/api/track-click on click. We deliberately don't preventDefault — the
 * Beacon API is designed for fire-and-forget on unload, so the browser
 * navigates immediately while the beacon is queued. That keeps middle-click,
 * ctrl/cmd-click, target=_blank, keyboard activation, and assistive tech
 * working unchanged.
 */
(function () {
  'use strict';

  var ENDPOINT = '/v2/api/track-click';

  function findTracked(target) {
    var el = target;
    while (el && el !== document.body) {
      if (el.tagName === 'A' && el.dataset && el.dataset.trackKey) return el;
      el = el.parentElement;
    }
    return null;
  }

  function onClick(event) {
    var anchor = findTracked(event.target);
    if (!anchor) return;

    var payload = JSON.stringify({
      key: anchor.dataset.trackKey,
      url: anchor.href,
    });

    try {
      if (navigator.sendBeacon) {
        var blob = new Blob([payload], { type: 'application/json' });
        navigator.sendBeacon(ENDPOINT, blob);
      } else {
        // Fallback for environments without sendBeacon: best-effort fetch
        // with keepalive so the request survives the unload.
        fetch(ENDPOINT, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: payload,
          keepalive: true,
          credentials: 'same-origin',
        });
      }
    } catch (_) {
      // Tracking must never block navigation.
    }
  }

  document.addEventListener('click', onClick, true);
})();
