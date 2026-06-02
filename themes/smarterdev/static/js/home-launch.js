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
