/* ══════════════════════════════════════════════════════
   Lab Demo — on-rails coding-agent TUI + coaching chat (mock)
   Left column: a terminal UI for a coding agent — a scrolling
   transcript plus an input field. When a decision is due the
   input lights up; clicking it opens an options menu, and the
   chosen option is echoed back as a message the user sent
   before the agent acts on it.
   Right column: the same coaching chat as the Gym. After every
   coding-agent turn the coach drops a tone-tinted note on what
   the decision actually cost.
   ══════════════════════════════════════════════════════ */
(function () {
  'use strict';

  function init(shell) {
    var term = shell.querySelector('[data-term]');
    var inputBtn = shell.querySelector('[data-input]');
    var inputText = shell.querySelector('[data-input-text]');
    var inputLine = shell.querySelector('[data-input-line]');
    var menu = shell.querySelector('[data-menu]');

    // Keep the latest typed character (and the caret) in view: the input clips
    // overflow, so scroll the line to its right edge after each text update.
    function setTyped(s) {
      if (!inputText) return;
      inputText.textContent = s;
      if (inputLine) inputLine.scrollLeft = inputLine.scrollWidth;
    }
    var coachEl = shell.querySelector('[data-coach]');

    var state = { call1Clean: false, call2Clean: false, busy: false };
    var pending = null;     // { options, onPick }
    var timers = [];        // all scheduled steps, cleared on reset
    var intervals = [];     // typing intervals, cleared on reset
    function later(fn, ms) { var id = setTimeout(fn, ms); timers.push(id); return id; }

    // ── transcript helpers (left terminal) ──
    var clock = 0;
    function pushNow(html, cls) {
      var d = document.createElement('div');
      d.className = 'tline in ' + (cls || '');
      d.innerHTML = html;
      term.appendChild(d);
      term.scrollTop = term.scrollHeight;
      return d;
    }
    function emit(html, cls, gap) {
      gap = gap == null ? 130 : gap;
      clock += gap;
      later(function () { pushNow(html, cls); }, clock);
    }
    function after(fn, gap) { clock += (gap == null ? 160 : gap); later(fn, clock); }

    function agent(html, gap) { emit('<span class="t-agent"><span class="bul">\u23fa</span><span>' + html + '</span></span>', '', gap); }
    function sub(html) { emit('<span class="t-sub"><span class="cor">\u23bf</span><span>' + html + '</span></span>'); }
    function ok(t) { emit('<span class="t-ok">\u2713 ' + t + '</span>'); }
    function warn(t) { emit('<span class="t-warn">! ' + t + '</span>'); }
    function bad(t) { emit('<span class="t-bad">\u2717 ' + t + '</span>'); }
    // simulated file-edit command with addition / deletion counts
    function edit(path, add, del) {
      emit('<span class="t-edit"><span class="ed-op">edit</span>' +
        '<span class="ed-path">' + path + '</span>' +
        '<span class="ed-add">+' + add + '</span>' +
        '<span class="ed-del">\u2212' + del + '</span></span>');
    }
    function userMsg(text) { pushNow('<span class="t-user"><span class="uarr">\u276f</span><span>' + text + '</span></span>'); }

    // ── coaching chat (right column) ──
    function coach(html, tone, label) {
      var d = document.createElement('div');
      d.className = 'cmsg' + (tone ? ' feedback ' + tone : '');
      d.innerHTML = '<div class="cbubble">' + (label ? '<span class="cb-label">' + label + '</span>' : '') + html + '</div>';
      coachEl.appendChild(d);
      coachEl.scrollTop = coachEl.scrollHeight;
    }

    // ── input + options menu ──
    function setInput(stateName) {
      if (inputText) inputText.textContent = '';
      inputBtn.classList.toggle('ready', stateName === 'ready');
      inputBtn.disabled = stateName !== 'ready';
      if (stateName !== 'ready') closeMenu();
    }
    function openMenu(options) {
      var html = '<div class="tui-menu-panel"><div class="tui-menu-head">// choose your prompt</div>';
      options.forEach(function (o) {
        html += '<button class="tui-mi" type="button" data-k="' + o.k + '">' +
          '<span class="mbody"><span class="mt">' + o.title + '</span></span>' +
          '</button>';
      });
      html += '</div>';
      menu.innerHTML = html;
      menu.hidden = false;
      inputBtn.classList.add('open');
      menu.querySelector('.tui-menu-panel').addEventListener('click', function (e) { e.stopPropagation(); });
      menu.querySelectorAll('.tui-mi').forEach(function (mi) {
        mi.addEventListener('click', function (e) {
          e.stopPropagation();
          var k = mi.getAttribute('data-k');
          var opt = options.filter(function (x) { return x.k === k; })[0];
          selectOption(opt);
        });
      });
    }
    function closeMenu() { menu.hidden = true; inputBtn.classList.remove('open'); }
    function toggleMenu() { if (menu.hidden) { if (pending) openMenu(pending.options); } else closeMenu(); }

    inputBtn.addEventListener('click', function (e) { e.stopPropagation(); if (pending) toggleMenu(); });
    menu.addEventListener('click', function () { closeMenu(); });
    document.addEventListener('click', function () { closeMenu(); });

    // pick → close overlay → type the prompt into the TUI input → send it
    function selectOption(opt) {
      if (!pending) return;
      var onPick = pending.onPick;
      pending = null;
      closeMenu();
      typeAndSend(opt.title, function () { clock = 0; onPick(opt.k); });
    }
    function typeAndSend(text, done) {
      inputBtn.classList.remove('ready');
      inputBtn.classList.add('typing');
      inputBtn.disabled = true;
      setTyped('');
      // Time-based typing: compute the character count from elapsed wall-clock
      // time rather than one setTimeout per character. Under timer throttling the
      // interval still fires (just late), and each tick catches up to the right
      // position — so it can never freeze mid-word, and completion always runs.
      var start = Date.now();
      var dur = Math.min(900, 200 + text.length * 22);
      var finished = false;
      function finish() {
        if (finished) return;
        finished = true;
        inputBtn.classList.remove('typing');
        setTyped('');
        userMsg(text);
        done();
      }
      var iv = setInterval(function () {
        var t = (Date.now() - start) / dur;
        if (t >= 1) {
          clearInterval(iv);
          setTyped(text);
          later(finish, 300);
          return;
        }
        setTyped(text.slice(0, Math.max(0, Math.floor(t * text.length))));
      }, 28);
      intervals.push(iv);
    }
    function arm(options, onPick, prompt) {
      pending = { options: options, onPick: onPick };
      setInput('ready', prompt);
    }
    function ask(question, options, onPick, prompt) {
      // the coach poses the decision in the chat; you answer it in the terminal
      clock += 260;
      later(function () { coach(question, null, 'YOUR TASK'); }, clock);
      clock += 150;
      later(function () { arm(options, onPick, prompt); }, clock);
    }

    // The flow pauses after each call. A sound call advances on its own. An unsound
    // call drops a Continue button (proceed, and let the mistake bite at the stress
    // test) and also arms the input with two fixes — pick one to set it right.
    function advance(nextFn) { state.busy = false; clock = 0; nextFn(); }

    // ── boot ──
    function boot() {
      setInput('busy');
      coach('Ship gift cards on this checkout. The agent writes the code, the architectural calls are yours, and I\u2019ll flag what each one costs. First call: gift-card balances need a home. Tell the agent what to do.', null, 'YOUR TASK');
      var art = [
        '\u250c\u2500\u2510\u250c\u252c\u2510\u250c\u2500\u2510\u252c\u2500\u2510\u250c\u252c\u2510\u250c\u2500\u2510\u252c\u2500\u2510  \u250c\u252c\u2510\u250c\u2500\u2510\u252c  \u252c',
        '\u2514\u2500\u2510\u2502\u2502\u2502\u251c\u2500\u2524\u251c\u252c\u2518 \u2502 \u251c\u2524 \u251c\u252c\u2518   \u2502\u2502\u251c\u2524 \u2514\u2510\u250c\u2518',
        '\u2514\u2500\u2518\u2534 \u2534\u2534 \u2534\u2534\u2514\u2500 \u2534 \u2514\u2500\u2518\u2534\u2514\u2500  \u2500\u2534\u2518\u2514\u2500\u2518 \u2514\u2518 '
      ].join('\n');
      emit('<span class="t-art">' + art + '</span>', '', 200);
      emit('<span class="t-dim">build the judgment agents can\u2019t</span>', '');
      after(node1, 320);
    }

    // ── NODE 1 — gift-card balances need a home ──
    function node1() {
      clock = 0;
      arm([
        { k: 'A', title: 'Put it inside the Payments service' },
        { k: 'B', title: 'Give it its own Balances component' }
      ], chooseNode1);
    }
    function chooseNode1(pick) {
      if (state.busy) return;
      state.busy = true;
      if (pick === 'B') {
        // sound call — advances on its own
        agent('creating <span class="hl">components/balances/</span> (balance, ledger, redeem)', 320);
        edit('components/balances/balance.py', 94, 0);
        edit('components/balances/ledger.py', 61, 0);
        edit('components/balances/redeem.py', 38, 0);
        ok('build passed · 0 conflicts');
        after(function () {
          coach('Balances is its own thing with a clean surface. Payments stays about moving money. Stored value can change now without anyone else noticing, which is the cut paying off.', 'good');
          state.call1Clean = true;
          advance(node2);
        }, 240);
      } else {
        // unsound call — the input arms with two fixes
        agent('extending <span class="hl">services/payments/</span> with balance and redemption logic', 320);
        edit('services/payments/payments.py', 186, 4);
        edit('services/payments/redeem.py', 52, 0);
        ok('build passed');
        after(function () {
          coach('Balance lives inside Payments now, so anything that touches stored value has to go through the service that moves money. You just widened a seam that was already too wide.', 'warn');
          state.call1Clean = false;
          state.busy = false;
          arm([
            { k: 'R', title: 'Pull balance into its own Balances component.' },
            { k: 'E', title: 'Give balance its own module but keep it inside the payments service.' }
          ], fixNode1);
        }, 240);
      }
    }
    function fixNode1(pick) {
      if (state.busy) return;
      state.busy = true;
      if (pick === 'R') {
        agent('moving balance out of <span class="hl">payments/</span> into <span class="hl">components/balances/</span>', 320);
        edit('components/balances/balance.py', 118, 0);
        edit('services/payments/payments.py', 6, 186);
        ok('build passed · 0 conflicts');
        after(function () {
          coach('That\u2019s the call. Balance is its own thing now and Payments just moves money. That\u2019s clean.', 'good');
          state.call1Clean = true;
          advance(node2);
        }, 240);
      } else {
        agent('balance pulled behind its own interface, still deployed under <span class="hl">payments/</span>', 320);
        edit('services/payments/balance/__init__.py', 88, 0);
        edit('services/payments/payments.py', 14, 172);
        ok('build passed');
        after(function () {
          coach('Right enough. Balance has its own surface and the coupling is mostly gone, even if it still ships inside payments. We\u2019ll take it.', 'good');
          state.call1Clean = true;
          advance(node2);
        }, 240);
      }
    }

    // ── NODE 2 — a gift card has to discount the order ──
    function node2() {
      clock = 0;
      ask('A gift card has to discount the order. How does it get applied?', [
        { k: 'A', title: 'Let Orders and Notifications read the balance and apply it themselves' },
        { k: 'B', title: 'Pricing exposes one adjustment the others consume' }
      ], chooseNode2);
    }
    function chooseNode2(pick) {
      if (state.busy) return;
      state.busy = true;
      if (pick === 'B') {
        // sound call — advances on its own
        agent('<span class="hl">Pricing.applyAdjustments()</span> returns final line items', 320);
        edit('services/pricing/pricing.py', 73, 2);
        edit('services/orders/orders.py', 4, 41);
        edit('services/notifications/receipt.py', 3, 28);
        ok('build passed · rule lives in one place');
        after(function () {
          coach('One service owns how a card is applied, and everyone else just reads the answer. The rule has a single home, so it has a single place to change.', 'good');
          state.call2Clean = true;
          advance(node3);
        }, 240);
      } else {
        // unsound call — the input arms with two fixes
        agent('importing balance logic into <span class="hl">orders/</span> and <span class="hl">notifications/</span>', 320);
        edit('services/orders/orders.py', 96, 0);
        edit('services/notifications/receipt.py', 84, 0);
        ok('build passed');
        after(function () {
          coach('Three services now know how a gift card is applied. Change the rule in one place and you\u2019ve already forgotten the other two.', 'warn');
          state.call2Clean = false;
          state.busy = false;
          arm([
            { k: 'R', title: 'Make Pricing the one place that applies it; everyone else reads the result.' },
            { k: 'E', title: 'Pick one service to own it and have the others read from there.' }
          ], fixNode2);
        }, 240);
      }
    }
    function fixNode2(pick) {
      if (state.busy) return;
      state.busy = true;
      if (pick === 'R') {
        agent('<span class="hl">Pricing.applyAdjustments()</span> owns it, <span class="hl">orders/</span> and <span class="hl">notifications/</span> consume the result', 320);
        edit('services/pricing/pricing.py', 68, 0);
        edit('services/orders/orders.py', 5, 96);
        edit('services/notifications/receipt.py', 4, 84);
        ok('build passed · rule in one place');
        after(function () {
          coach('That\u2019s it. One owner for the rule, everyone else reads the answer. That\u2019s clean.', 'good');
          state.call2Clean = true;
          advance(node3);
        }, 240);
      } else {
        agent('<span class="hl">orders/</span> becomes the single owner, <span class="hl">notifications/</span> reads from it', 320);
        edit('services/orders/orders.py', 58, 0);
        edit('services/notifications/receipt.py', 6, 84);
        ok('build passed · rule in one place');
        after(function () {
          coach('Right enough. You gave the rule one owner, which is the part that matters, even if Pricing is the more natural home. We\u2019ll take it.', 'good');
          state.call2Clean = true;
          advance(node3);
        }, 240);
      }
    }

    // ── NODE 3 (stress) ──
    function node3() {
      clock = 0;
      ask('A promotion launches that stacks with gift cards. Ship the change.', [
        { k: '\u25b6', title: 'Let\u2019s see if it works, run it', note: 'rebuild and bring the stack up' }
      ], function () { runChange(); }, 'ship the change…');
    }

    function runChange() {
      if (state.busy) return;
      state.busy = true; clock = 0;
      var clean = (state.call1Clean ? 1 : 0) + (state.call2Clean ? 1 : 0);

      agent('running <span class="hl">docker compose up --build</span>', 300);

      if (clean === 2) {
        sub('rebuilding <span class="hl">pricing</span>');
        ok('1 service rebuilt · stack up · tests green');
        after(function () {
          coach('One service lit up. The promo only touched Pricing, because the balance is isolated and the discount rule lives in one place. The same change tore through five a minute ago. This time it stayed in one.', 'good', 'RESULT · 1 SERVICE TOUCHED');
          finish();
        }, 260);
      } else if (clean === 1) {
        sub('rebuilding <span class="hl">pricing</span>, <span class="hl">orders</span>, <span class="hl">notifications</span>');
        emit('<span class="t-dim">3 services rebuilt · stack up · 1 test flaky</span>');
        after(function () {
          coach('Three services. One clean call wasn\u2019t enough \u2014 the open seam pulled the promo into services that have no business caring about it. Half-contained still means a change you\u2019ll be chasing later.', 'warn', 'RESULT · 3 SERVICES TOUCHED');
          finish();
        }, 260);
      } else {
        sub('rebuilding <span class="hl">payments</span>, <span class="hl">pricing</span>, <span class="hl">orders</span>, <span class="hl">notifications</span>, <span class="hl">inventory</span>');
        bad('5 services rebuilt · 2 tests failing');
        after(function () {
          coach('Every service. The balance leaked into Payments and the rule leaked into Orders and Notifications, so a promo that\u2019s really a pricing tweak rewrites half the system. The only difference from a clean build was where you drew the lines.', 'bad', 'RESULT · 5 SERVICES TOUCHED');
          finish();
        }, 260);
      }
    }

    function finish() {
      after(function () {
        emit('<span class="t-dim">session complete · try again to run a different path</span>', '');
        after(function () {
          coach('That was a guided run. In the real Lab the agents and infrastructure are live, and the systems are real. The calls are yours, and you stop predicting what a decision will cost and start measuring it.', null, 'THE LAB');
          var a = document.createElement('div');
          a.className = 'chat-actions';
          a.innerHTML = '<button class="gym-retry" type="button">Try again</button>';
          a.querySelector('.gym-retry').addEventListener('click', reset);
          coachEl.appendChild(a);
          coachEl.scrollTop = coachEl.scrollHeight;
          setInput('done', 'session complete');
          state.busy = false;
        }, 60);
      }, 260);
    }

    // ── reset / replay ──
    function reset() {
      timers.forEach(clearTimeout); timers = [];
      intervals.forEach(clearInterval); intervals = [];
      term.innerHTML = '';
      coachEl.innerHTML = '';
      pending = null; closeMenu();
      inputBtn.classList.remove('typing', 'ready');
      if (inputText) inputText.textContent = '';
      state = { call1Clean: false, call2Clean: false, busy: false };
      clock = 0;
      boot();
    }
    document.querySelectorAll('[data-lab-replay]').forEach(function (b) {
      b.addEventListener('click', function (e) { e.preventDefault(); reset(); });
    });

    // ── start when scrolled into view (IO + scroll + interval backstop) ──
    var started = false, io = null, timer = null;
    function cleanup() {
      window.removeEventListener('scroll', tryStart);
      if (io) io.disconnect();
      if (timer) clearInterval(timer);
    }
    function tryStart() {
      if (started) return;
      var r = shell.getBoundingClientRect();
      var vh = window.innerHeight || document.documentElement.clientHeight;
      if (r.top < vh * 0.8 && r.bottom > 0) { started = true; cleanup(); boot(); }
    }
    try {
      io = new IntersectionObserver(function (entries) {
        entries.forEach(function (en) { if (en.isIntersecting) tryStart(); });
      }, { threshold: 0.2 });
      io.observe(shell);
    } catch (e) { /* IO unsupported */ }
    window.addEventListener('scroll', tryStart, { passive: true });
    timer = setInterval(tryStart, 600);
    tryStart();
  }

  // ── left-column view toggle (terminal ⇄ files) ──
  // Independent of the Agent/Coach mobile switcher; no shared selectors.
  function wireLeftTabs(shell) {
    var tabs = shell.querySelectorAll('[data-lab-view]');
    if (!tabs.length) return;
    var panes = shell.querySelectorAll('[data-lab-pane]');
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        var view = tab.getAttribute('data-lab-view');
        tabs.forEach(function (t) {
          var on = t === tab;
          t.classList.toggle('is-active', on);
          t.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        panes.forEach(function (p) {
          p.classList.toggle('is-active', p.getAttribute('data-lab-pane') === view);
        });
      });
    });
  }

  function bootAll() {
    document.querySelectorAll('[data-lab]').forEach(init);
    document.querySelectorAll('[data-lab-left]').forEach(wireLeftTabs);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bootAll);
  else bootAll();
})();
