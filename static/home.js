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
      ratio: 2006 / 784, moon: [73.3, 16.3], ringW: 17, waterH: 19,
      glows: [[6.6, 77.3, 2.4], [8.9, 82.3, 2.2], [52, 80.6, 2.2], [57.5, 81.1, 2.2], [62.4, 81.6, 2.4], [76, 73, 2.2], [87.7, 83.5, 2.6]],
      big: [[90, 40, 26], [17, 42, 28]]
    },
    tall: {
      ratio: 940 / 1672, moon: [78.7, 21.1], ringW: 62, waterH: 29,
      glows: [[5.1, 71.7, 7], [92, 72.2, 7], [64.5, 70.9, 6], [56, 69.8, 5]],
      big: [[91.5, 52.6, 60], [10, 45, 64]]
    }
  };
  var current = '';

  function svgRings() {
    var ns = 'http://www.w3.org/2000/svg';
    var s = document.createElementNS(ns, 'svg');
    s.setAttribute('viewBox', '-100 -100 200 200'); s.setAttribute('class', 'rings'); s.setAttribute('aria-hidden', 'true');
    s.innerHTML =
      '<g class="r1"><ellipse rx="96" ry="34" transform="rotate(-24)" fill="none" stroke="rgba(236,201,119,.55)" stroke-width=".7"/><circle cx="92" cy="-6" r="1.8" fill="#ecc977" transform="rotate(-24)"/></g>' +
      '<g class="r2"><ellipse rx="78" ry="26" transform="rotate(18)" fill="none" stroke="rgba(236,201,119,.35)" stroke-width=".6"/><circle cx="-76" cy="4" r="1.3" fill="#fff4d6" transform="rotate(18)"/></g>' +
      '<g class="r3"><ellipse rx="60" ry="60" fill="none" stroke="rgba(183,204,195,.22)" stroke-width=".5" stroke-dasharray="1.5 3"/></g>';
    return s;
  }

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
    var r = svgRings(); r.style.left = v.moon[0] + '%'; r.style.top = v.moon[1] + '%'; r.style.setProperty('--rw', v.ringW + '%');
    layer.appendChild(r);
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
    sizeStars();
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
  function loop(t) {
    if (!running) return;
    if (t - last > 33) { draw(t); last = t; }
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
