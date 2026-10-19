/* Homepage motion: hero layers aligned to the artwork, star field, parallax,
   chat demo typing and the live-voice caption. Everything stops when off screen
   and is skipped for visitors who prefer reduced motion. */
(function () {
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var stage = document.querySelector('.hero-stage');
  var layer = document.getElementById('stage-layer');
  var hero = document.querySelector('.hero');
  if (!stage || !layer) return;

  /* Positions are percentages of each picture, measured on the artwork itself. */
  var VARIANTS = {
    wide: {
      ratio: 2006 / 784, moon: [73.3, 16.3], waterH: 19,
      glows: [[6.6, 77.3, 2.4], [8.9, 82.3, 2.2], [52, 80.6, 2.2], [57.5, 81.1, 2.2], [62.4, 81.6, 2.4], [76, 73, 2.2], [87.7, 83.5, 2.6]],
      big: [[90, 40, 26], [17, 42, 28]]
    },
    tall: {
      ratio: 940 / 1672, moon: [78.7, 21.1], waterH: 29,
      glows: [[5.1, 71.7, 7], [92, 72.2, 7], [64.5, 70.9, 6], [56, 69.8, 5]],
      big: [[91.5, 52.6, 60], [10, 45, 64]]
    }
  };
  var current = '';

  function build(name) {
    var v = VARIANTS[name];
    layer.innerHTML = '';
    var img = document.createElement('img');
    img.alt = ''; img.decoding = 'async'; img.fetchPriority = 'high';
    img.src = name === 'tall' ? '/static/img/hero-tall.webp' : '/static/img/hero-wide.webp';
    layer.appendChild(img);

    v.big.forEach(function (g) {
      var d = document.createElement('i'); d.className = 'glow big';
      d.style.cssText = 'left:' + g[0] + '%;top:' + g[1] + '%;--s:' + g[2] + '%;--t:' + (5 + Math.random() * 3).toFixed(1) + 's;--dl:-' + (Math.random() * 4).toFixed(1) + 's';
      layer.appendChild(d);
    });
    v.glows.forEach(function (g) {
      var d = document.createElement('i'); d.className = 'glow';
      d.style.cssText = 'left:' + g[0] + '%;top:' + g[1] + '%;--s:' + g[2] + '%;--t:' + (2.4 + Math.random() * 2.4).toFixed(1) + 's;--dl:-' + (Math.random() * 3).toFixed(1) + 's';
      layer.appendChild(d);
    });
    var w = document.createElement('div'); w.className = 'water'; w.style.setProperty('--wh', v.waterH + '%'); layer.appendChild(w);
    current = name;
  }

  /* Size the picture like background-size: cover so the % positions stay true. */
  function layout() {
    var W = stage.clientWidth, H = stage.clientHeight;
    var name = W <= 760 ? 'tall' : 'wide';
    if (name !== current) build(name);
    var ratio = VARIANTS[name].ratio;
    var w = W, h = W / ratio;
    if (h < H) { h = H; w = H * ratio; }
    var left = name === 'tall' ? (W - w) / 2 : 0; // wide: keep the pavilion side, crop the side behind the headline
    var top = name === 'tall' ? (H - h) / 2 : H - h; // wide: keep the water, tall: centre
    layer.style.width = w + 'px'; layer.style.height = h + 'px';
    layer.style.left = left + 'px'; layer.style.top = top + 'px';
    sizeStars(); sizeFx();
  }

  /* ── stars ── */
  var cv = document.getElementById('stars'), cx = cv && cv.getContext('2d');
  var stars = [], shoot = null, nextShoot = 0, dpr = 1, running = false, last = 0;
  function sizeStars() {
    if (!cv) return;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = stage.clientWidth * dpr; cv.height = stage.clientHeight * dpr;
    var n = stage.clientWidth <= 760 ? 70 : 140;
    stars = [];
    for (var i = 0; i < n; i++) {
      stars.push({
        x: Math.random(), y: Math.pow(Math.random(), 1.6) * 0.62,
        r: 0.4 + Math.random() * 1.1, a: 0.35 + Math.random() * 0.55,
        s: 0.6 + Math.random() * 1.8, p: Math.random() * 6.28
      });
    }
    draw(performance.now());
  }
  function draw(t) {
    if (!cx) return;
    var W = cv.width, H = cv.height;
    cx.clearRect(0, 0, W, H);
    for (var i = 0; i < stars.length; i++) {
      var s = stars[i], tw = reduce ? 1 : 0.55 + 0.45 * Math.sin(t / 1000 * s.s + s.p);
      cx.globalAlpha = s.a * tw;
      cx.fillStyle = '#fff6dc';
      cx.beginPath(); cx.arc(s.x * W, s.y * H, s.r * dpr, 0, 6.283); cx.fill();
    }
    if (shoot) {
      var k = (t - shoot.t0) / shoot.dur;
      if (k >= 1) { shoot = null; }
      else {
        var x = shoot.x + shoot.dx * k, y = shoot.y + shoot.dy * k;
        var g = cx.createLinearGradient(x, y, x - shoot.dx * 0.16, y - shoot.dy * 0.16);
        g.addColorStop(0, 'rgba(255,244,214,' + (0.95 * (1 - k)) + ')'); g.addColorStop(1, 'rgba(255,244,214,0)');
        cx.globalAlpha = 1; cx.strokeStyle = g; cx.lineWidth = 1.6 * dpr; cx.lineCap = 'round';
        cx.beginPath(); cx.moveTo(x, y); cx.lineTo(x - shoot.dx * 0.16, y - shoot.dy * 0.16); cx.stroke();
      }
    } else if (t > nextShoot && !reduce) {
      var W2 = cv.width, H2 = cv.height;
      shoot = { x: W2 * (0.45 + Math.random() * 0.45), y: H2 * (0.04 + Math.random() * 0.16), dx: -W2 * 0.28, dy: H2 * 0.16, t0: t, dur: 900 };
      nextShoot = t + 8000 + Math.random() * 7000;
    }
    cx.globalAlpha = 1;
  }

  /* ── fireflies that follow the pointer, sparks on click, and a gentle parallax ── */
  var fx = document.getElementById('fx'), fc = fx && fx.getContext('2d');
  var flies = [], sparks = [], dust = [], meteors = [];
  var ptr = { x: -999, y: -999, inside: false, lastMove: 0, tx: 0, ty: 0, cx: 0, cy: 0 };
  var sprite = (function () {
    var c = document.createElement('canvas'); c.width = c.height = 64;
    var g = c.getContext('2d'), r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    r.addColorStop(0, 'rgba(255,240,196,1)'); r.addColorStop(.18, 'rgba(255,214,128,.95)');
    r.addColorStop(.45, 'rgba(236,190,96,.32)'); r.addColorStop(1, 'rgba(236,190,96,0)');
    g.fillStyle = r; g.fillRect(0, 0, 64, 64); return c;
  })();
  var fxW = 0, fxH = 0, lastFx = 0;

  function sizeFx() {
    if (!fx) return;
    fxW = hero.clientWidth; fxH = hero.clientHeight;
    fx.width = fxW * dpr; fx.height = fxH * dpr;
    var n = fxW <= 760 ? 14 : 28;
    if (flies.length !== n) {
      flies = [];
      for (var i = 0; i < n; i++) flies.push({
        x: Math.random() * fxW, y: fxH * (0.15 + Math.random() * 0.7), vx: 0, vy: 0,
        ph: Math.random() * 6.28, sz: 14 + Math.random() * 18, tw: 1.2 + Math.random() * 1.6
      });
    }
  }
  function burst(x, y) {
    for (var i = 0; i < 18; i++) {
      var a = Math.random() * 6.283, sp = 70 + Math.random() * 220;
      sparks.push({ x: x, y: y, vx: Math.cos(a) * sp, vy: Math.sin(a) * sp - 40, life: 0, max: 0.8 + Math.random() * 0.8, r: 1 + Math.random() * 2 });
    }
    flies.forEach(function (f) {
      var dx = f.x - x, dy = f.y - y, d = Math.hypot(dx, dy) || 1;
      if (d < 260) { var k = (1 - d / 260) * 320; f.vx += dx / d * k; f.vy += dy / d * k; }
    });
    meteors.push({ x: x + 40, y: y - 30, dx: -Math.max(260, fxW * 0.22), dy: Math.max(150, fxH * 0.12), t: 0, dur: 0.85 });
  }
  function drawFx(t) {
    if (!fc) return;
    var dt = Math.min(0.05, (t - lastFx) / 1000 || 0.016); lastFx = t;
    fc.clearRect(0, 0, fx.width, fx.height);

    // parallax follows the pointer smoothly, and relaxes when it leaves
    var tx = ptr.inside ? ptr.tx : 0, ty = ptr.inside ? ptr.ty : 0;
    ptr.cx += (tx - ptr.cx) * 0.06; ptr.cy += (ty - ptr.cy) * 0.06;
    layer.style.translate = (-ptr.cx * 22).toFixed(2) + 'px ' + (-ptr.cy * 12).toFixed(2) + 'px';
    layer.style.scale = '1.05';
    if (cv) cv.style.translate = (-ptr.cx * 9).toFixed(2) + 'px ' + (-ptr.cy * 5).toFixed(2) + 'px';

    var active = ptr.inside && t - ptr.lastMove < 2500;
    flies.forEach(function (f) {
      f.vx += Math.sin(t / 1700 + f.ph) * 14 * dt; f.vy += Math.cos(t / 2100 + f.ph * 1.3) * 14 * dt;
      var near = 0;
      if (active) {
        var dx = ptr.x - f.x, dy = ptr.y - f.y, d = Math.hypot(dx, dy) || 1;
        if (d < 300) {
          near = 1 - d / 300;
          f.vx += (dx / d * 120 - dy / d * 90) * near * dt * 3; f.vy += (dy / d * 120 + dx / d * 90) * near * dt * 3;
          if (d < 80) { f.vx -= dx / d * 520 * dt * (1 - d / 80); f.vy -= dy / d * 520 * dt * (1 - d / 80); }
        }
      }
      var sp = Math.hypot(f.vx, f.vy), max = 130;
      if (sp > max) { f.vx *= max / sp; f.vy *= max / sp; }
      var damp = Math.pow(0.5, dt * 1.4); f.vx *= damp; f.vy *= damp;
      f.x += f.vx * dt; f.y += f.vy * dt;
      if (f.x < -30) f.x = fxW + 30; else if (f.x > fxW + 30) f.x = -30;
      if (f.y < fxH * 0.05) f.vy += 40 * dt; else if (f.y > fxH * 0.9) f.vy -= 40 * dt;
      var a = (0.45 + 0.55 * Math.sin(t / 380 * f.tw + f.ph)) * (0.7 + near * 0.6);
      var size = (f.sz * (1 + near * 0.5)) * dpr;
      fc.globalAlpha = Math.max(0.12, Math.min(1, a));
      fc.drawImage(sprite, f.x * dpr - size / 2, f.y * dpr - size / 2, size, size);
    });

    // dust trail behind the pointer
    for (var i = dust.length - 1; i >= 0; i--) {
      var p = dust[i]; p.life += dt; if (p.life > p.max) { dust.splice(i, 1); continue; }
      p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 18 * dt;
      fc.globalAlpha = (1 - p.life / p.max) * 0.85; fc.fillStyle = '#ffe9b0';
      fc.beginPath(); fc.arc(p.x * dpr, p.y * dpr, p.r * dpr, 0, 6.283); fc.fill();
    }
    // click sparks
    for (var j = sparks.length - 1; j >= 0; j--) {
      var q = sparks[j]; q.life += dt; if (q.life > q.max) { sparks.splice(j, 1); continue; }
      q.x += q.vx * dt; q.y += q.vy * dt; q.vy += 240 * dt; q.vx *= Math.pow(0.4, dt);
      fc.globalAlpha = 1 - q.life / q.max; fc.fillStyle = '#ffd98a';
      fc.beginPath(); fc.arc(q.x * dpr, q.y * dpr, q.r * dpr, 0, 6.283); fc.fill();
    }
    // shooting stars released by a click
    for (var m = meteors.length - 1; m >= 0; m--) {
      var s = meteors[m]; s.t += dt; var k = s.t / s.dur;
      if (k >= 1) { meteors.splice(m, 1); continue; }
      var x = (s.x + s.dx * k) * dpr, y = (s.y + s.dy * k) * dpr, tx2 = x - s.dx * 0.22 * dpr, ty2 = y - s.dy * 0.22 * dpr;
      var gr = fc.createLinearGradient(x, y, tx2, ty2);
      gr.addColorStop(0, 'rgba(255,244,214,' + (0.95 * (1 - k)) + ')'); gr.addColorStop(1, 'rgba(255,244,214,0)');
      fc.globalAlpha = 1; fc.strokeStyle = gr; fc.lineWidth = 1.8 * dpr; fc.lineCap = 'round';
      fc.beginPath(); fc.moveTo(x, y); fc.lineTo(tx2, ty2); fc.stroke();
    }
    fc.globalAlpha = 1;
  }
  function track(e) {
    var r = hero.getBoundingClientRect();
    ptr.x = e.clientX - r.left; ptr.y = e.clientY - r.top; ptr.inside = true;
    ptr.tx = ptr.x / r.width - 0.5; ptr.ty = ptr.y / r.height - 0.5;
    var now = performance.now();
    if (e.pointerType === 'mouse' && now - ptr.lastMove > 16 && dust.length < 60) {
      for (var i = 0; i < 2; i++) dust.push({ x: ptr.x + (Math.random() - 0.5) * 8, y: ptr.y + (Math.random() - 0.5) * 8, vx: (Math.random() - 0.5) * 24, vy: (Math.random() - 0.5) * 24, life: 0, max: 0.5 + Math.random() * 0.5, r: 0.8 + Math.random() * 1.6 });
    }
    ptr.lastMove = now;
  }
  if (fx && !reduce) {
    hero.addEventListener('pointermove', track, { passive: true });
    hero.addEventListener('pointerleave', function () { ptr.inside = false; });
    hero.addEventListener('pointerdown', function (e) {
      if (e.target.closest && e.target.closest('a,button')) return;
      track(e); burst(ptr.x, ptr.y);
    });
  }
  function loop(t) {
    if (!running) return;
    if (t - last > 33) { draw(t); last = t; }
    drawFx(t);
    requestAnimationFrame(loop);
  }
  function setRunning(on) {
    if (reduce || on === running) return;
    running = on; if (on) { nextShoot = performance.now() + 3500; requestAnimationFrame(loop); }
  }

  /* ── scroll parallax ── */
  var ticking = false;
  function onScroll() {
    if (ticking) return; ticking = true;
    requestAnimationFrame(function () {
      var y = window.scrollY, h = hero.offsetHeight;
      if (y < h * 1.2) {
        stage.style.transform = 'translate3d(0,' + (y * 0.22).toFixed(1) + 'px,0)';
        var copy = document.querySelector('.hero-copy');
        if (copy) { copy.style.transform = 'translate3d(0,' + (-y * 0.06).toFixed(1) + 'px,0)'; copy.style.opacity = String(Math.max(0, 1 - y / (h * 0.7))); }
      }
      ticking = false;
    });
  }

  layout();
  var rz; window.addEventListener('resize', function () { clearTimeout(rz); rz = setTimeout(layout, 120); });
  if (!reduce) {
    window.addEventListener('scroll', onScroll, { passive: true });
    new IntersectionObserver(function (e) { setRunning(e[0].isIntersecting && !document.hidden); }).observe(hero);
    document.addEventListener('visibilitychange', function () { setRunning(!document.hidden); });
  }

  /* ── chat demo: types itself the first time it scrolls into view ── */
  var demo = document.getElementById('demo');
  if (demo) {
    var meText = demo.getAttribute('data-me'), aiText = demo.getAttribute('data-ai');
    var me = demo.querySelector('.me .t'), ai = demo.querySelector('.ai .t'), dots = demo.querySelector('.typing-row');
    var words = function (el, txt) { el.innerHTML = txt.split(' ').map(function (w) { return '<span class="w">' + w + '</span>'; }).join(' '); return el.querySelectorAll('.w'); };
    var mw = words(me, meText), aw = words(ai, aiText);
    var seq = function (list, gap, done) {
      var i = 0; (function step() { if (i < list.length) { list[i++].classList.add('on'); setTimeout(step, gap); } else if (done) done(); })();
    };
    var played = false;
    var play = function () {
      if (played) return; played = true;
      if (reduce) { mw.forEach(function (n) { n.classList.add('on'); }); aw.forEach(function (n) { n.classList.add('on'); }); dots.style.display = 'none'; return; }
      demo.querySelector('.me').style.visibility = 'visible';
      seq(mw, 70, function () {
        dots.style.display = 'block';
        setTimeout(function () { dots.style.display = 'none'; demo.querySelector('.ai').style.visibility = 'visible'; seq(aw, 85); }, 1100);
      });
    };
    if (reduce) { demo.querySelector('.me').style.visibility = 'visible'; demo.querySelector('.ai').style.visibility = 'visible'; play(); }
    else new IntersectionObserver(function (e, o) { if (e[0].isIntersecting) { play(); o.disconnect(); } }, { threshold: 0.45 }).observe(demo);
  }

  /* ── live voice caption: text appears as if being spoken ── */
  var cap = document.getElementById('live-text');
  if (cap) {
    var lines = (cap.getAttribute('data-lines') || '').split('|');
    var li = 0, ci = 0, timer = null, shown = false;
    var tick = function () {
      var line = lines[li];
      if (ci <= line.length) { cap.textContent = line.slice(0, ci++); timer = setTimeout(tick, 52); }
      else { timer = setTimeout(function () { li = (li + 1) % lines.length; ci = 0; tick(); }, 2600); }
    };
    if (reduce) cap.textContent = lines[0];
    else new IntersectionObserver(function (e) {
      if (e[0].isIntersecting && !shown) { shown = true; tick(); }
      else if (!e[0].isIntersecting && shown) { shown = false; clearTimeout(timer); }
    }, { threshold: 0.4 }).observe(cap);
  }
})();
