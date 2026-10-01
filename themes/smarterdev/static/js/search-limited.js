/** The search-limit page: count down to when the visitor can search again. */
(function () {
  'use strict';

  const box = document.querySelector('[data-limited][data-wait]');
  if (!box) return;
  const shown = box.querySelector('[data-wait-seconds]');
  const retry = box.querySelector('[data-retry]');
  let left = Number(box.dataset.wait);
  const timer = window.setInterval(function () {
    left -= 1;
    if (left > 0) {
      shown.textContent = left;
      return;
    }
    window.clearInterval(timer);
    box.querySelector('[data-wait-line]').textContent = 'You can search again now.';
    if (retry) retry.removeAttribute('aria-disabled');
  }, 1000);
})();
