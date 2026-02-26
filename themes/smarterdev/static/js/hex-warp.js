/**
 * Hex Warp Background — Interactive hexagonal grid canvas animation.
 *
 * Extracted from mockup-42. Renders a field of hexagons with:
 * - Mouse-driven particle displacement
 * - Ambient wave animations
 * - Click-based radial deflection
 * - Animated trace paths flowing through the hex grid
 * - Intersection Observer for scroll-triggered .reveal animations
 *
 * Automatically reinitializes on sk:navigation events (SPA navigation).
 */
(function () {
  'use strict';

  function initHexWarp() {
    var canvas = document.getElementById('bg');
    if (!canvas) return;
    var ctx = canvas.getContext('2d');

    // Hex grid config
    var HEX_SIZE = 18;
    var H_SPACING = HEX_SIZE * Math.sqrt(3);
    var V_SPACING = HEX_SIZE * 1.5;
    var DOT_RADIUS = 2;
    var INFLUENCE_RADIUS = 200;
    var MAX_DISPLACEMENT = 20;
    var LERP_SPEED = 0.08;
    var AMBIENT_AMPLITUDE = 1;
    var AMBIENT_SPEED = 0.0008;
    var CLICK_DURATION = 500;
    var HEX_LINE_REST = 0.025;
    var HEX_LINE_ACTIVE = 0.08;
    var PATH_COUNT = 60;
    var PATH_SPEED = 1.2;
    var TURN_CHANCE = 0.2;
    var SEGMENT_FADE = 0.001;
    var SEGMENT_PEAK_ALPHA = 0.12;

    // Read accent color from CSS variable (allows per-page override)
    var accentStyle = getComputedStyle(document.documentElement).getPropertyValue('--hex-accent').trim();
    var accentR = 0, accentG = 212, accentB = 255;
    if (accentStyle) {
      var m = accentStyle.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
      if (m) { accentR = parseInt(m[1], 16); accentG = parseInt(m[2], 16); accentB = parseInt(m[3], 16); }
    }
    function ac(a) { return 'rgba(' + accentR + ',' + accentG + ',' + accentB + ',' + a + ')'; }

    var dots = [];
    var cols = 0;
    var rows = 0;
    var mouseX = -9999;
    var mouseY = -9999;
    var mouseActive = false;
    var clickTime = 0;
    var clickActive = false;
    var animId = null;
    var traces = [];

    // Hex vertex offsets
    var hexVerts = [];
    for (var i = 0; i < 6; i++) {
      var angle = Math.PI / 180 * (60 * i - 30);
      hexVerts.push({ x: HEX_SIZE * Math.cos(angle), y: HEX_SIZE * Math.sin(angle) });
    }

    function createDots() {
      var w = canvas.width;
      var h = canvas.height;
      cols = Math.ceil(w / H_SPACING) + 3;
      rows = Math.ceil(h / V_SPACING) + 3;
      dots = [];
      for (var row = 0; row < rows; row++) {
        for (var col = 0; col < cols; col++) {
          var isOdd = row % 2 === 1;
          dots.push({
            baseX: col * H_SPACING + (isOdd ? H_SPACING * 0.5 : 0) - H_SPACING,
            baseY: row * V_SPACING - V_SPACING,
            offsetX: 0, offsetY: 0,
            targetOffsetX: 0, targetOffsetY: 0,
            col: col, row: row,
            phaseX: Math.random() * Math.PI * 2,
            phaseY: Math.random() * Math.PI * 2
          });
        }
      }
      initTraces();
    }

    function getDot(row, col) {
      if (row < 0 || row >= rows || col < 0 || col >= cols) return null;
      return dots[row * cols + col];
    }

    function downNeighbors(row, col) {
      var isOdd = row % 2 === 1;
      if (isOdd) return [{ row: row + 1, col: col }, { row: row + 1, col: col + 1 }];
      return [{ row: row + 1, col: col - 1 }, { row: row + 1, col: col }];
    }

    function buildRoute(startCol) {
      var route = [];
      var col = startCol;
      for (var row = 0; row < rows; row++) {
        route.push({ row: row, col: col });
        if (Math.random() < TURN_CHANCE && row < rows - 1) {
          var dir = Math.random() < 0.5 ? -1 : 1;
          var newCol = col + dir;
          if (newCol >= 0 && newCol < cols) { route.push({ row: row, col: newCol }); col = newCol; }
        }
        if (row < rows - 1) {
          var dns = downNeighbors(row, col);
          var pick = dns[Math.random() < 0.5 ? 0 : 1];
          if (pick.col >= 0 && pick.col < cols) col = pick.col;
          else col = dns[0].col >= 0 && dns[0].col < cols ? dns[0].col : dns[1].col;
        }
      }
      return route;
    }

    var recentSpawnCols = [];
    function pickSpawnCol() {
      var MIN_DIST = 8;
      for (var a = 0; a < 200; a++) {
        var col = Math.floor(Math.random() * cols);
        var bad = false;
        for (var p = 0; p < recentSpawnCols.length; p++) {
          if (Math.abs(col - recentSpawnCols[p]) < MIN_DIST) { bad = true; break; }
        }
        if (!bad) { recentSpawnCols.push(col); if (recentSpawnCols.length > 3) recentSpawnCols.shift(); return col; }
      }
      var col2 = Math.floor(Math.random() * cols);
      recentSpawnCols.push(col2); if (recentSpawnCols.length > 3) recentSpawnCols.shift();
      return col2;
    }

    function spawnTrace() {
      var col = pickSpawnCol();
      return { route: buildRoute(col), step: 0, progress: 0, speed: PATH_SPEED * (0.5 + Math.random() * 0.8), segments: [] };
    }

    function initTraces() {
      recentSpawnCols = []; traces = [];
      for (var i = 0; i < PATH_COUNT; i++) {
        var t = spawnTrace();
        t.step = Math.floor(Math.random() * t.route.length * 0.8);
        traces.push(t);
      }
    }

    function updateTraces() {
      for (var i = 0; i < traces.length; i++) {
        var t = traces[i];
        if (t.step < t.route.length - 1) {
          var from = t.route[t.step], to = t.route[t.step + 1];
          var d1 = getDot(from.row, from.col), d2 = getDot(to.row, to.col);
          var stepDist = H_SPACING;
          if (d1 && d2) { var dx = d2.baseX - d1.baseX, dy = d2.baseY - d1.baseY; stepDist = Math.sqrt(dx * dx + dy * dy) || H_SPACING; }
          t.progress += t.speed / stepDist;
          if (t.progress >= 1) { t.segments.push({ fromRow: from.row, fromCol: from.col, toRow: to.row, toCol: to.col, alpha: SEGMENT_PEAK_ALPHA }); t.progress -= 1; t.step++; }
        } else { traces[i] = spawnTrace(); }
        for (var j = t.segments.length - 1; j >= 0; j--) { t.segments[j].alpha -= SEGMENT_FADE; if (t.segments[j].alpha <= 0) t.segments.splice(j, 1); }
      }
    }

    function resize() {
      var dpr = window.devicePixelRatio || 1;
      canvas.width = window.innerWidth * dpr;
      canvas.height = window.innerHeight * dpr;
      canvas.style.width = window.innerWidth + 'px';
      canvas.style.height = window.innerHeight + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      createDots();
    }

    function onMouseMove(e) { mouseX = e.clientX; mouseY = e.clientY; mouseActive = true; }
    function onMouseLeave() { mouseActive = false; mouseX = -9999; mouseY = -9999; }
    function onBgClick() { clickTime = performance.now(); clickActive = true; }

    function update(now) {
      var clickFactor = 0;
      if (clickActive) { var el = now - clickTime; if (el < CLICK_DURATION) { clickFactor = 1 - el / CLICK_DURATION; clickFactor *= clickFactor; } else clickActive = false; }
      var irSq = INFLUENCE_RADIUS * INFLUENCE_RADIUS;
      for (var i = 0; i < dots.length; i++) {
        var dot = dots[i];
        var ax = Math.sin(now * AMBIENT_SPEED + dot.phaseX) * AMBIENT_AMPLITUDE;
        var ay = Math.cos(now * AMBIENT_SPEED * 0.7 + dot.phaseY) * AMBIENT_AMPLITUDE;
        var dx2 = 0, dy2 = 0;
        if (mouseActive) {
          var ddx = dot.baseX - mouseX, ddy = dot.baseY - mouseY, dSq = ddx * ddx + ddy * ddy;
          if (dSq < irSq && dSq > 0.01) { var d = Math.sqrt(dSq), p = 1 - d / INFLUENCE_RADIUS, s = p * p * MAX_DISPLACEMENT, b = 1 - 2 * clickFactor; dx2 = ddx / d * s * b; dy2 = ddy / d * s * b; }
        }
        dot.targetOffsetX = dx2 + ax; dot.targetOffsetY = dy2 + ay;
        dot.offsetX += (dot.targetOffsetX - dot.offsetX) * LERP_SPEED;
        dot.offsetY += (dot.targetOffsetY - dot.offsetY) * LERP_SPEED;
      }
      updateTraces();
    }

    function drawHex(cx, cy, alpha) {
      ctx.beginPath(); ctx.moveTo(cx + hexVerts[0].x, cy + hexVerts[0].y);
      for (var i = 1; i < 6; i++) ctx.lineTo(cx + hexVerts[i].x, cy + hexVerts[i].y);
      ctx.closePath(); ctx.strokeStyle = ac(alpha.toFixed(4)); ctx.lineWidth = 0.5; ctx.stroke();
    }

    function draw() {
      var w = window.innerWidth, h = window.innerHeight;
      ctx.clearRect(0, 0, w, h);
      for (var i = 0; i < dots.length; i++) {
        var dot = dots[i], x = dot.baseX + dot.offsetX, y = dot.baseY + dot.offsetY;
        var disp = Math.sqrt(dot.offsetX * dot.offsetX + dot.offsetY * dot.offsetY);
        var ed = Math.max(0, disp - AMBIENT_AMPLITUDE), dn = Math.min(ed / MAX_DISPLACEMENT, 1);
        drawHex(x, y, HEX_LINE_REST + (HEX_LINE_ACTIVE - HEX_LINE_REST) * dn);
        ctx.beginPath(); ctx.arc(x, y, DOT_RADIUS, 0, Math.PI * 2);
        ctx.fillStyle = ac((0.06 + 0.24 * dn).toFixed(3)); ctx.fill();
      }
      for (var ti = 0; ti < traces.length; ti++) {
        var t = traces[ti];
        for (var si = 0; si < t.segments.length; si++) {
          var seg = t.segments[si];
          var d1 = getDot(seg.fromRow, seg.fromCol), d2 = getDot(seg.toRow, seg.toCol);
          if (!d1 || !d2) continue;
          ctx.beginPath(); ctx.moveTo(d1.baseX + d1.offsetX, d1.baseY + d1.offsetY);
          ctx.lineTo(d2.baseX + d2.offsetX, d2.baseY + d2.offsetY);
          ctx.strokeStyle = ac(seg.alpha.toFixed(4)); ctx.lineWidth = 2.5; ctx.stroke();
        }
        if (t.step < t.route.length - 1) {
          var from = t.route[t.step], to = t.route[t.step + 1];
          var fd1 = getDot(from.row, from.col), fd2 = getDot(to.row, to.col);
          if (fd1 && fd2) {
            var x1 = fd1.baseX + fd1.offsetX, y1 = fd1.baseY + fd1.offsetY, x2 = fd2.baseX + fd2.offsetX, y2 = fd2.baseY + fd2.offsetY;
            ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x1 + (x2 - x1) * t.progress, y1 + (y2 - y1) * t.progress);
            ctx.strokeStyle = ac(SEGMENT_PEAK_ALPHA); ctx.lineWidth = 2.5; ctx.stroke();
          }
        }
      }
    }

    function loop(now) { update(now); draw(); animId = requestAnimationFrame(loop); }

    // Cleanup previous instance if any
    if (window._hexWarpCleanup) window._hexWarpCleanup();

    resize();
    window.addEventListener('resize', resize);
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseleave', onMouseLeave);
    window.addEventListener('click', onBgClick);
    animId = requestAnimationFrame(loop);

    // Store cleanup function
    window._hexWarpCleanup = function () {
      if (animId) cancelAnimationFrame(animId);
      window.removeEventListener('resize', resize);
      window.removeEventListener('mousemove', onMouseMove);
      window.removeEventListener('mouseleave', onMouseLeave);
      window.removeEventListener('click', onBgClick);
    };
  }

  // Initialize reveal animations
  function initReveals() {
    var obs = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('visible');
          obs.unobserve(entry.target);
        }
      });
    }, { threshold: 0.1 });
    document.querySelectorAll('.reveal:not(.visible)').forEach(function (el) { obs.observe(el); });
  }

  // Auto-init
  function init() {
    initHexWarp();
    initReveals();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Re-init reveals after SPA navigation
  document.addEventListener('sk:navigation', function () {
    initReveals();
  });
})();
