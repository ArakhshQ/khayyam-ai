/* Khayyam chalkboard — lesson model.
   Turns a lesson script (scenes of actions) into positioned elements with fixed geometry and timing.
   Nothing here touches pixels, so the same model can be replayed, seeked and re-drawn at any size.

   Lesson:  { v:1, title, digits:'fa', scenes:[ { say:'spoken line', clear:false, do:[ action, ... ] } ] }
   Boxes are [x, y, w, h] in percent of the board; shape coordinates are 0..100 inside the (square-fitted) box. */
(function (global) {
  'use strict';
  var KB = global.KB;
  var W = 1000;

  var SIZES = { xl: 58, l: 46, m: 37, s: 30, xs: 25 };
  var ZONES = {
    wide: { title: [6, 3, 88, 13], main: [6, 18, 88, 62], b: [6, 18, 43, 62], a: [51, 18, 43, 62], bottom: [6, 81, 88, 14], full: [4, 4, 92, 92], center: [22, 22, 56, 56] },
    tall: { title: [6, 2, 88, 10], main: [6, 14, 88, 70], a: [6, 14, 88, 34], b: [6, 50, 88, 34], bottom: [6, 86, 88, 11], full: [4, 3, 92, 94], center: [10, 20, 80, 60] }
  };
  ZONES.wide.left = ZONES.wide.b; ZONES.wide.right = ZONES.wide.a; ZONES.tall.left = ZONES.tall.b; ZONES.tall.right = ZONES.tall.a;
  ZONES.wide.first = ZONES.wide.a; ZONES.wide.second = ZONES.wide.b; ZONES.tall.first = ZONES.tall.a; ZONES.tall.second = ZONES.tall.b;

  var FAM = { body: 'Vazirmatn, system-ui, sans-serif', display: 'Amiri, Vazirmatn, serif' };
  var RTL_RE = /[֐-ࣿיִ-﷿ﹰ-﻿]/;
  var SPEECH_CPS = 12.5;

  var mctx = null;
  function measure(str, size, fam, weight) {
    if (!mctx) mctx = document.createElement('canvas').getContext('2d');
    mctx.font = (weight || 500) + ' ' + size + 'px ' + (FAM[fam] || fam || FAM.body);
    return mctx.measureText(str).width;
  }
  KB.measure = measure;

  function num(v, d, lo, hi) { v = +v; if (!isFinite(v)) v = d; return KB.clamp(v, lo == null ? -1e4 : lo, hi == null ? 1e4 : hi); }
  function str(v, n) { return typeof v === 'string' ? v.slice(0, n || 400) : (v == null ? '' : String(v).slice(0, n || 400)); }
  function pct(b, P) { return [b[0] * W / 100, b[1] * P.H / 100, b[2] * W / 100, b[3] * P.H / 100]; }

  /* ───────── text layout ───────── */
  function wrapText(s, size, maxW, fam, weight) {
    var out = [], paras = String(s).split('\n');
    paras.forEach(function (p) {
      var words = p.split(/\s+/).filter(Boolean), cur = '';
      if (!words.length) { out.push(''); return; }
      words.forEach(function (w) {
        var t = cur ? cur + ' ' + w : w;
        if (cur && measure(t, size, fam, weight) > maxW) { out.push(cur); cur = w; } else cur = t;
      });
      out.push(cur);
    });
    return out;
  }

  /* ───────── builder ───────── */
  KB.buildModel = function (lesson, profile) {
    var P = profile || { wide: true, H: 580, ts: 1 };
    var Z = P.wide ? ZONES.wide : ZONES.tall;
    var digits = lesson.digits === 'en' ? 'en' : 'fa';
    var ids = Object.create(null);      // id -> element
    var warnings = [];
    var cursors = {};                   // zone -> y used
    var auto = 0;
    var model = { W: W, H: P.H, profile: P, scenes: [], ids: ids, warnings: warnings, digits: digits, title: str(lesson.title, 120) };

    function newId(a, pre) { var id = str(a.id, 24).replace(/[^\w-]/g, ''); if (!id || ids[id]) id = pre + (++auto); return id; }
    function fa(s) { return digits === 'fa' ? KB.faDigits(s) : s; }

    function resolveBox(a, defZone) {
      if (Array.isArray(a.box) && a.box.length === 4) return pct(a.box.map(function (v) { return num(v, 0, 0, 100); }), P);
      var z = KB.own(Z, a.zone) ? Z[a.zone] : (defZone ? Z[defZone] : null);
      if (z) return pct(z, P);
      return pct(Z.main, P);
    }
    function fitSquare(b) { var s = Math.min(b[2], b[3]) / 100; return { s: s, ox: b[0] + (b[2] - 100 * s) / 2, oy: b[1] + (b[3] - 100 * s) / 2 }; }
    function lp(f, x, y) { return [f.ox + x * f.s, f.oy + y * f.s]; }

    function mkEl(a, kind, pre) {
      var el = { id: newId(a, pre), kind: kind, items: [], dur: 0, bbox: null, color: KB.color(a.color), seed: KB.hash(str(a.id) + kind + (auto + 1)) };
      ids[el.id] = el; return el;
    }
    function addStroke(el, pts, o) {
      o = o || {}; var pl = KB.poly(pts); if (pl.len < 1) return;
      var d = Math.max(.04, pl.len / (o.speed || 380));
      el.items.push({ t: 's', pl: pl, color: o.color || el.color, w: o.w || 3.4, t0: el.dur, d: d });
      el.dur += d + .04;
    }
    function bboxOfPts(list) {
      var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      list.forEach(function (pts) { pts.forEach(function (p) { x0 = Math.min(x0, p[0]); y0 = Math.min(y0, p[1]); x1 = Math.max(x1, p[0]); y1 = Math.max(y1, p[1]); }); });
      return x0 > x1 ? null : [x0, y0, x1 - x0, y1 - y0];
    }
    function growBox(el, b) {
      if (!b) return; if (!el.bbox) { el.bbox = b.slice(); return; }
      var x0 = Math.min(el.bbox[0], b[0]), y0 = Math.min(el.bbox[1], b[1]), x1 = Math.max(el.bbox[0] + el.bbox[2], b[0] + b[2]), y1 = Math.max(el.bbox[1] + el.bbox[3], b[1] + b[3]);
      el.bbox = [x0, y0, x1 - x0, y1 - y0];
    }
    function strokesOf(el, list, o) { list.forEach(function (pts) { addStroke(el, pts, o); }); growBox(el, bboxOfPts(list)); }

    // shading with parallel chalk lines clipped to a polygon
    function hatch(poly, ang, gap, seed) {
      var out = [], c = Math.cos(ang), s = Math.sin(ang), x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
      poly.forEach(function (p) { x0 = Math.min(x0, p[0]); x1 = Math.max(x1, p[0]); y0 = Math.min(y0, p[1]); y1 = Math.max(y1, p[1]); });
      var cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, R = Math.hypot(x1 - x0, y1 - y0) / 2 + 2, k = 0;
      for (var o = -R; o <= R; o += gap, k++) {
        // line: points p = center + o*normal + t*dir
        var nx = -s, ny = c, ts = [];
        for (var i = 0; i < poly.length; i++) {
          var a = poly[i], b = poly[(i + 1) % poly.length];
          var da = (a[0] - cx) * nx + (a[1] - cy) * ny - o, db = (b[0] - cx) * nx + (b[1] - cy) * ny - o;
          if ((da < 0) !== (db < 0)) { var u = da / (da - db), px = a[0] + (b[0] - a[0]) * u, py = a[1] + (b[1] - a[1]) * u; ts.push((px - cx) * c + (py - cy) * s); }
        }
        ts.sort(function (p, q) { return p - q; });
        for (var j = 0; j + 1 < ts.length; j += 2) {
          var ta = ts[j] + 1.5, tb = ts[j + 1] - 1.5; if (tb - ta < 3) continue;
          var pa = [cx + nx * o + c * ta, cy + ny * o + s * ta], pb = [cx + nx * o + c * tb, cy + ny * o + s * tb];
          out.push(KB.line(pa[0], pa[1], pb[0], pb[1], seed + k * 5 + j, .5));
        }
      }
      return out;
    }

    /* ----- text and math sprites ----- */
    function buildText(a, pre) {
      var el = mkEl(a, 'text', pre || 't');
      var raw = str(a.t != null ? a.t : a.text, 600); if (!raw) return null;
      var sz = (typeof a.size === 'number' ? KB.clamp(a.size, 14, 90) : (KB.own(SIZES, a.size) ? SIZES[a.size] : SIZES.m)) * P.ts;
      var fam = a.font === 'display' || a.size === 'xl' ? 'display' : 'body', weight = a.bold || a.size === 'xl' ? 700 : 500;
      var rtl = RTL_RE.test(raw), box, zn = KB.own(Z, a.zone) ? a.zone : (Array.isArray(a.box) || Array.isArray(a.at) ? null : 'main');
      if (Array.isArray(a.at)) { var ax = num(a.at[0], 10, 0, 100) * W / 100, ay = num(a.at[1], 10, 0, 100) * P.H / 100; box = [ax, ay, Math.min(W - ax - 10, W * .5), P.H - ay - 10]; if (rtl && !a.align) { box = [Math.max(10, ax - W * .5), ay, Math.min(ax - 10, W * .5), P.H - ay - 10]; } }
      else box = resolveBox(a, 'main');
      var y = box[1];
      if (zn) { y += (cursors[zn] || 0); }
      var avail = Math.max(30, box[1] + box[3] - y), maxW = box[2], lines, lh, tries = 0, s = sz;
      for (;;) {
        lines = wrapText(raw, s, maxW, fam, weight); lh = s * (RTL_RE.test(raw) ? 1.72 : 1.5);
        if (lines.length * lh <= avail || tries++ > 8 || s <= sz * .62) break;
        s *= .92;
      }
      var overflow = lines.length * lh > avail + 2;
      if (overflow) {
        warnings.push('text overflow: ' + raw.slice(0, 24));
        if (zn && (cursors[zn] || 0) > 0) { delete ids[el.id]; return { overflow: true, el: null }; }
      }
      var align = a.align || (rtl ? 'start' : 'start'), lineObjs = [], maxLW = 0;
      lines.forEach(function (ln, i) {
        var w = measure(ln, s, fam, weight); maxLW = Math.max(maxLW, w);
        var lr = RTL_RE.test(ln) || (!ln && rtl);
        var x;
        if (align === 'center') x = box[0] + (box[2] - w) / 2; else if (lr) x = box[0] + box[2] - w; else x = box[0];
        if (align === 'end') x = lr ? box[0] : box[0] + box[2] - w;
        lineObjs.push({ s: ln, x: x, y: y + i * lh, w: w, rtl: lr });
      });
      var chars = raw.replace(/\s/g, '').length, d = Math.max(.5, chars / 11);
      var it = { t: 'x', mode: 'text', lines: lineObjs, size: s, lh: lh, fam: fam, weight: weight, color: el.color, t0: 0, d: d, cat: chars };
      el.items.push(it); el.dur = d;
      var bx = lineObjs.length ? Math.min.apply(null, lineObjs.map(function (l) { return l.x; })) : box[0];
      el.bbox = [bx, y, maxLW, lines.length * lh];
      if (zn) cursors[zn] = (cursors[zn] || 0) + lines.length * lh + s * .35;
      el.linesH = lh;
      return { el: el };
    }

    function buildMath(a) {
      var el = mkEl(a, 'math', 'm');
      var src = str(a.m != null ? a.m : a.t, 300); if (!src) return null;
      var sz = (typeof a.size === 'number' ? KB.clamp(a.size, 16, 110) : (KB.own(SIZES, a.size) ? SIZES[a.size] : SIZES.l) * 1.3) * P.ts;
      var lay = KB.mathLayout(src, sz, digits, function (s, z) { return measure(s, z, 'body', 500); });
      var box, zn = KB.own(Z, a.zone) ? a.zone : (Array.isArray(a.box) || Array.isArray(a.at) ? null : 'main');
      if (Array.isArray(a.at)) { box = [num(a.at[0], 10, 0, 100) * W / 100, num(a.at[1], 10, 0, 100) * P.H / 100, lay.w, lay.asc + lay.desc]; }
      else box = resolveBox(a, 'main');
      var y = box[1] + (zn ? (cursors[zn] || 0) : 0);
      var x = (a.align === 'start' || Array.isArray(a.at)) ? box[0] : box[0] + (box[2] - lay.w) / 2;
      if (lay.w > box[2] && !Array.isArray(a.at)) { warnings.push('math too wide: ' + src.slice(0, 20)); }
      var d = .8 + lay.w / 260;
      el.items.push({ t: 'x', mode: 'math', lay: lay, x: x, y: y + lay.asc, size: sz, color: el.color, t0: 0, d: d, boxW: lay.w, boxH: lay.asc + lay.desc });
      el.dur = d; el.bbox = [x, y, lay.w, lay.asc + lay.desc];
      if (zn) cursors[zn] = (cursors[zn] || 0) + lay.asc + lay.desc + sz * .5;
      return { el: el };
    }

    /* ----- shapes ----- */
    function buildShape(a) {
      var op = a.op, f = fitSquare(resolveBox(a, 'center')), col = KB.color(a.color), el = mkEl(a, op, 'f');
      var sd = el.seed, wd = num(a.width, 3.4, 1.5, 8);
      function L(x, y) { return lp(f, num(x, 0, -50, 150), num(y, 0, -50, 150)); }
      var o = { color: col, w: wd };
      if (op === 'line' || op === 'arrow') {
        var p1 = L(a.from ? a.from[0] : 10, a.from ? a.from[1] : 50), p2 = L(a.to ? a.to[0] : 90, a.to ? a.to[1] : 50);
        strokesOf(el, [KB.line(p1[0], p1[1], p2[0], p2[1], sd)], o);
        if (op === 'arrow') strokesOf(el, KB.arrowHead(p1[0], p1[1], p2[0], p2[1], sd + 9, 17), o);
        if (a.both) strokesOf(el, KB.arrowHead(p2[0], p2[1], p1[0], p1[1], sd + 19, 17), o);
      } else if (op === 'rect') {
        var x = num(a.x, 10, -50, 150), y = num(a.y, 10, -50, 150), w = num(a.w, 80, 1, 200), h = num(a.h, 80, 1, 200);
        var q = L(x, y);
        if (a.fill) strokesOf(el, hatch([q, [q[0] + w * f.s, q[1]], [q[0] + w * f.s, q[1] + h * f.s], [q[0], q[1] + h * f.s]], -.8, 7, sd), { color: col, w: 3 });
        strokesOf(el, KB.rectLines(q[0], q[1], w * f.s, h * f.s, sd), o);
      } else if (op === 'circle' || op === 'ellipse') {
        var c = L(a.cx != null ? a.cx : 50, a.cy != null ? a.cy : 50), r = num(a.r, 35, 1, 150), rx = (op === 'ellipse' ? num(a.rx, r, 1, 150) : r) * f.s, ry = (op === 'ellipse' ? num(a.ry, r, 1, 150) : r) * f.s;
        if (a.fill) {
          var pp = KB.ellipse(c[0], c[1], rx, ry, sd, 0, Math.PI * 2).slice(0, -1);
          strokesOf(el, hatch(pp, -.8, 7, sd), { color: col, w: 3 });
        }
        strokesOf(el, [KB.ellipse(c[0], c[1], rx, ry, sd)], o);
      } else if (op === 'poly') {
        var pts = (Array.isArray(a.pts) ? a.pts : []).slice(0, 40).map(function (p) { return L(p[0], p[1]); });
        if (pts.length < 2) return null;
        if (a.close !== false && pts.length > 2) pts.push(pts[0]);
        if (a.fill && pts.length > 3) strokesOf(el, hatch(pts.slice(0, -1), -.8, 7, sd), { color: col, w: 3 });
        var segs = [];
        for (var i = 0; i + 1 < pts.length; i++) segs.push(KB.line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], sd + i));
        strokesOf(el, segs, o);
      } else if (op === 'path') {
        var subs = KB.parsePath(str(a.d, 1200), 5).slice(0, 12);
        strokesOf(el, subs.map(function (s, i) { return KB.wobble(s.map(function (p) { return L(p[0], p[1]); }), sd + i, .8, 6); }), o);
      } else if (op === 'icon') {
        var raw = KB.iconStrokes(str(a.name, 20), sd); if (!raw) { warnings.push('unknown icon ' + a.name); return null; }
        var sc = num(a.scale, 1, .2, 3);
        var cx = 50 - 50 * sc, strokes = raw.map(function (pts) { return pts.map(function (p) { return L(50 + (p[0] - 50) * sc, 50 + (p[1] - 50) * sc); }); });
        strokesOf(el, strokes, { color: col, w: wd * Math.min(1.1, .55 + f.s * .5) });
      }
      return el.items.length ? { el: el } : null;
    }

    function buildPie(a) {
      var b = resolveBox(a, 'center'), f = fitSquare(b), el = mkEl(a, 'pie', 'p'), sd = el.seed;
      var parts = Math.round(num(a.parts, 4, 1, 24)), fill = Math.round(num(a.fill, 1, 0, parts));
      var cx = f.ox + 50 * f.s, cy = f.oy + 50 * f.s, R = 44 * f.s, col = KB.color(a.color), fcol = KB.color(a.fillColor || 'yellow');
      var a0 = -Math.PI / 2;
      // shading first, then outline, then the dividing lines
      for (var k = 0; k < fill; k++) {
        var s0 = a0 + Math.PI * 2 * k / parts, s1 = a0 + Math.PI * 2 * (k + 1) / parts, poly = [[cx, cy]], n = Math.max(6, Math.round((s1 - s0) * R / 8));
        for (var i = 0; i <= n; i++) poly.push([cx + Math.cos(s0 + (s1 - s0) * i / n) * R * .97, cy + Math.sin(s0 + (s1 - s0) * i / n) * R * .97]);
        hatch(poly, -.8 + k * .05, 7, sd + k * 31).forEach(function (pts) { addStroke(el, pts, { color: fcol, w: 3, speed: 600 }); });
      }
      strokesOf(el, [KB.ellipse(cx, cy, R, R, sd, a0, a0 + Math.PI * 2.05)], { color: col, w: 3.6 });
      if (parts > 1) for (var j = 0; j < parts; j++) { var an = a0 + Math.PI * 2 * j / parts; strokesOf(el, [KB.line(cx, cy, cx + Math.cos(an) * R, cy + Math.sin(an) * R, sd + 50 + j, .6)], { color: col, w: 3 }); }
      el.bbox = [cx - R, cy - R, R * 2, R * 2];
      return { el: el };
    }

    function buildBars(a) {
      var b = resolveBox(a, 'center'), el = mkEl(a, 'bars', 'b'), sd = el.seed, col = KB.color(a.color), fcol = KB.color(a.fillColor || 'yellow');
      var rows = (Array.isArray(a.rows) ? a.rows : [{ parts: 4, fill: 1 }]).slice(0, 6), n = rows.length;
      var rh = Math.min(b[3] / n * .62, 64), gap = (b[3] - rh * n) / Math.max(1, n - 1 || 1);
      if (n === 1) gap = 0;
      rows.forEach(function (r, ri) {
        var parts = Math.round(num(r.parts, 4, 1, 16)), fill = Math.round(num(r.fill, 1, 0, parts)), y = n === 1 ? b[1] + (b[3] - rh) / 2 : b[1] + ri * (rh + gap), w = b[2], pw = w / parts;
        for (var k = 0; k < parts; k++) {
          var x = b[0] + k * pw;
          if (k < fill) hatch([[x, y], [x + pw, y], [x + pw, y + rh], [x, y + rh]], -.8, 7, sd + ri * 7 + k).forEach(function (pts) { addStroke(el, pts, { color: fcol, w: 3, speed: 700 }); });
          strokesOf(el, KB.rectLines(x, y, pw, rh, sd + ri * 100 + k * 4), { color: col, w: 3.4, speed: 520 });
        }
      });
      el.bbox = [b[0], b[1], b[2], b[3]];
      return { el: el };
    }

    function buildNumberline(a) {
      var b = resolveBox(a, 'bottom'), el = mkEl(a, 'numberline', 'n'), sd = el.seed, col = KB.color(a.color);
      var mn = num(a.min, 0, -1e4, 1e4), mx = num(a.max, 10, mn + 1e-6, 1e4), step = num(a.step, 1, 1e-6, 1e4), y = b[1] + b[3] * .5;
      var xl = b[0] + 14, xr = b[0] + b[2] - 14;
      strokesOf(el, [KB.line(xl - 8, y, xr + 8, y, sd)], { color: col, w: 3.4 });
      strokesOf(el, KB.arrowHead(xl, y, xr + 8, y, sd + 1, 14), { color: col, w: 3.2 });
      var ticks = Math.min(60, Math.round((mx - mn) / step));
      var tick = Math.min(18, b[3] * .22), lab = Math.min(30, b[3] * .34) * P.ts;
      for (var i = 0; i <= ticks; i++) {
        var v = mn + i * step, x = xl + (xr - xl) * (v - mn) / (mx - mn);
        strokesOf(el, [KB.line(x, y - tick, x, y + tick, sd + 10 + i, .3)], { color: col, w: 3, speed: 500 });
      }
      var labels = [];
      var every = ticks > 14 ? Math.ceil(ticks / 12) : 1;
      for (var j = 0; j <= ticks; j += every) {
        var v2 = mn + j * step, x2 = xl + (xr - xl) * (v2 - mn) / (mx - mn);
        labels.push({ s: fa(String(Math.round(v2 * 1000) / 1000)), x: x2, y: y + tick + 6 });
      }
      el.nlLabels = labels; el.nlSize = lab;
      (Array.isArray(a.marks) ? a.marks : []).slice(0, 8).forEach(function (m, mi) {
        var v = num(m.at, mn, mn, mx), x = xl + (xr - xl) * (v - mn) / (mx - mn), mc = KB.color(m.color || 'yellow');
        strokesOf(el, [KB.ellipse(x, y, 11, 11, sd + 200 + mi, 0, 6.5)], { color: mc, w: 4 });
        hatch(KB.ellipse(x, y, 9, 9, 3, 0, 6.28).slice(0, -1), -.8, 5, sd + mi).forEach(function (pts) { addStroke(el, pts, { color: mc, w: 3, speed: 800 }); });
        if (m.label) labels.push({ s: str(m.label, 20), x: x, y: y - tick - 8 - lab, color: mc, hi: true });
      });
      // the tick labels are written after the line, as small text sprites
      labels.forEach(function (lb) {
        var w = measure(lb.s, lab, 'body', 500);
        el.items.push({ t: 'x', mode: 'text', lines: [{ s: lb.s, x: lb.x - w / 2, y: lb.y, w: w, rtl: RTL_RE.test(lb.s) }], size: lab, lh: lab * 1.6, fam: 'body', weight: 500, color: lb.color || col, t0: el.dur, d: .35, cat: lb.s.length });
        el.dur += .2;
      });
      el.bbox = [b[0], b[1], b[2], b[3]];
      return { el: el };
    }

    function buildGraph(a) {
      var b = resolveBox(a, 'center'), el = mkEl(a, 'graph', 'g'), sd = el.seed, col = KB.color(a.color || 'yellow'), ac = KB.color('white');
      var xmin = num(a.xmin, -5, -1e3, 1e3), xmax = num(a.xmax, 5, xmin + .01, 1e3), ymin = num(a.ymin, -5, -1e3, 1e3), ymax = num(a.ymax, 5, ymin + .01, 1e3);
      var pad = 26, gx = b[0] + pad, gy = b[1] + 8, gw = b[2] - pad - 8, gh = b[3] - pad - 8;
      function X(v) { return gx + gw * (v - xmin) / (xmax - xmin); }
      function Y(v) { return gy + gh * (1 - (v - ymin) / (ymax - ymin)); }
      var x0 = KB.clamp(0, xmin, xmax), y0 = KB.clamp(0, ymin, ymax);
      if (a.grid !== false) {
        var gxs = Math.max(1, Math.round((xmax - xmin) / 10)), gys = Math.max(1, Math.round((ymax - ymin) / 10)), k = 0;
        for (var v = Math.ceil(xmin / gxs) * gxs; v <= xmax; v += gxs) { strokesOf(el, [KB.line(X(v), gy, X(v), gy + gh, sd + (++k), .3)], { color: '#6f8f82', w: 1.6, speed: 900 }); }
        for (var u = Math.ceil(ymin / gys) * gys; u <= ymax; u += gys) { strokesOf(el, [KB.line(gx, Y(u), gx + gw, Y(u), sd + (++k), .3)], { color: '#6f8f82', w: 1.6, speed: 900 }); }
      }
      strokesOf(el, [KB.line(gx - 6, Y(y0), gx + gw + 6, Y(y0), sd + 1, .5)], { color: ac, w: 3.4 });
      strokesOf(el, KB.arrowHead(gx, Y(y0), gx + gw + 8, Y(y0), sd + 2, 14), { color: ac, w: 3.2 });
      strokesOf(el, [KB.line(X(x0), gy + gh + 6, X(x0), gy - 6, sd + 3, .5)], { color: ac, w: 3.4 });
      strokesOf(el, KB.arrowHead(X(x0), gy + gh, X(x0), gy - 8, sd + 4, 14), { color: ac, w: 3.2 });
      var fn = a.fn ? KB.compileExpr(a.fn) : null, series = [];
      if (fn) {
        var cur = [];
        for (var i = 0; i <= 160; i++) {
          var xv = xmin + (xmax - xmin) * i / 160, yv = fn(xv);
          if (isFinite(yv) && yv >= ymin - (ymax - ymin) * .1 && yv <= ymax + (ymax - ymin) * .1) cur.push([X(xv), Y(KB.clamp(yv, ymin - (ymax - ymin) * .1, ymax + (ymax - ymin) * .1))]);
          else { if (cur.length > 1) series.push(cur); cur = []; }
        }
        if (cur.length > 1) series.push(cur);
        series.forEach(function (pts, i) { addStroke(el, KB.wobble(pts, sd + 77 + i, .7, 6), { color: col, w: 4, speed: 300 }); });
      }
      (Array.isArray(a.points) ? a.points : []).slice(0, 12).forEach(function (p, i) {
        var px = X(num(p[0], 0, xmin, xmax)), py = Y(num(p[1], 0, ymin, ymax));
        strokesOf(el, [KB.ellipse(px, py, 8, 8, sd + 300 + i, 0, 6.5)], { color: KB.color('pink'), w: 4 });
      });
      var ls = 24 * P.ts;
      [[xmax, X(xmax) - 4, Y(y0) + 6], [ymax, X(x0) + 8, gy - 4]].forEach(function (lb) {
        var s = fa(String(Math.round(lb[0] * 100) / 100)), w = measure(s, ls, 'body', 500);
        el.items.push({ t: 'x', mode: 'text', lines: [{ s: s, x: lb[1] - w / 2, y: lb[2], w: w, rtl: false }], size: ls, lh: ls * 1.5, fam: 'body', weight: 500, color: ac, t0: el.dur, d: .3, cat: 3 });
        el.dur += .15;
      });
      el.bbox = [b[0], b[1], b[2], b[3]];
      return { el: el };
    }

    function buildTable(a) {
      var b = resolveBox(a, 'center'), el = mkEl(a, 'table', 'tb'), sd = el.seed, col = KB.color(a.color);
      var rows = (Array.isArray(a.rows) ? a.rows : []).slice(0, 8).map(function (r) { return (Array.isArray(r) ? r : [r]).slice(0, 6).map(function (c) { return str(c, 40); }); });
      if (!rows.length) return null;
      var nr = rows.length, nc = Math.max.apply(null, rows.map(function (r) { return r.length; })), ch = Math.min(b[3] / nr, 78 * P.ts), cw = b[2] / nc;
      var tw = cw * nc, th = ch * nr, tx = b[0], ty = b[1];
      var segs = [KB.line(tx, ty, tx + tw, ty, sd), KB.line(tx, ty + th, tx + tw, ty + th, sd + 1), KB.line(tx, ty, tx, ty + th, sd + 2), KB.line(tx + tw, ty, tx + tw, ty + th, sd + 3)];
      for (var r = 1; r < nr; r++) segs.push(KB.line(tx, ty + r * ch, tx + tw, ty + r * ch, sd + 10 + r, .5));
      for (var c = 1; c < nc; c++) segs.push(KB.line(tx + c * cw, ty, tx + c * cw, ty + th, sd + 30 + c, .5));
      strokesOf(el, segs, { color: col, w: 3, speed: 700 });
      var fs = Math.min(SIZES.s * P.ts, ch * .52);
      rows.forEach(function (row, ri) {
        row.forEach(function (cell, ci) {
          if (!cell) return;
          var txt = fa(cell), w = measure(txt, fs, 'body', ri === 0 && a.header !== false ? 700 : 500);
          var cx = tx + ci * cw + (cw - w) / 2, rt = RTL_RE.test(txt);
          el.items.push({ t: 'x', mode: 'text', lines: [{ s: txt, x: cx, y: ty + ri * ch + (ch - fs * 1.6) / 2, w: w, rtl: rt }], size: fs, lh: fs * 1.6, fam: 'body', weight: ri === 0 && a.header !== false ? 700 : 500, color: ri === 0 && a.header !== false ? KB.color('yellow') : col, t0: el.dur, d: Math.max(.3, cell.length / 12), cat: cell.length });
          el.dur += Math.max(.3, cell.length / 12) * .9;
        });
      });
      el.bbox = [tx, ty, tw, th];
      return { el: el };
    }

    function buildDots(a) {
      var b = resolveBox(a, 'center'), el = mkEl(a, 'dots', 'd'), sd = el.seed, col = KB.color(a.color || 'white');
      var n = Math.round(num(a.n, 6, 1, 40)), cols = Math.round(num(a.cols, Math.min(n, 10), 1, 12)), rows = Math.ceil(n / cols);
      var s = Math.min(b[2] / cols, b[3] / rows, 74), r = s * .3;
      var ox = b[0] + (b[2] - s * cols) / 2, oy = b[1] + (b[3] - s * rows) / 2;
      for (var i = 0; i < n; i++) {
        var cx = ox + (i % cols + .5) * s, cy = oy + (Math.floor(i / cols) + .5) * s;
        strokesOf(el, [KB.ellipse(cx, cy, r, r, sd + i, 0, 6.6)], { color: col, w: 3.6, speed: 500 });
        hatch(KB.ellipse(cx, cy, r * .8, r * .8, 3, 0, 6.28).slice(0, -1), -.8, 5, sd + i).forEach(function (pts) { addStroke(el, pts, { color: col, w: 3, speed: 900 }); });
      }
      el.bbox = [ox, oy, s * cols, s * rows];
      return { el: el };
    }

    /* ----- marks on existing things ----- */
    function targetBox(a) {
      if (typeof a.target === 'string' && ids[a.target] && ids[a.target].bbox) return ids[a.target].bbox;
      if (Array.isArray(a.at)) return [a.at[0] * W / 100 - 30, a.at[1] * P.H / 100 - 30, 60, 60];
      return null;
    }
    function buildMark(a) {
      var tb = targetBox(a); if (!tb) { warnings.push('mark without target'); return null; }
      var el = mkEl(a, 'mark', 'k'), sd = el.seed, style = a.style || 'underline', col = KB.color(a.color || 'yellow'), o = { color: col, w: 3.8 };
      var x = tb[0], y = tb[1], w = tb[2], h = tb[3], p = 8;
      if (style === 'box') strokesOf(el, KB.rectLines(x - p, y - p, w + 2 * p, h + 2 * p, sd), o);
      else if (style === 'circle') strokesOf(el, [KB.ellipse(x + w / 2, y + h / 2, w / 2 + 18, h / 2 + 12, sd, 0, 6.5)], o);
      else if (style === 'tick') strokesOf(el, [KB.wobble([[x + w + 14, y + h * .5], [x + w + 28, y + h * .5 + 16], [x + w + 54, y + h * .5 - 18]], sd, .5, 4)], { color: KB.color('green'), w: 5 });
      else if (style === 'cross') strokesOf(el, [KB.line(x + w + 14, y + h * .5 - 14, x + w + 42, y + h * .5 + 14, sd), KB.line(x + w + 42, y + h * .5 - 14, x + w + 14, y + h * .5 + 14, sd + 1)], { color: KB.color('pink'), w: 5 });
      else if (style === 'arrow') {
        var ax = x + w + 70, ay = y + h / 2;
        strokesOf(el, [KB.line(ax, ay, x + w + 10, y + h / 2, sd)], o); strokesOf(el, KB.arrowHead(ax, ay, x + w + 10, y + h / 2, sd + 1, 16), o);
      } else { var uy = y + h - 2; strokesOf(el, [KB.line(x - 4, uy, x + w + 4, uy, sd, 1.2), KB.line(x, uy + 7, x + w * .8, uy + 6, sd + 5, 1)], o); }
      return { el: el, noBox: true };
    }

    /* ----- animations (drawn live on their own layer) ----- */
    function buildAnim(a) {
      var b = resolveBox(a, 'center'), f = fitSquare(b), kind = str(a.kind, 12);
      var an = { id: newId(a, 'a'), kind: 'anim', anim: kind, f: f, box: b, a: a, dur: num(a.dur, 3, .5, 20), loop: false, color: KB.color(a.color), seed: KB.hash(kind + (auto + 1)) };
      var ic = KB.own(KB.ICONS, a.icon) ? a.icon : 'ball';
      an.icon = ic; an.iconStrokes = KB.iconStrokes(ic, an.seed);
      an.size = num(a.size, 22, 6, 90);
      an.bbox = b.slice();
      if (kind === 'swing' || kind === 'orbit' || kind === 'wave' || kind === 'spin') an.loop = a.loop !== false;
      if (['move', 'fall', 'swing', 'orbit', 'wave', 'grow', 'fill', 'count', 'spin'].indexOf(kind) < 0) { warnings.push('unknown anim ' + kind); return null; }
      if (kind === 'fill') an.shape = a.shape === 'bar' ? 'bar' : (a.shape === 'pie' ? 'pie' : 'cup');
      if (kind === 'count') { an.from = num(a.from, 0, -1e6, 1e6); an.to = num(a.to, 10, -1e6, 1e6); }
      if (kind === 'orbit') an.bodies = (Array.isArray(a.bodies) ? a.bodies : [{ r: 30, period: 4, icon: 'ball' }]).slice(0, 4).map(function (bd, i) { return { r: num(bd.r, 30, 8, 48), period: num(bd.period, 4, 1, 30), icon: KB.own(KB.ICONS, bd.icon) ? bd.icon : 'ball', size: num(bd.size, 12, 4, 30), strokes: KB.iconStrokes(KB.own(KB.ICONS, bd.icon) ? bd.icon : 'ball', an.seed + i) }; });
      ids[an.id] = an;
      return { anim: an };
    }

    /* ───────── scenes ───────── */
    function speechSecs(s) { return Math.max(1.8, str(s, 1200).length / SPEECH_CPS); }
    var BUILD = { text: buildText, math: buildMath, line: buildShape, arrow: buildShape, rect: buildShape, circle: buildShape, ellipse: buildShape, poly: buildShape, path: buildShape, icon: buildShape,
                  pie: buildPie, bars: buildBars, numberline: buildNumberline, graph: buildGraph, table: buildTable, dots: buildDots, mark: buildMark, anim: buildAnim };
    var scenes = Array.isArray(lesson.scenes) ? lesson.scenes.slice(0, 800) : [];
    scenes.forEach(function (sc, si) {
      var S = { say: str(sc.say, 1200), acts: [], dur: 0, clear: !!sc.clear || si === 0 && false, index: si, aliveAfter: null };
      if (S.clear) {
        Object.keys(cursors).forEach(function (k) { delete cursors[k]; });
        S.acts.push({ type: 'erase', targets: 'all', natural: 1.1 });
      }
      (Array.isArray(sc.do) ? sc.do : []).slice(0, 16).forEach(function (a) {
        if (!a || typeof a !== 'object') return;
        var op = str(a.op, 14);
        if (op === 'erase') {
          var ts = a.target === 'all' || !a.target ? 'all' : [].concat(a.target).filter(function (t) { return typeof t === 'string' && ids[t]; });
          if (ts === 'all') Object.keys(cursors).forEach(function (k) { delete cursors[k]; });
          if (ts === 'all' || ts.length) S.acts.push({ type: 'erase', targets: ts, natural: ts === 'all' ? 1.1 : .8 });
          return;
        }
        if (op === 'wait' || op === 'pause') { S.acts.push({ type: 'wait', natural: num(a.s, 1, .2, 6) }); return; }
        if (op === 'point') {
          var tb = targetBox(a); if (tb) S.acts.push({ type: 'point', box: tb, natural: 1.6 });
          return;
        }
        var fn = KB.own(BUILD, op) ? BUILD[op] : null; if (!fn) { warnings.push('unknown op ' + op); return; }
        var res; try { res = fn(a, 't'); } catch (err) { warnings.push('bad ' + op + ': ' + err.message); return; }
        if (!res) return;
        if (res.overflow) {                     // zone full: start a fresh page, then place it
          Object.keys(cursors).forEach(function (k) { delete cursors[k]; });
          S.acts.push({ type: 'erase', targets: 'all', natural: 1.1 });
          try { res = fn(a, 't'); } catch (err2) { return; } if (!res || !res.el) return;
        }
        if (res.anim) S.acts.push({ type: 'anim', anim: res.anim, natural: Math.min(res.anim.dur, 6) });
        else S.acts.push({ type: 'draw', el: res.el, natural: Math.max(.3, res.el.dur) });
      });
      // pacing: spread the actions over the time the line takes to say
      var say = speechSecs(S.say), lead = .3, D = 0;
      S.acts.forEach(function (a) { D += a.natural; });
      var n = Math.max(1, S.acts.length), slack = say * .9 - D, gap = KB.clamp(slack > 0 ? slack / n : 0, .12, 1.8);
      var t = lead;
      var squeeze = KB.clamp(say * .95 / Math.max(.1, D + n * .12), .5, 1);   // long drawings are drawn a little faster, never slower than the speech allows
      S.acts.forEach(function (a) { a.dur = a.type === 'anim' ? a.natural : a.natural * squeeze; a.start = t; t += a.dur + gap * Math.max(.4, squeeze); });
      S.speech = say;
      S.dur = Math.max(say + .5, t + .5);
      model.scenes.push(S);
    });
    return model;
  };

  KB.speechSecs = function (s) { return Math.max(1.8, String(s || '').length / SPEECH_CPS); };
  KB.RTL_RE = RTL_RE; KB.FAM = FAM;
})(window);
