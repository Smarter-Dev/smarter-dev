/* ══════════════════════════════════════════════════════
   Gym Rep — "Try it out" inline challenge (on rails)
   Always open. Two fixed-height columns that scroll on their
   own: the lesson on the left, a coach chat on the right.
   The prompt is the first message, the options are the
   composer, and on submit the feedback and the "that was one
   rep" close-out slide in at the bottom and push the thread up.
   Each message holds one embedded card, tinted by tone.
   The map stays a neutral static reference — it never reacts.
   ══════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var VERDICTS = {
    A: { tone: 'bad', text: 'That is the trap, and it is the common one. You built on the broken boundary. Now Payments owns money movement, pricing, and stored value, so the next discount feature lands in the service you least want to touch. The blast radius only grows from here.' },
    B: { tone: 'good', text: 'That is the move. With pricing on its own boundary, gift cards become a pricing change and Payments never has to know they exist. You spent one refactor to make every future pricing feature cheap.' },
    C: { tone: 'warn', text: 'Tempting, since checkout lives in Orders, but you just spread pricing logic into a third place. Orders computes discounts, Payments still does too, and the receipt reads its own copy. The rule has no home, so every change has to track down all three.' }
  };

  var BRIDGE = '<b>That was one rep.</b> The Gym is a few hundred of them, each system worse than the last, with a coach on every one, until reading a system this way is reflex. That was a slice of Boundaries, the first course.';

  function init(rep) {
    var chat = rep.querySelector('[data-chat]');
    var opts = rep.querySelectorAll('.gym-opt');
    var submit = rep.querySelector('.btn-submit');
    var picked = null, locked = false;
    var timers = [];

    function toBottom() {
      // keep the newest message in view within the chat's own scroll
      chat.scrollTop = chat.scrollHeight;
    }

    // ── option select ──
    opts.forEach(function (o) {
      o.addEventListener('click', function () {
        if (locked) return;
        opts.forEach(function (x) { x.classList.remove('selected'); });
        o.classList.add('selected');
        picked = o.getAttribute('data-opt');
        submit.disabled = false;
      });
    });

    // ── chat helpers ──
    function bubble(html, tone) {
      var d = document.createElement('div');
      d.className = 'cmsg' + (tone ? ' feedback ' + tone : '');
      d.setAttribute('data-appended', '');
      d.innerHTML = '<div class="cbubble">' + html + '</div>';
      return d;
    }
    function push(node, ms) {
      timers.push(setTimeout(function () {
        chat.appendChild(node);
        toBottom();
      }, ms));
    }

    // ── submit ──
    submit.addEventListener('click', function () {
      if (locked || !picked) return;
      locked = true;
      opts.forEach(function (o) {
        o.disabled = true;
        if (o.getAttribute('data-opt') === picked) {
          o.classList.remove('selected');
          o.classList.add(picked === 'B' ? 'correct' : picked === 'C' ? 'partial' : 'wrong');
        }
      });
      submit.style.display = 'none';

      var v = VERDICTS[picked];
      push(bubble(v.text, v.tone), 240);
      push(bubble(BRIDGE, null), 1000);

      timers.push(setTimeout(function () {
        var a = document.createElement('div');
        a.className = 'chat-actions';
        a.setAttribute('data-appended', '');
        a.innerHTML = '<button class="gym-retry" type="button">Try another</button>';
        a.querySelector('.gym-retry').addEventListener('click', reset);
        chat.appendChild(a);
        toBottom();
      }, 1500));
    });

    // ── reset ──
    function reset() {
      timers.forEach(clearTimeout); timers = [];
      locked = false; picked = null;
      chat.querySelectorAll('[data-appended]').forEach(function (n) { n.remove(); });
      opts.forEach(function (o) { o.disabled = false; o.classList.remove('selected', 'correct', 'wrong', 'partial'); });
      submit.style.display = '';
      submit.disabled = true;
      toBottom();
    }
  }

  function boot() {
    var rep = document.querySelector('.gym-rep');
    if (rep) init(rep);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
