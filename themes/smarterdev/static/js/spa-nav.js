/**
 * Skrift SPA Navigator
 *
 * Intercepts same-origin link clicks and fetches only changed content partials
 * via the X-Sk-Partial header. Falls back to full page loads gracefully.
 *
 * No dependencies — standalone ES module.
 */
(function () {
  'use strict';

  const PARTIAL_HEADER = 'X-Sk-Partial';
  const LOADING_CLASS = 'sk-partial-loading';
  const NO_SPA_ATTR = 'data-sk-no-spa';

  // Scroll position cache keyed by history state id
  const scrollCache = new Map();
  let stateId = 0;

  /**
   * Check if a URL is same-origin and eligible for SPA navigation.
   */
  function isSameOrigin(url) {
    try {
      const u = new URL(url, location.origin);
      return u.origin === location.origin;
    } catch {
      return false;
    }
  }

  /**
   * Check if a click event should be intercepted.
   */
  function shouldIntercept(event, anchor) {
    // Modifier keys or non-primary button
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
    if (event.button !== 0) return false;

    // Explicit opt-out
    if (anchor.hasAttribute(NO_SPA_ATTR)) return false;

    // External link or download
    if (anchor.target === '_blank') return false;
    if (anchor.hasAttribute('download')) return false;

    // Non-HTTP protocols
    const href = anchor.href;
    if (!href || href.startsWith('mailto:') || href.startsWith('tel:') || href.startsWith('javascript:')) return false;

    // Must be same origin
    if (!isSameOrigin(href)) return false;

    // Skip hash-only navigation on the same page
    const url = new URL(href, location.origin);
    if (url.pathname === location.pathname && url.hash) return false;

    return true;
  }

  /**
   * Get the list of partial names declared on the current page.
   */
  function getPartialNames() {
    const meta = document.querySelector('meta[name="sk-partials"]');
    return meta ? meta.getAttribute('content') || '' : 'masthead,content,footer';
  }

  /**
   * Swap partial content into the DOM.
   */
  function swapPartials(data) {
    const partials = data.partials || {};
    const swapped = [];

    for (const [name, html] of Object.entries(partials)) {
      const el = document.querySelector(`[data-sk-partial="${name}"]`);
      if (el) {
        el.innerHTML = html;
        swapped.push(name);
      }
    }

    // Update document title
    if (data.title) {
      document.title = data.title;
    }

    // Update page type meta
    if (data.meta && data.meta.page_type) {
      let meta = document.querySelector('meta[name="sk-page-type"]');
      if (meta) {
        meta.setAttribute('content', data.meta.page_type);
      }
    }

    // Update partial names meta
    if (data.meta && data.meta.partial_names) {
      let meta = document.querySelector('meta[name="sk-partials"]');
      if (meta) {
        meta.setAttribute('content', data.meta.partial_names);
      }
    }

    return swapped;
  }

  /**
   * Save current scroll position to cache.
   */
  function saveScroll() {
    const id = history.state && history.state._skId;
    if (id != null) {
      scrollCache.set(id, { x: scrollX, y: scrollY });
    }
  }

  /**
   * Restore scroll position from cache, or scroll to top.
   */
  function restoreScroll(id) {
    const pos = scrollCache.get(id);
    if (pos) {
      window.scrollTo(pos.x, pos.y);
    } else {
      window.scrollTo(0, 0);
    }
  }

  /**
   * Navigate to a URL via partial fetch.
   */
  async function navigate(url, opts = {}) {
    const { pushState = true, scrollId = null } = opts;

    // Add loading class
    document.documentElement.classList.add(LOADING_CLASS);

    try {
      const response = await fetch(url, {
        headers: {
          [PARTIAL_HEADER]: getPartialNames(),
          'Accept': 'application/json',
        },
        credentials: 'same-origin',
      });

      // Fall back to full page load on error or non-JSON
      const contentType = response.headers.get('content-type') || '';
      if (!response.ok || !contentType.includes('application/json')) {
        window.location.href = url;
        return;
      }

      const data = await response.json();

      // Save scroll before changing state
      saveScroll();

      // Swap the partials
      const swapped = swapPartials(data);

      // Update history
      if (pushState) {
        stateId++;
        history.pushState({ _skId: stateId, url }, data.title || '', url);
        window.scrollTo(0, 0);
      } else if (scrollId != null) {
        restoreScroll(scrollId);
      }

      // Dispatch navigation event for page-specific JS init
      document.dispatchEvent(new CustomEvent('sk:navigation', {
        detail: {
          url,
          partials: swapped,
          meta: data.meta || {},
          title: data.title || '',
        },
      }));

    } catch {
      // Network error — fall back to full page load
      window.location.href = url;
    } finally {
      document.documentElement.classList.remove(LOADING_CLASS);
    }
  }

  /**
   * Handle click events on anchors.
   */
  function onClick(event) {
    // Walk up to find the nearest <a>
    let anchor = event.target;
    while (anchor && anchor.tagName !== 'A') {
      anchor = anchor.parentElement;
    }
    if (!anchor) return;

    if (shouldIntercept(event, anchor)) {
      event.preventDefault();
      navigate(anchor.href);
    }
  }

  /**
   * Handle form submissions (GET forms only).
   */
  function onSubmit(event) {
    const form = event.target;
    if (form.method && form.method.toUpperCase() !== 'GET') return;
    if (form.hasAttribute(NO_SPA_ATTR)) return;

    const action = form.action || location.href;
    if (!isSameOrigin(action)) return;

    event.preventDefault();
    const params = new URLSearchParams(new FormData(form));
    const url = new URL(action, location.origin);
    url.search = params.toString();
    navigate(url.toString());
  }

  /**
   * Handle popstate (back/forward navigation).
   */
  function onPopState(event) {
    const state = event.state;
    if (!state || !state.url) {
      // No SPA state — full reload
      location.reload();
      return;
    }
    navigate(state.url, { pushState: false, scrollId: state._skId });
  }

  /**
   * Initialize the SPA navigator.
   */
  function init() {
    // Set initial history state
    stateId++;
    history.replaceState({ _skId: stateId, url: location.href }, document.title);

    // Event listeners
    document.addEventListener('click', onClick);
    document.addEventListener('submit', onSubmit);
    window.addEventListener('popstate', onPopState);

    // Save scroll on beforeunload for the case where user navigates away
    window.addEventListener('beforeunload', saveScroll);
  }

  // Auto-init when DOM is ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
