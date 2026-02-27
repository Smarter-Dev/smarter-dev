(function() {
    var logo = document.getElementById('sudo-logo');
    var rest = document.getElementById('sudo-hero-rest');
    var spans = logo.querySelectorAll('span');
    var waits = [1200, 600, 600, 600];
    var current = 0;

    function step() {
        if (current > 0) {
            spans[current - 1].classList.remove('cursor');
            spans[current - 1].classList.add('show');
        }
        if (current < spans.length) {
            spans[current].classList.add('cursor');
            setTimeout(step, waits[current]);
            current++;
        } else {
            setTimeout(function() {
                var children = rest.children;
                for (var j = 0; j < children.length; j++) {
                    (function(el, delay) {
                        setTimeout(function() { el.classList.add('show'); }, delay);
                    })(children[j], j * 200);
                }
            }, 1200);
        }
    }
    setTimeout(step, 500);
})();

(function() {
    var forms = [
        { form: document.getElementById('sudo-signup-form-hero'), msg: document.getElementById('sudo-signup-msg-hero') },
        { form: document.getElementById('sudo-signup-form'), msg: document.getElementById('sudo-signup-msg') }
    ];

    forms.forEach(function(pair) {
        if (!pair.form) return;
        pair.form.addEventListener('submit', function(e) {
            e.preventDefault();
            var email = pair.form.querySelector('input[name="email"]').value;
            var slug = pair.form.querySelector('input[name="campaign_slug"]').value;
            var btn = pair.form.querySelector('button');

            btn.disabled = true;
            btn.textContent = '...';
            pair.msg.textContent = '';
            pair.msg.className = 'sudo-signup-msg';

            fetch('/v2/api/campaign-signups', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({campaign_slug: slug, email: email})
            })
            .then(function(r) { return r.json().then(function(d) { return {ok: r.ok, data: d}; }); })
            .then(function(res) {
                if (res.ok) {
                    pair.msg.textContent = 'Check your inbox to confirm your email.';
                    pair.msg.className = 'sudo-signup-msg ok';
                    pair.form.querySelector('input[name="email"]').value = '';
                } else {
                    pair.msg.textContent = res.data.detail || 'Something went wrong.';
                    pair.msg.className = 'sudo-signup-msg err';
                }
            })
            .catch(function() {
                pair.msg.textContent = 'Network error. Try again.';
                pair.msg.className = 'sudo-signup-msg err';
            })
            .finally(function() {
                btn.disabled = false;
                btn.textContent = 'Notify me';
            });
        });
    });
})();
