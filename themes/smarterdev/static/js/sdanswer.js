/**
 * sdanswer hydrator — turns ``.sdanswer[data-block-id]`` placeholders inside
 * assistant turns into real card rows, paths, and modal-triggered cards.
 *
 * The matching payload travels in a sibling ``<script type="application/json"
 * class="sdanswer-payload">`` element on the turn article. We read the JSON,
 * find each placeholder by index, and replace it with the rendered DOM.
 *
 * Exposed entry points:
 *   - ``SDAnswer.hydrateAll()`` — runs at DOMContentLoaded on every turn.
 *   - ``SDAnswer.hydrate(article)`` — call after dynamically inserting a
 *     turn (used by answer-view.js for follow-up replies).
 */
(function () {
  'use strict';

  var TYPE_LABELS = {
    'Article': 'ARTICLE',
    'Blog Post': 'ARTICLE',
    'Tutorial': 'TUTORIAL',
    'Course': 'COURSE',
    'Book': 'BOOK',
    'Talk': 'TALK',
    'Video': 'WATCH',
    'Podcast': 'LISTEN',
    'Tool': 'TOOL',
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function typeChip(learningType) {
    if (!learningType) return '';
    var label = TYPE_LABELS[learningType] || String(learningType).toUpperCase();
    return '<span class="sda-chip sda-chip-type">' + esc(label) + '</span>';
  }

  function hexGlyph() {
    return (
      '<svg class="sda-hex" width="12" height="12" viewBox="0 0 10 10" aria-hidden="true">' +
      '<polygon points="5,0.5 9.5,3 9.5,7 5,9.5 0.5,7 0.5,3" fill="none" stroke="currentColor" stroke-width="1.2"/>' +
      '<circle cx="5" cy="5" r="1.8" fill="currentColor"/>' +
      '</svg>'
    );
  }

  function renderArticleCard(card) {
    var trackAttr = card.track_key ? ' data-track-key="' + esc(card.track_key) + '"' : '';
    return (
      '<a class="sda-card sda-card-article" href="' + esc(card.url) + '"' + trackAttr +
      ' target="_blank" rel="noopener" data-sk-no-spa>' +
      '<div class="sda-card-meta">' +
      typeChip(card.learning_type) +
      (card.byline ? '<span class="sda-byline">' + esc(card.byline) + '</span>' : '') +
      '</div>' +
      '<h3 class="sda-card-title">' + esc(card.title) + '</h3>' +
      (card.blurb ? '<p class="sda-card-blurb">' + esc(card.blurb) + '</p>' : '') +
      '</a>'
    );
  }

  function renderSnippetCard(card, index) {
    var firstLines = String(card.snippet || '').split('\n').slice(0, 4).join('\n');
    return (
      '<button type="button" class="sda-card sda-card-snippet" data-sda-open="snippet" data-sda-index="' + index + '">' +
      '<div class="sda-card-meta">' +
      '<span class="sda-chip sda-chip-type">SNIPPET</span>' +
      (card.category ? '<span class="sda-chip sda-chip-cat">' + esc(card.category) + '</span>' : '') +
      '</div>' +
      '<h3 class="sda-card-title">' + esc(card.title) + '</h3>' +
      (card.description ? '<p class="sda-card-blurb">' + esc(card.description) + '</p>' : '') +
      '<pre class="sda-snippet-preview"><code>' + esc(firstLines) + '</code></pre>' +
      '<span class="sda-card-cta">VIEW SNIPPET ' + hexGlyph() + '</span>' +
      '</button>'
    );
  }

  function renderCollectionCard(card, index) {
    var preview = (card.items || []).slice(0, 3).map(function (item) {
      return (
        '<li class="sda-coll-row">' +
        typeChip(item.learning_type) +
        '<span class="sda-coll-row-title">' + esc(item.title) + '</span>' +
        '</li>'
      );
    }).join('');
    var total = (card.items || []).length;
    return (
      '<button type="button" class="sda-card sda-card-collection" data-sda-open="collection" data-sda-index="' + index + '">' +
      '<div class="sda-card-meta">' +
      '<span class="sda-chip sda-chip-type">COLLECTION · ' + total + '</span>' +
      (card.category ? '<span class="sda-chip sda-chip-cat">' + esc(card.category) + '</span>' : '') +
      '</div>' +
      '<h3 class="sda-card-title">' + esc(card.title) + '</h3>' +
      (card.description ? '<p class="sda-card-blurb">' + esc(card.description) + '</p>' : '') +
      '<ul class="sda-coll-preview">' + preview + '</ul>' +
      '<span class="sda-card-cta">OPEN COLLECTION ' + hexGlyph() + '</span>' +
      '</button>'
    );
  }

  function renderTradeoffCard(card) {
    var options = (card.options || []).map(function (opt) {
      var bullets = (opt.bullets || []).map(function (b) {
        return '<li>' + esc(b) + '</li>';
      }).join('');
      return (
        '<div class="sda-tradeoff-option">' +
        '<h4 class="sda-tradeoff-label">' + esc(opt.label) + '</h4>' +
        '<ul class="sda-tradeoff-bullets">' + bullets + '</ul>' +
        '</div>'
      );
    }).join('');
    return (
      '<div class="sda-card sda-card-tradeoff">' +
      '<div class="sda-card-meta">' +
      '<span class="sda-chip sda-chip-type">TRADEOFF</span>' +
      '</div>' +
      '<h3 class="sda-card-title">' + esc(card.title) + '</h3>' +
      '<div class="sda-tradeoff-grid">' + options + '</div>' +
      '</div>'
    );
  }

  function renderPrereqCard(card) {
    var items = (card.items || []).map(function (item) {
      if (item.url) {
        return (
          '<li class="sda-prereq-item sda-prereq-item-link">' +
          '<a href="' + esc(item.url) + '" target="_blank" rel="noopener" data-sk-no-spa>' +
          esc(item.label) +
          '</a>' +
          '</li>'
        );
      }
      return '<li class="sda-prereq-item">' + esc(item.label) + '</li>';
    }).join('');
    return (
      '<div class="sda-card sda-card-prereq">' +
      '<div class="sda-card-meta">' +
      '<span class="sda-chip sda-chip-type">PREREQ</span>' +
      '</div>' +
      '<h3 class="sda-card-title">' + esc(card.title) + '</h3>' +
      '<ul class="sda-prereq-list">' + items + '</ul>' +
      '</div>'
    );
  }

  function renderGotchaCard(card) {
    return (
      '<div class="sda-card sda-card-gotcha">' +
      '<div class="sda-card-meta">' +
      '<span class="sda-chip sda-chip-type sda-chip-warn">GOTCHA</span>' +
      (card.language ? '<span class="sda-chip sda-chip-cat">' + esc(card.language) + '</span>' : '') +
      '</div>' +
      '<h3 class="sda-card-title">' + esc(card.title) + '</h3>' +
      (card.description ? '<p class="sda-card-blurb">' + esc(card.description) + '</p>' : '') +
      '<pre class="sda-gotcha-wrong"><code>' + esc(card.wrong) + '</code></pre>' +
      (card.right ? '<p class="sda-gotcha-right"><span class="sda-gotcha-right-label">Instead:</span> ' + esc(card.right) + '</p>' : '') +
      '</div>'
    );
  }

  function renderCards(payload, blockId) {
    var cards = (payload.cards || []).map(function (card, i) {
      var cardIndex = blockId + ':' + i;
      if (card.type === 'snippet')    return renderSnippetCard(card, cardIndex);
      if (card.type === 'collection') return renderCollectionCard(card, cardIndex);
      if (card.type === 'tradeoff')   return renderTradeoffCard(card);
      if (card.type === 'prereq')     return renderPrereqCard(card);
      if (card.type === 'gotcha')     return renderGotchaCard(card);
      return renderArticleCard(card);
    }).join('');
    return '<div class="sda-cards-row">' + cards + '</div>';
  }

  function renderPath(payload) {
    var steps = (payload.steps || []).map(function (step, i) {
      var num = String(i + 1).padStart(2, '0');
      var trackAttr = step.track_key ? ' data-track-key="' + esc(step.track_key) + '"' : '';
      var estimateChip = step.estimate
        ? '<span class="sda-path-estimate">~' + esc(step.estimate) + '</span>'
        : '';
      return (
        '<li class="sda-path-step">' +
        '<span class="sda-path-num">' + num + '</span>' +
        '<div class="sda-path-body">' +
        '<div class="sda-path-meta">' + typeChip(step.learning_type) +
        (step.byline ? '<span class="sda-byline">' + esc(step.byline) + '</span>' : '') +
        estimateChip +
        '</div>' +
        '<h4 class="sda-path-title">' + esc(step.title) + '</h4>' +
        (step.description ? '<p class="sda-path-desc">' + esc(step.description) + '</p>' : '') +
        '</div>' +
        '<a class="sda-path-cta" href="' + esc(step.url) + '"' + trackAttr +
        ' target="_blank" rel="noopener" data-sk-no-spa>' + hexGlyph() + ' DIVE IN</a>' +
        '</li>'
      );
    }).join('');
    var label = payload.title || 'Resource Path';
    var count = (payload.steps || []).length;
    var estimate = payload.estimate ? ' · ~' + esc(payload.estimate) : '';
    return (
      '<div class="sda-path">' +
      '<p class="sda-path-header">// ' + esc(label.toLowerCase()) + ' · ' + count + ' step' + (count === 1 ? '' : 's') + estimate + '</p>' +
      '<ol class="sda-path-list">' + steps + '</ol>' +
      '</div>'
    );
  }

  function hydrateArticle(article) {
    if (!article || article.__sdaHydrated) return;
    var script = article.querySelector('script.sdanswer-payload');
    if (!script) return;
    var payload;
    try { payload = JSON.parse(script.textContent || '[]'); }
    catch (e) { return; }
    if (!Array.isArray(payload) || payload.length === 0) return;

    article.__sdaHydrated = true;
    article.__sdaBlocks = payload;

    var placeholders = article.querySelectorAll('.sdanswer[data-block-id]');
    Array.prototype.forEach.call(placeholders, function (slot) {
      var idx = parseInt(slot.getAttribute('data-block-id'), 10);
      if (isNaN(idx) || !payload[idx]) return;
      var block = payload[idx];
      var html;
      if (block.type === 'cards')      html = renderCards(block, idx);
      else if (block.type === 'path')  html = renderPath(block);
      else return;
      slot.innerHTML = html;
      slot.setAttribute('data-block-hydrated', '1');
    });
  }

  function hydrateAll(root) {
    var scope = root || document;
    var articles = scope.querySelectorAll('.ai-turn-assistant[data-has-sdanswer]');
    Array.prototype.forEach.call(articles, hydrateArticle);
  }

  // ── Modal ──────────────────────────────────────────────────────────────
  var modal = null;
  function ensureModal() {
    if (modal) return modal;
    modal = document.createElement('div');
    modal.className = 'sda-modal';
    modal.setAttribute('hidden', '');
    modal.innerHTML =
      '<div class="sda-modal-backdrop" data-sda-close></div>' +
      '<div class="sda-modal-window" role="dialog" aria-modal="true" aria-labelledby="sda-modal-title">' +
      '<button type="button" class="sda-modal-close" data-sda-close aria-label="Close">×</button>' +
      '<div class="sda-modal-body" id="sda-modal-body"></div>' +
      '</div>';
    document.body.appendChild(modal);
    modal.addEventListener('click', function (e) {
      if (e.target && e.target.matches && e.target.matches('[data-sda-close]')) closeModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !modal.hasAttribute('hidden')) closeModal();
    });
    return modal;
  }

  function openModal(innerHtml) {
    ensureModal();
    modal.querySelector('#sda-modal-body').innerHTML = innerHtml;
    modal.removeAttribute('hidden');
    document.body.classList.add('sda-modal-open');
  }

  function closeModal() {
    if (!modal) return;
    modal.setAttribute('hidden', '');
    modal.querySelector('#sda-modal-body').innerHTML = '';
    document.body.classList.remove('sda-modal-open');
  }

  function snippetModalBody(card) {
    return (
      '<p class="sda-modal-eyebrow">// snippet' + (card.category ? ' · ' + esc(card.category) : '') + '</p>' +
      '<h2 id="sda-modal-title" class="sda-modal-title">' + esc(card.title) + '</h2>' +
      (card.description ? '<p class="sda-modal-desc">' + esc(card.description) + '</p>' : '') +
      '<div class="sda-modal-snippet">' +
      '<div class="sda-modal-snippet-head">' +
      (card.language ? '<span class="sda-chip sda-chip-cat">' + esc(card.language) + '</span>' : '<span></span>') +
      '<button type="button" class="sda-modal-copy" data-sda-copy>COPY</button>' +
      '</div>' +
      '<pre><code class="sda-modal-snippet-body">' + esc(card.snippet) + '</code></pre>' +
      '</div>'
    );
  }

  function collectionModalBody(card) {
    var items = (card.items || []).map(function (item) { return renderArticleCard(item); }).join('');
    return (
      '<p class="sda-modal-eyebrow">// collection' + (card.category ? ' · ' + esc(card.category) : '') + '</p>' +
      '<h2 id="sda-modal-title" class="sda-modal-title">' + esc(card.title) + '</h2>' +
      (card.description ? '<p class="sda-modal-desc">' + esc(card.description) + '</p>' : '') +
      '<div class="sda-modal-grid">' + items + '</div>'
    );
  }

  document.addEventListener('click', function (event) {
    var trigger = event.target && event.target.closest && event.target.closest('[data-sda-open]');
    if (trigger) {
      event.preventDefault();
      var kind = trigger.getAttribute('data-sda-open');
      var idxAttr = trigger.getAttribute('data-sda-index') || '';
      var parts = idxAttr.split(':');
      var blockIdx = parseInt(parts[0], 10);
      var cardIdx = parseInt(parts[1], 10);
      var article = trigger.closest('.ai-turn-assistant');
      var blocks = article && article.__sdaBlocks;
      if (!blocks || isNaN(blockIdx) || isNaN(cardIdx)) return;
      var block = blocks[blockIdx];
      if (!block || block.type !== 'cards') return;
      var card = (block.cards || [])[cardIdx];
      if (!card) return;
      if (kind === 'snippet')    openModal(snippetModalBody(card));
      else if (kind === 'collection') openModal(collectionModalBody(card));
      return;
    }
    if (event.target && event.target.matches && event.target.matches('[data-sda-copy]')) {
      var pre = modal && modal.querySelector('.sda-modal-snippet-body');
      if (pre && navigator.clipboard) {
        navigator.clipboard.writeText(pre.textContent || '').then(function () {
          event.target.textContent = 'COPIED';
          setTimeout(function () { event.target.textContent = 'COPY'; }, 1500);
        });
      }
    }
  });

  /* ── Copy-answer button ──────────────────────────────────────────────
     Fetches the raw markdown from `/v2/api/agent/messages/{id}/markdown`
     on click and writes it to the clipboard. Keeps the HTML source clean
     (no duplicate <template> stash per turn) while still giving real
     markdown to whoever pastes — lists, code fences, sdanswer blocks.

     Falls back to `bodyEl.innerText` if the turn has no `data-turn-id`
     (e.g. a freshly-rendered live turn that hasn't been persisted yet)
     or the fetch fails.
  */
  function flashCopied(btn) {
    var def = btn.querySelector('[data-default]');
    var ok = btn.querySelector('[data-copied]');
    if (def) def.setAttribute('hidden', '');
    if (ok) ok.removeAttribute('hidden');
    btn.classList.add('is-copied');
    setTimeout(function () {
      if (def) def.removeAttribute('hidden');
      if (ok) ok.setAttribute('hidden', '');
      btn.classList.remove('is-copied');
    }, 1600);
  }

  function writeAndFlash(text, btn) {
    if (!text) return;
    var write = navigator.clipboard && navigator.clipboard.writeText
      ? navigator.clipboard.writeText(text)
      : Promise.reject(new Error('clipboard unavailable'));
    write.then(function () { flashCopied(btn); }).catch(function () {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); flashCopied(btn); } catch (e) { /* swallow */ }
      document.body.removeChild(ta);
    });
  }

  document.addEventListener('click', function (event) {
    var btn = event.target.closest && event.target.closest('[data-ai-copy]');
    if (!btn) return;
    event.preventDefault();
    var turn = btn.closest('.ai-turn-assistant');
    if (!turn) return;
    var turnId = turn.getAttribute('data-turn-id') ||
                 (turn.id && turn.id.indexOf('turn-') === 0 ? turn.id.slice(5) : null);
    if (turnId) {
      fetch('/v2/api/agent/messages/' + encodeURIComponent(turnId) + '/markdown', {
        credentials: 'same-origin',
        headers: { 'Accept': 'text/markdown, text/plain' },
      })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(new Error(r.status)); })
        .then(function (md) { writeAndFlash(md.replace(/^\n+/, '').replace(/\n+$/, ''), btn); })
        .catch(function () {
          // Fallback to rendered text if the API roundtrip fails.
          var body = turn.querySelector('.ai-turn-body');
          var md = body ? (body.innerText || body.textContent || '') : '';
          writeAndFlash(md.replace(/^\n+/, '').replace(/\n+$/, ''), btn);
        });
      return;
    }
    // No id yet — use the rendered text.
    var body2 = turn.querySelector('.ai-turn-body');
    var md2 = body2 ? (body2.innerText || body2.textContent || '') : '';
    writeAndFlash(md2.replace(/^\n+/, '').replace(/\n+$/, ''), btn);
  });

  window.SDAnswer = { hydrate: hydrateArticle, hydrateAll: hydrateAll };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { hydrateAll(); });
  } else {
    hydrateAll();
  }
})();
