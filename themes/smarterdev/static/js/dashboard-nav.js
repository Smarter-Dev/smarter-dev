/**
 * The dashboard sidebar's accordion (templates/dashboard/_nav.html), on the
 * search pages and the chat pages alike.
 *
 * One section is open at a time. The server opens the one for what is on
 * screen; the chevron opens or shuts a section in place, and following a
 * section's entry opens it before the page changes, so the rail never shows
 * the old section open over the new view. dashboard.js calls
 * `dashboardNav.open('search')` when its own routing changes the view.
 */
(function () {
  'use strict';

  const nav = document.querySelector('[data-ud-nav]');
  if (!nav) return;

  function setSection(section, open) {
    section.toggleAttribute('data-open', open);
    const toggle = section.querySelector('[data-ud-toggle]');
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    document.getElementById(toggle.getAttribute('aria-controls')).hidden = !open;
  }

  function show(section, open) {
    if (open) {
      nav.querySelectorAll('[data-ud-section]').forEach(function (each) {
        if (each !== section) setSection(each, false);
      });
    }
    setSection(section, open);
  }

  nav.addEventListener('click', function (event) {
    const toggle = event.target.closest('[data-ud-toggle]');
    if (toggle) {
      show(toggle.closest('[data-ud-section]'), toggle.getAttribute('aria-expanded') !== 'true');
      return;
    }
    const entry = event.target.closest('[data-ud-entry]');
    // A modified click opens a new tab and leaves this page as it is.
    if (!entry || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    show(entry.closest('[data-ud-section]'), true);
  });

  window.dashboardNav = {
    open: function (name) {
      const section = nav.querySelector('[data-ud-section="' + name + '"]');
      if (section && !section.hasAttribute('data-open')) show(section, true);
    },
  };
})();
