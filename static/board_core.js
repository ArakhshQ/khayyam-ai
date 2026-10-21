/* Khayyam chalkboard — core: random/noise, hand-drawn geometry, icons, chalk texture.
   Everything here is resolution independent (logical board units); drawing to pixels happens in board.js. */
(function (global) {
  'use strict';
  var KB = global.KB = global.KB || {};

  /* ───────── random + noise ───────── */
  KB.rng = function (seed) {
    var a = seed >>> 0;
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      var t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  };
  KB.hash = function (s) {
    s = String(s); var h = 2166136261;
    for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  };
  KB.noise = function (seed) {                     // smooth 1-D value noise, range -1..1
    var r = KB.rng(seed), tab = [];
    for (var i = 0; i < 64; i++) tab.push(r() * 2 - 1);
    return function (x) {
      var i0 = Math.floor(x), f = x - i0, u = f * f * (3 - 2 * f);
      var a = tab[((i0 % 64) + 64) % 64], b = tab[(((i0 + 1) % 64) + 64) % 64];
      return a + (b - a) * u;
    };
  };
  KB.clamp = function (v, a, b) { return v < a ? a : v > b ? b : v; };
  KB.ease = function (t) { return t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; };

  /* ───────── colours ───────── */
  KB.COLORS = {
    white: '#f6f3e6', yellow: '#f4d86e', pink: '#f4a9bd', blue: '#8fcbf2',
    green: '#a9deA0'.toLowerCase(), orange: '#f4a85c'
  };
  KB.own = function (o, k) { return typeof k === 'string' && Object.prototype.hasOwnProperty.call(o, k); };
  KB.color = function (name) { if (typeof name === 'string' && /^#[0-9a-fA-F]{6}$/.test(name)) return name; return KB.own(KB.COLORS, name) ? KB.COLORS[name] : KB.COLORS.white; };

  /* ───────── polylines ───────── */
  // A polyline is {pts:[[x,y]...], cum:[0,...], len}
  KB.poly = function (pts) {
    var cum = [0], len = 0;
    for (var i = 1; i < pts.length; i++) {
      len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
      cum.push(len);
    }
    return { pts: pts, cum: cum, len: len };
  };
  // point at distance d along polyline
  KB.pointAt = function (pl, d) {
    if (!pl.pts.length) return [0, 0];
    if (d <= 0) return pl.pts[0];
    if (d >= pl.len) return pl.pts[pl.pts.length - 1];
    var lo = 0, hi = pl.cum.length - 1;
    while (hi - lo > 1) { var m = (lo + hi) >> 1; if (pl.cum[m] <= d) lo = m; else hi = m; }
    var t = (d - pl.cum[lo]) / ((pl.cum[hi] - pl.cum[lo]) || 1);
    return [pl.pts[lo][0] + (pl.pts[hi][0] - pl.pts[lo][0]) * t, pl.pts[lo][1] + (pl.pts[hi][1] - pl.pts[lo][1]) * t];
  };
  // resample a raw point list to roughly even spacing
  KB.resample = function (pts, step) {
    if (pts.length < 2) return pts.slice();
    var pl = KB.poly(pts), out = [], n = Math.max(2, Math.round(pl.len / step) + 1);
    for (var i = 0; i < n; i++) out.push(KB.pointAt(pl, pl.len * i / (n - 1)));
    return out;
  };
  // hand-made look: displace points sideways with slow noise, optional overshoot at both ends
  KB.wobble = function (pts, seed, amp, step) {
    amp = amp == null ? 1.1 : amp; step = step || 7;
    var src = KB.resample(pts, step), nz = KB.noise(seed), out = [], pl = KB.poly(src);
    for (var i = 0; i < src.length; i++) {
      var a = src[Math.max(0, i - 1)], b = src[Math.min(src.length - 1, i + 1)];
      var dx = b[0] - a[0], dy = b[1] - a[1], l = Math.hypot(dx, dy) || 1;
      var k = nz(pl.cum[i] / 38) * amp;
      out.push([src[i][0] - dy / l * k, src[i][1] + dx / l * k]);
    }
    return out;
  };
  KB.line = function (x1, y1, x2, y2, seed, amp) {
    var l = Math.hypot(x2 - x1, y2 - y1) || 1, ux = (x2 - x1) / l, uy = (y2 - y1) / l;
    var r = KB.rng(seed), o1 = (r() - .4) * Math.min(5, l * .06), o2 = (r() - .4) * Math.min(5, l * .06);
    return KB.wobble([[x1 - ux * o1, y1 - uy * o1], [x2 + ux * o2, y2 + uy * o2]], seed, amp == null ? Math.min(1.2, l / 60) : amp);
  };
  KB.ellipse = function (cx, cy, rx, ry, seed, a0, a1) {
    var r = KB.rng(seed), nz = KB.noise(seed + 7);
    a0 = a0 == null ? r() * Math.PI * 2 : a0;
    a1 = a1 == null ? a0 + Math.PI * 2 * 1.04 : a1;
    var n = Math.max(12, Math.round(Math.abs(a1 - a0) * Math.max(rx, ry) / 6)), pts = [];
    for (var i = 0; i <= n; i++) {
      var a = a0 + (a1 - a0) * i / n, k = 1 + nz(i / 6) * .008 + (i / n) * .007;
      pts.push([cx + Math.cos(a) * rx * k, cy + Math.sin(a) * ry * k]);
    }
    return pts;
  };
  KB.rectLines = function (x, y, w, h, seed) {
    return [KB.line(x, y, x + w, y, seed + 1), KB.line(x + w, y, x + w, y + h, seed + 2),
            KB.line(x + w, y + h, x, y + h, seed + 3), KB.line(x, y + h, x, y, seed + 4)];
  };
  KB.arrowHead = function (x1, y1, x2, y2, seed, size) {
    size = size || 14;
    var a = Math.atan2(y2 - y1, x2 - x1), s = .5;
    return [KB.wobble([[x2 - Math.cos(a - s) * size, y2 - Math.sin(a - s) * size], [x2, y2], [x2 - Math.cos(a + s) * size, y2 - Math.sin(a + s) * size]], seed, .5, 4)];
  };

  /* ───────── tiny SVG path reader (M L H V C Q Z, absolute and relative) ───────── */
  KB.parsePath = function (d, step) {
    step = step || 5;
    var tok = String(d || '').match(/[MLHVCQZmlhvcqz]|-?\d*\.?\d+(?:e-?\d+)?/g) || [];
    var i = 0, cmd = '', x = 0, y = 0, sx = 0, sy = 0, subs = [], cur = null;
    function num() { return parseFloat(tok[i++]); }
    function bez(p0, p1, p2, p3) {
      var n = Math.max(6, Math.round((Math.hypot(p1[0] - p0[0], p1[1] - p0[1]) + Math.hypot(p2[0] - p1[0], p2[1] - p1[1]) + Math.hypot(p3[0] - p2[0], p3[1] - p2[1])) / step)), out = [];
      for (var k = 1; k <= n; k++) {
        var t = k / n, u = 1 - t;
        out.push([u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
                  u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1]]);
      }
      return out;
    }
    var guard = 0;
    while (i < tok.length && guard++ < 4000) {
      if (/[A-Za-z]/.test(tok[i])) cmd = tok[i++];
      var rel = cmd === cmd.toLowerCase(), C = cmd.toUpperCase();
      if (C === 'Z') { if (cur) { cur.push([sx, sy]); x = sx; y = sy; } continue; }
      if (C === 'M') { var mx = num(), my = num(); if (rel) { mx += x; my += y; } x = sx = mx; y = sy = my; cur = [[x, y]]; subs.push(cur); cmd = rel ? 'l' : 'L'; continue; }
      if (!cur) { cur = [[x, y]]; subs.push(cur); }
      if (C === 'L') { var lx = num(), ly = num(); if (rel) { lx += x; ly += y; } cur.push([lx, ly]); x = lx; y = ly; }
      else if (C === 'H') { var hx = num(); if (rel) hx += x; cur.push([hx, y]); x = hx; }
      else if (C === 'V') { var vy = num(); if (rel) vy += y; cur.push([x, vy]); y = vy; }
      else if (C === 'C') {
        var a = [num(), num()], b = [num(), num()], c = [num(), num()];
        if (rel) { a = [a[0] + x, a[1] + y]; b = [b[0] + x, b[1] + y]; c = [c[0] + x, c[1] + y]; }
        Array.prototype.push.apply(cur, bez([x, y], a, b, c)); x = c[0]; y = c[1];
      } else if (C === 'Q') {
        var q = [num(), num()], e = [num(), num()];
        if (rel) { q = [q[0] + x, q[1] + y]; e = [e[0] + x, e[1] + y]; }
        var c1 = [x + 2 / 3 * (q[0] - x), y + 2 / 3 * (q[1] - y)], c2 = [e[0] + 2 / 3 * (q[0] - e[0]), e[1] + 2 / 3 * (q[1] - e[1])];
        Array.prototype.push.apply(cur, bez([x, y], c1, c2, e)); x = e[0]; y = e[1];
      } else { i++; }
    }
    return subs.filter(function (s) { return s.length > 1; });
  };

  /* ───────── chalk line-art icons, drawn in a 100 x 100 box ─────────
     each icon is a list of: a path string, or ['c',cx,cy,r], ['e',cx,cy,rx,ry], ['rays',cx,cy,r1,r2,n], ['star',cx,cy,R,r] */
  KB.ICONS = {
    apple: ['M50 32 C32 18 10 30 14 56 C18 82 36 94 50 87 C64 94 82 82 86 56 C90 30 68 18 50 32 Z', 'M50 32 C50 24 52 17 57 10', 'M57 16 C67 7 79 11 81 19 C71 24 63 22 57 16 Z'],
    book: ['M50 28 C38 19 20 19 7 25 L7 80 C20 74 38 74 50 83 Z', 'M50 28 C62 19 80 19 93 25 L93 80 C80 74 62 74 50 83 Z', 'M14 38 C23 35 32 35 42 39', 'M14 50 C23 47 32 47 42 51', 'M14 62 C23 59 32 59 42 63', 'M58 39 C68 35 77 35 86 38', 'M58 51 C68 47 77 47 86 50'],
    sun: [['c', 50, 50, 17], ['rays', 50, 50, 27, 42, 10]],
    moon: ['M64 12 C36 16 20 42 28 66 C36 90 68 94 86 76 C62 78 46 56 52 36 C55 26 60 19 64 12 Z', ['star', 78, 26, 7, 3]],
    star: [['star', 50, 52, 42, 18]],
    tree: ['M50 8 C30 6 14 24 22 42 C9 50 18 70 38 66 L62 66 C82 70 91 50 78 42 C86 24 70 6 50 8 Z', 'M44 94 L45 64', 'M56 94 L55 64', 'M50 66 L50 40', 'M50 52 L40 44'],
    house: ['M10 50 L50 14 L90 50', 'M19 44 L19 88 L81 88 L81 44', 'M41 88 L41 62 L59 62 L59 88', 'M26 54 L36 54 L36 66 L26 66 Z', 'M64 54 L74 54 L74 66 L64 66 Z'],
    car: ['M6 66 L12 50 C14 45 18 42 23 42 L58 42 C65 42 70 46 74 52 L89 56 C94 57 95 60 95 65 L95 72 L6 72 Z', 'M27 44 L33 54 L60 54 L55 44', ['c', 27, 74, 10], ['c', 74, 74, 10], ['c', 27, 74, 3], ['c', 74, 74, 3]],
    ball: [['c', 50, 50, 34], 'M32 38 C35 32 41 28 48 27'],
    basketball: [['c', 50, 50, 34], 'M16 50 L84 50', 'M50 16 L50 84', 'M26 28 C40 40 40 60 26 72', 'M74 28 C60 40 60 60 74 72'],
    drop: ['M50 8 C50 8 20 46 20 66 C20 83 34 93 50 93 C66 93 80 83 80 66 C80 46 50 8 50 8 Z', 'M36 66 C36 76 42 82 50 83'],
    cloud: ['M26 72 C9 72 7 51 24 47 C24 28 52 22 60 39 C74 31 92 43 86 59 C98 61 96 74 83 74 Z'],
    flower: [['c', 50, 38, 9], ['c', 50, 17, 11], ['c', 71, 32, 11], ['c', 64, 55, 11], ['c', 36, 55, 11], ['c', 29, 32, 11], 'M50 49 C48 66 52 78 50 94', 'M50 78 C38 78 30 70 28 62 C40 62 48 68 50 78 Z', 'M50 70 C62 70 70 62 72 54 C60 54 52 60 50 70 Z'],
    fish: ['M8 50 C24 24 56 22 72 44 L92 28 L92 72 L72 56 C56 78 24 76 8 50 Z', ['c', 28, 46, 3.5], 'M44 36 C48 46 48 56 44 64'],
    bird: ['M8 46 C22 30 38 36 50 54 C62 36 78 30 92 46 C78 44 62 54 50 70 C38 54 22 44 8 46 Z'],
    heart: ['M50 88 C8 58 8 20 31 19 C42 18 48 25 50 32 C52 25 58 18 69 19 C92 20 92 58 50 88 Z'],
    pencil: ['M13 88 L19 66 L67 18 L83 34 L35 82 Z', 'M58 27 L74 43', 'M19 66 L35 82', 'M13 88 L24 85'],
    cup: ['M20 30 L27 85 L67 85 L74 30 Z', 'M74 40 C94 40 94 68 70 68', 'M20 30 L74 30', 'M38 10 C32 16 44 20 38 24', 'M52 10 C46 16 58 20 52 24'],
    coin: [['c', 50, 50, 38], ['c', 50, 50, 28], 'M50 32 L50 68', 'M40 40 C40 34 60 34 60 42 C60 52 40 48 40 58 C40 66 60 66 60 60'],
    bread: ['M8 50 C8 26 28 18 50 18 C72 18 92 26 92 50 C92 74 72 82 50 82 C28 82 8 74 8 50 Z', ['c', 32, 44, 2.2], ['c', 50, 38, 2.2], ['c', 66, 46, 2.2], ['c', 40, 60, 2.2], ['c', 60, 62, 2.2], ['c', 28, 58, 2.2]],
    clock: [['c', 50, 50, 40], 'M50 50 L50 24', 'M50 50 L68 58', ['c', 50, 50, 2.5], 'M50 12 L50 18', 'M50 82 L50 88', 'M12 50 L18 50', 'M82 50 L88 50'],
    person: [['c', 50, 20, 11], 'M50 31 L50 62', 'M50 40 L30 54', 'M50 40 L70 54', 'M50 62 L34 90', 'M50 62 L66 90'],
    bulb: ['M50 8 C26 8 16 28 24 46 C30 58 38 62 38 74 L62 74 C62 62 70 58 76 46 C84 28 74 8 50 8 Z', 'M38 82 L62 82', 'M42 90 L58 90', 'M42 52 L50 40 L58 52'],
    mountain: ['M4 88 L36 32 L52 58 L66 38 L96 88 Z', 'M28 46 L36 52 L44 44', 'M60 48 L66 54 L74 48'],
    check: ['M16 54 L40 78 L86 24'],
    cross: ['M22 22 L78 78', 'M78 22 L22 78'],
    arrowr: ['M10 50 L88 50', 'M70 30 L90 50 L70 70'],
    ruler: ['M6 38 L94 38 L94 62 L6 62 Z', 'M20 38 L20 50', 'M34 38 L34 56', 'M48 38 L48 50', 'M62 38 L62 56', 'M76 38 L76 50'],
    triangle: ['M50 12 L92 86 L8 86 Z'],
    square: ['M14 14 L86 14 L86 86 L14 86 Z'],
    circle: [['c', 50, 50, 38]],
    magnet: ['M22 14 L22 54 C22 82 78 82 78 54 L78 14 L58 14 L58 54 C58 62 42 62 42 54 L42 14 Z', 'M22 28 L42 28', 'M58 28 L78 28'],
    seed: ['M50 94 L50 56', 'M50 62 C32 62 20 50 18 34 C36 34 48 44 50 62 Z', 'M50 52 C66 52 78 40 80 24 C64 24 52 34 50 52 Z']
  };
  KB.iconStrokes = function (name, seed) {          // -> array of raw point lists in the 0..100 box
    if (!KB.own(KB.ICONS, name)) return null;
    var def = KB.ICONS[name];
    var out = [], r = KB.rng(seed);
    def.forEach(function (it, k) {
      if (typeof it === 'string') {
        KB.parsePath(it, 4).forEach(function (s) { out.push(KB.wobble(s, seed + k * 11, .9, 5)); });
      } else if (it[0] === 'c') out.push(KB.ellipse(it[1], it[2], it[3], it[3], seed + k * 11, r() * 6.28));
      else if (it[0] === 'e') out.push(KB.ellipse(it[1], it[2], it[3], it[4], seed + k * 11, r() * 6.28));
      else if (it[0] === 'rays') {
        for (var i = 0; i < it[5]; i++) {
          var a = Math.PI * 2 * i / it[5];
          out.push(KB.line(it[1] + Math.cos(a) * it[3], it[2] + Math.sin(a) * it[3], it[1] + Math.cos(a) * it[4], it[2] + Math.sin(a) * it[4], seed + i * 3, .4));
        }
      } else if (it[0] === 'star') {
        var p = [];
        for (var j = 0; j <= 10; j++) { var ang = -Math.PI / 2 + Math.PI * j / 5, rr = j % 2 ? it[4] : it[3]; p.push([it[1] + Math.cos(ang) * rr, it[2] + Math.sin(ang) * rr]); }
        out.push(KB.wobble(p, seed + k, .6, 4));
      }
    });
    return out;
  };

  /* ───────── chalk grain tile (white with holes); tinted per colour by caller ───────── */
  KB.makeGrain = function (hex) {
    var c = document.createElement('canvas'); c.width = c.height = 144;
    var x = c.getContext('2d'), img = x.createImageData(144, 144), d = img.data, r = KB.rng(90210);
    var R = parseInt(hex.slice(1, 3), 16), G = parseInt(hex.slice(3, 5), 16), B = parseInt(hex.slice(5, 7), 16);
    for (var i = 0; i < d.length; i += 4) {
      var v = r(), a = 255;
      if (v < .07) a = 70 + r() * 70;            // pits
      else if (v < .30) a = 190 + r() * 45;      // thin spots
      d[i] = R; d[i + 1] = G; d[i + 2] = B; d[i + 3] = a;
    }
    // a few larger scratches so strokes look dragged over a rough board
    x.putImageData(img, 0, 0);
    x.globalCompositeOperation = 'destination-out';
    for (var s = 0; s < 22; s++) { x.fillStyle = 'rgba(0,0,0,' + (.25 + r() * .4) + ')'; x.fillRect(r() * 144, r() * 144, 1 + r() * 9, 1 + r() * 1.4); }
    return c;
  };
})(window);