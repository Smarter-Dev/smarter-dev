(function () {
    // In-page anchor links: scroll smoothly with an offset for the fixed nav.
    document.addEventListener('click', function (e) {
        var a = e.target.closest ? e.target.closest('.hp a[href^="#"]') : null;
        if (!a) return;
        var id = a.getAttribute('href').slice(1);
        if (!id) return;
        var target = document.getElementById(id);
        if (!target) return;
        e.preventDefault();
        var offset = 0;
        document.querySelectorAll('nav, header, [data-sk-partial="masthead"]').forEach(function (el) {
            var pos = getComputedStyle(el).position;
            if (pos === 'fixed' || pos === 'sticky') {
                offset = Math.max(offset, el.getBoundingClientRect().height);
            }
        });
        var top = target.getBoundingClientRect().top + window.scrollY - offset - 12;
        window.scrollTo({ top: top, behavior: 'smooth' });
        history.pushState(null, '', '#' + id);
    });
})();

(function () {
    var els = document.querySelectorAll('.hp .reveal');
    if (!els.length) return;

    function revealAll() {
        els.forEach(function (el) { el.classList.add('visible'); });
    }

    if (!('IntersectionObserver' in window)) {
        revealAll();
        return;
    }

    var io = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
            if (entry.isIntersecting) {
                entry.target.classList.add('visible');
                io.unobserve(entry.target);
            }
        });
    }, { threshold: 0.05, rootMargin: '0px 0px -5% 0px' });

    els.forEach(function (el) { io.observe(el); });

    // Fallback: if the observer never fires (layout quirk, print, etc.), reveal everything.
    setTimeout(revealAll, 1200);
})();

(function () {
    // Mobile demo tabs: each demo (Gym, Lab) collapses to a single visible pane
    // on small screens, switched by a tab bar. The Lab's Coach tab shows an
    // unread badge that counts coach messages arriving while it's not in view.
    var groups = document.querySelectorAll('.hp [data-demo-tabs]');
    if (!groups.length) return;

    var mobile = window.matchMedia('(max-width: 768px)');

    groups.forEach(function (tabs) {
        var root = tabs.closest('.gym-rep, .lab-shell') || tabs.parentElement;
        if (!root) return;
        var buttons = tabs.querySelectorAll('.demo-tab');
        var panes = root.querySelectorAll('[data-tab-pane]');

        function activate(name) {
            buttons.forEach(function (b) {
                var on = b.getAttribute('data-tab') === name;
                b.classList.toggle('is-active', on);
                b.setAttribute('aria-selected', on ? 'true' : 'false');
                if (on) {
                    var badge = b.querySelector('[data-unread]');
                    if (badge) { badge.textContent = ''; badge.hidden = true; badge.dataset.count = '0'; }
                }
            });
            panes.forEach(function (p) {
                p.classList.toggle('is-active', p.getAttribute('data-tab-pane') === name);
            });
        }

        buttons.forEach(function (b) {
            b.addEventListener('click', function () { activate(b.getAttribute('data-tab')); });
        });

        // Default to whichever tab the markup marks active (falls back to first).
        var initial = tabs.querySelector('.demo-tab.is-active') || buttons[0];
        if (initial) activate(initial.getAttribute('data-tab'));

        // Unread badge: only the Coach tab that carries [data-unread] is tracked.
        var coachBtn = tabs.querySelector('.demo-tab[data-tab="coach"]');
        var badge = coachBtn && coachBtn.querySelector('[data-unread]');
        if (!badge) return;
        var coachPane = root.querySelector('[data-tab-pane="coach"]');
        var thread = coachPane && coachPane.querySelector('[data-coach], [data-chat]');
        if (!thread) return;

        var mo = new MutationObserver(function (muts) {
            if (!mobile.matches) return;                      // tabs only exist on mobile
            if (coachBtn.classList.contains('is-active')) return; // already in view
            var added = 0;
            muts.forEach(function (m) {
                m.addedNodes.forEach(function (n) {
                    if (n.nodeType === 1 && n.classList.contains('cmsg')) added++;
                });
            });
            if (!added) return;
            var count = (parseInt(badge.dataset.count || '0', 10) || 0) + added;
            badge.dataset.count = String(count);
            badge.textContent = count > 9 ? '9+' : String(count);
            badge.hidden = false;
        });
        mo.observe(thread, { childList: true });
    });
})();
