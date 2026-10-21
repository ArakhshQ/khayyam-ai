/* Khayyam chalkboard — formulas and function plotting (no libraries).
   Formula syntax is a small LaTeX subset:  \frac{a}{b}  x^2  x_1  \sqrt{x}  \times \div \cdot \pm \le \ge \ne \approx \pi \to  */
(function (global) {
  'use strict';
  var KB = global.KB = global.KB || {};

  var FA = '۰۱۲۳۴۵۶۷۸۹';
  KB.faDigits = function (s) { return String(s).replace(/[0-9]/g, function (d) { return FA[d]; }).replace(/\./g, '٫'); };

  var SYM = {
    times: '×', div: '÷', cdot: '·', pm: '±', le: '≤', ge: '≥', leq: '≤', geq: '≥', ne: '≠', neq: '≠', approx: '≈',
    pi: 'π', theta: 'θ', alpha: 'α', beta: 'β', gamma: 'γ', delta: 'δ', Delta: 'Δ', lambda: 'λ', mu: 'μ', sigma: 'σ', omega: 'ω', phi: 'φ',
    to: '→', rightarrow: '→', leftarrow: '←', infty: '∞', degree: '°', circ: '°', sum: 'Σ', percent: '٪', '%': '٪', neg: '¬', in: '∈', cup: '∪', cap: '∩',
    ',': ' ', ' ': ' ', ';': ' ', quad: ' ', ldots: '…', dots: '…'
  };
  var BIN = { '=': 1, '+': 1, '−': 1, '-': 1, '×': 1, '÷': 1, '<': 1, '>': 1, '≤': 1, '≥': 1, '≈': 1, '≠': 1, '→': 1, '±': 1, '·': 0 };
  var FUNCS = { sin: 1, cos: 1, tan: 1, log: 1, ln: 1, cot: 1, sec: 1, lim: 1, min: 1, max: 1, sqrt: 0 };

  /* ───────── parse ───────── */
  function tokenize(src) {
    var re = /\\([A-Za-z]+|[,;% ])|([0-9]+(?:[.,][0-9]+)?)|([A-Za-z]+)|(\S)/g, m, out = [];
    while ((m = re.exec(src))) {
      if (m[1] != null) out.push({ t: 'cmd', v: m[1] });
      else if (m[2] != null) out.push({ t: 'num', v: m[2] });
      else if (m[3] != null) out.push({ t: 'id', v: m[3] });
      else out.push({ t: 'ch', v: m[4] });
    }
    return out;
  }
  function parse(src) {
    var toks = tokenize(src), i = 0;
    function atom() {
      var t = toks[i++]; if (!t) return null;
      if (t.t === 'num') return { k: 'n', s: t.v };
      if (t.t === 'id') return FUNCS[t.v] === 1 || t.v.length > 1 ? { k: 'w', s: t.v } : { k: 'v', s: t.v };
      if (t.t === 'cmd') {
        if (t.v === 'frac' || t.v === 'dfrac') { var a = group(), b = group(); return { k: 'frac', n: a, d: b }; }
        if (t.v === 'sqrt') { return { k: 'sqrt', x: group() }; }
        if (t.v === 'left' || t.v === 'right') { return atom(); }
        if (t.v === 'text' || t.v === 'mathrm') { var g = group(); return { k: 'w', s: g.map(function (n) { return n.s || ''; }).join('') }; }
        if (SYM[t.v] != null) return { k: 'o', s: SYM[t.v] };
        return { k: 'w', s: t.v };
      }
      if (t.v === '{') { i--; return { k: 'g', c: group() }; }
      if (t.v === '/') return { k: 'o', s: '/' };
      return { k: 'o', s: t.v === '*' ? '×' : t.v === '-' ? '−' : t.v };
    }
    function group() {
      if (toks[i] && toks[i].t === 'ch' && toks[i].v === '{') {
        i++; var out = [];
        while (i < toks.length && !(toks[i].t === 'ch' && toks[i].v === '}')) out = out.concat(seqOne());
        i++; return out;
      }
      var a = atom(); return a ? [a] : [];
    }
    function seqOne() {
      var a = atom(); if (!a) return [];
      while (toks[i] && toks[i].t === 'ch' && (toks[i].v === '^' || toks[i].v === '_')) {
        var up = toks[i].v === '^'; i++;
        var e = group();
        a = up ? { k: 'sup', b: a, e: e } : { k: 'sub', b: a, e: e };
      }
      return [a];
    }
    var all = [];
    while (i < toks.length) all = all.concat(seqOne());
    return all;
  }

  /* ───────── layout: boxes {w, asc, desc, ops:[{k:'t'|'l', ...}]} relative to (0, baseline) ───────── */
  KB.mathLayout = function (src, size, digits, measure) {
    var tree = parse(src);
    function txt(s, sz, extra) {
      var w = measure(s, sz);
      return { w: w, asc: sz * .8, desc: sz * .26, ops: [{ k: 't', s: s, x: 0, y: 0, size: sz }] };
    }
    function hbox(list, sz) {
      var x = 0, asc = sz * .7, desc = sz * .22, ops = [], prevBin = false;
      list.forEach(function (n, idx) {
        var b = box(n, sz), pad = 0;
        if (n.k === 'o' && BIN[n.s]) pad = sz * .24;
        else if (n.k === 'w' && FUNCS[n.s] === 1) pad = sz * .06;
        x += pad;
        b.ops.forEach(function (o) { var c = Object.assign({}, o); if (o.k === 't') { c.x += x; } else { c.pts = o.pts.map(function (p) { return [p[0] + x, p[1]]; }); } ops.push(c); });
        x += b.w + pad + (n.k === 'w' ? sz * .08 : 0);
        asc = Math.max(asc, b.asc); desc = Math.max(desc, b.desc);
      });
      return { w: x, asc: asc, desc: desc, ops: ops };
    }
    function shift(b, dx, dy) {
      return b.ops.map(function (o) {
        var c = Object.assign({}, o);
        if (o.k === 't') { c.x += dx; c.y += dy; } else c.pts = o.pts.map(function (p) { return [p[0] + dx, p[1] + dy]; });
        return c;
      });
    }
    function box(n, sz) {
      if (n.k === 'n') return txt(digits === 'fa' ? KB.faDigits(n.s.replace(',', '.')) : n.s, sz);
      if (n.k === 'v' || n.k === 'w' || n.k === 'o') return txt(n.s, sz);
      if (n.k === 'g') return hbox(n.c, sz);
      if (n.k === 'frac') {
        var s2 = sz * .84, nb = hbox(n.n, s2), db = hbox(n.d, s2), w = Math.max(nb.w, db.w) + sz * .3;
        var axis = sz * .3, gap = sz * .16, lw = sz * .07;
        var nOps = shift(nb, (w - nb.w) / 2, -(axis + gap + nb.desc));
        var dOps = shift(db, (w - db.w) / 2, -axis + gap + db.asc);
        var bar = { k: 'l', w: lw, pts: [[sz * .04, -axis], [w - sz * .04, -axis]] };
        return { w: w, asc: axis + gap + nb.desc + nb.asc, desc: db.asc + db.desc + gap - axis, ops: nOps.concat(dOps, [bar]) };
      }
      if (n.k === 'sqrt') {
        var ib = hbox(n.x, sz), lead = sz * .8, top = ib.asc + sz * .16;
        var rad = { k: 'l', w: sz * .07, pts: [[sz * .04, -sz * .28], [sz * .2, -sz * .36], [sz * .42, sz * .14], [sz * .74, -top], [lead + ib.w + sz * .08, -top]] };
        return { w: lead + ib.w + sz * .12, asc: top + sz * .08, desc: Math.max(ib.desc, sz * .18), ops: shift(ib, lead, 0).concat([rad]) };
      }
      if (n.k === 'sup' || n.k === 'sub') {
        var bb = box(n.b, sz), eb = hbox(n.e, sz * .62), up = n.k === 'sup';
        var dy = up ? -(bb.asc * .62) : (sz * .22);
        var ops = bb.ops.concat(shift(eb, bb.w + sz * .03, dy));
        return { w: bb.w + eb.w + sz * .05, asc: up ? Math.max(bb.asc, -dy + eb.asc) : bb.asc, desc: up ? bb.desc : Math.max(bb.desc, dy + eb.desc), ops: ops };
      }
      return txt('', sz);
    }
    return hbox(tree, size);
  };

  /* ───────── safe expression evaluator for y = f(x) ───────── */
  KB.compileExpr = function (src) {
    var s = String(src || '').replace(/\s+/g, '').replace(/²/g, '^2').replace(/³/g, '^3').replace(/π/g, 'pi').replace(/\*\*/g, '^');
    if (s.length > 80 || /[^0-9a-z+\-*/^().,]/.test(s)) return null;
    var i = 0;
    function peek() { return s[i]; }
    function expr() { var v = term(); while (peek() === '+' || peek() === '-') { var op = s[i++], r = term(); v = (function (a, b, o) { return function (x) { return o === '+' ? a(x) + b(x) : a(x) - b(x); }; })(v, r, op); } return v; }
    function term() {
      var v = power();
      while (true) {
        var c = peek();
        if (c === '*' || c === '/') { i++; var r = power(); v = (function (a, b, o) { return function (x) { return o === '*' ? a(x) * b(x) : a(x) / b(x); }; })(v, r, c); }
        else if (c === 'x' || c === '(' || (c && /[a-z]/.test(c) && s.substr(i, 2) !== 'pi' && true && /^(sin|cos|tan|sqrt|abs|ln|exp|log)/.test(s.substr(i)))) { var r2 = power(); v = (function (a, b) { return function (x) { return a(x) * b(x); }; })(v, r2); }
        else return v;
      }
    }
    function power() { var b = unary(); if (peek() === '^') { i++; var e = power(); return function (x) { return Math.pow(b(x), e(x)); }; } return b; }
    function unary() { if (peek() === '-') { i++; var u = unary(); return function (x) { return -u(x); }; } if (peek() === '+') { i++; return unary(); } return prim(); }
    function prim() {
      var c = peek();
      if (c === '(') { i++; var v = expr(); if (peek() === ')') i++; return v; }
      var m = /^[0-9]*\.?[0-9]+/.exec(s.substr(i));
      if (m) { i += m[0].length; var num = parseFloat(m[0]); return function () { return num; }; }
      var f = /^(sin|cos|tan|sqrt|abs|ln|exp|log|pi|x|e)/.exec(s.substr(i));
      if (!f) throw new Error('bad');
      i += f[0].length;
      if (f[0] === 'x') return function (x) { return x; };
      if (f[0] === 'pi') return function () { return Math.PI; };
      if (f[0] === 'e') return function () { return Math.E; };
      var arg; if (peek() === '(') { i++; arg = expr(); if (peek() === ')') i++; } else arg = power();
      var fn = { sin: Math.sin, cos: Math.cos, tan: Math.tan, sqrt: Math.sqrt, abs: Math.abs, ln: Math.log, log: Math.log10, exp: Math.exp }[f[0]];
      return function (x) { return fn(arg(x)); };
    }
    try { var fnc = expr(); if (i < s.length) return null; return fnc; } catch (e) { return null; }
  };
})(window);