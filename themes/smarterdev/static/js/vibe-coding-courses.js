/**
 * Vibe Coding Courses — client-side filtering + popularity sort per section.
 *
 * Each section (currently "tools" and "workflow") has its own chip group, list,
 * and optional popularity toggle, linked together by matching `data-vc-target`
 * values. Chip groups carry `data-vc-attr` to say which card attribute they
 * filter against (data-tools for the tools section, data-category for workflow).
 *
 * Hash-based deep links:
 *   #tools/cursor             → scroll to "By Tool", activate the Cursor chip
 *   #workflow/best-practices  → scroll to the workflow section, filter to Best Practices
 *   #workflow                 → scroll to the section, keep current filter
 *
 * Filtering and reordering happen entirely in the DOM. Re-initializes on
 * `sk:navigation` so SPA-nav into the page rewires handlers against the
 * freshly-swapped partial.
 */
(function () {
  'use strict';

  function parseHash() {
    var h = (location.hash || '').replace(/^#/, '');
    if (!h) return null;
    var parts = h.split('/');
    return { section: parts[0], value: parts[1] ? decodeURIComponent(parts[1]) : null };
  }

  function updateHash(target, value) {
    if (!history.replaceState) return;
    // Use the bare section anchor when the value is the implicit "all"
    // (workflow section's default chip). For sections without an "all"
    // chip (tools), always include the value.
    var hash = value && value !== 'all' ? '#' + target + '/' + value : '#' + target;
    if (location.hash !== hash) history.replaceState(null, '', hash);
  }

  function initSection(target) {
    var chipGroup = document.querySelector('[data-vc-chips][data-vc-target="' + target + '"]');
    var list = document.querySelector('[data-vc-list][data-vc-target="' + target + '"]');
    if (!chipGroup || !list) return;
    if (list.dataset.vcWired === '1') return;
    list.dataset.vcWired = '1';

    var attr = chipGroup.getAttribute('data-vc-attr') || 'tools';
    // For multi-value attributes (e.g. data-tools="cursor claude-code"), match
    // if the requested value is in the space-separated set. For single-value
    // attributes (e.g. data-category="best-practices"), it's just equality.
    function cardMatches(card, value) {
      if (value === 'all') return true;
      var raw = card.getAttribute('data-' + attr) || '';
      if (attr === 'tools') {
        return raw.split(/\s+/).indexOf(value) !== -1;
      }
      return raw === value;
    }

    var chips = chipGroup.querySelectorAll('.vc-chip');
    var defaultOrder = Array.prototype.slice.call(list.children);
    var toggle = document.querySelector('[data-vc-popularity][data-vc-target="' + target + '"]');

    function applyFilter(value) {
      var cards = list.children;
      for (var i = 0; i < cards.length; i++) {
        cards[i].hidden = !cardMatches(cards[i], value);
      }
    }

    function applySort(byPopularity) {
      var ordered;
      if (byPopularity) {
        ordered = defaultOrder.slice().sort(function (a, b) {
          var ca = parseInt(a.getAttribute('data-click-count') || '0', 10);
          var cb = parseInt(b.getAttribute('data-click-count') || '0', 10);
          return cb - ca;
        });
      } else {
        ordered = defaultOrder;
      }
      var frag = document.createDocumentFragment();
      ordered.forEach(function (n) { frag.appendChild(n); });
      list.appendChild(frag);
    }

    function activate(chip, opts) {
      opts = opts || {};
      chips.forEach(function (c) { c.classList.remove('is-active'); });
      chip.classList.add('is-active');
      var value = chip.getAttribute('data-value') || 'all';
      applyFilter(value);
      if (opts.updateHash !== false) updateHash(target, value);
    }

    chips.forEach(function (chip) {
      chip.addEventListener('click', function () { activate(chip); });
    });

    // Honor a deep-link hash like #tools/cursor or #workflow/best-practices.
    // Falls back to the chip marked `is-active` in the rendered HTML
    // (claude-code for tools; "all" for workflow).
    var parsed = parseHash();
    var initialChip = null;
    if (parsed && parsed.section === target && parsed.value) {
      initialChip = chipGroup.querySelector('[data-value="' + parsed.value + '"]');
    }
    if (!initialChip) {
      initialChip = chipGroup.querySelector('.vc-chip.is-active');
    }
    if (initialChip) {
      // Don't write the hash for the implicit default on load — only when
      // the user actively clicks.
      activate(initialChip, { updateHash: false });
    }

    if (toggle) {
      toggle.addEventListener('change', function () { applySort(toggle.checked); });
    }
  }

  function initSmoothScroll() {
    var links = document.querySelectorAll('.vc-view-all, .vc-toc-link');
    links.forEach(function (link) {
      if (link.dataset.vcScrollWired === '1') return;
      link.dataset.vcScrollWired = '1';
      link.addEventListener('click', function (event) {
        var hash = link.getAttribute('href') || '';
        if (!hash.startsWith('#')) return;
        var target = document.getElementById(hash.slice(1));
        if (!target) return;
        event.preventDefault();
        target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        // Reflect in URL without re-triggering the browser scroll.
        if (history.replaceState) {
          history.replaceState(null, '', hash);
        }
      });
    });
  }

  function initActiveTocHighlight() {
    var tocLinks = document.querySelectorAll('.vc-toc-link');
    if (!tocLinks.length || !('IntersectionObserver' in window)) return;
    var map = {};
    tocLinks.forEach(function (link) {
      var id = (link.getAttribute('href') || '').slice(1);
      if (id) map[id] = link;
    });
    var sections = Object.keys(map)
      .map(function (id) { return document.getElementById(id); })
      .filter(Boolean);
    if (!sections.length) return;

    // Track which observed sections are currently in the trigger zone, then
    // highlight the topmost one in DOM order. When the user is at the top of
    // the page (above the first observed section) or below the last, no chip
    // is active — important since the Featured section isn't in the nav and
    // we don't want a stale chip lit up while it's on screen.
    var intersecting = new Set();
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) intersecting.add(e.target.id);
        else intersecting.delete(e.target.id);
      });
      tocLinks.forEach(function (l) { l.classList.remove('is-active'); });
      for (var i = 0; i < sections.length; i++) {
        if (intersecting.has(sections[i].id)) {
          var link = map[sections[i].id];
          if (link) link.classList.add('is-active');
          break;
        }
      }
    }, { rootMargin: '-30% 0px -60% 0px', threshold: 0 });
    sections.forEach(function (s) { observer.observe(s); });
  }

  function scrollToHashSection() {
    var parsed = parseHash();
    if (!parsed) return;
    var section = document.getElementById(parsed.section);
    if (!section) return;
    // The browser only auto-scrolls when the hash matches an element ID,
    // so for compound hashes like #tools/cursor we drive the scroll ourselves.
    section.scrollIntoView({ behavior: 'auto', block: 'start' });
  }

  function onHashChange() {
    var parsed = parseHash();
    if (!parsed) return;
    var chipGroup = document.querySelector('[data-vc-chips][data-vc-target="' + parsed.section + '"]');
    if (chipGroup && parsed.value) {
      var chip = chipGroup.querySelector('[data-value="' + parsed.value + '"]');
      if (chip) chip.click();
    }
    scrollToHashSection();
  }

  function init() {
    initSection('tools');
    initSection('workflow');
    initSmoothScroll();
    initActiveTocHighlight();
    // If the page loaded with a compound hash (#tools/cursor), scroll into
    // place — the section's filter was already applied inside initSection().
    if (parseHash() && parseHash().value) {
      scrollToHashSection();
    }
    window.addEventListener('hashchange', onHashChange);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  document.addEventListener('sk:navigation', init);
})();
