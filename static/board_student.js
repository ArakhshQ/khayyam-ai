/* Khayyam chalkboard: the student's side.
   The student can (1) POINT: a gold marker follows the mouse or finger, and a stroke around or under something on the board selects it;
   (2) WRITE with a pen (finger, stylus, mouse) in light-blue chalk; (3) TYPE text onto the board.
   Everything lives on its own canvas above the teacher's, so the lesson itself is never changed.
   The board can describe what a selection covers in exact terms (text lines, formulas, parts of a picture), and can take a snapshot,
   so the teacher is told precisely what the student means: no guessing from pixels alone.
   Events (board.on): 'select' {rect, desc, thumb}, 'ink' (writing paused: {count}), 'typed' {text, rect, desc}, 'tool' (name or null). */
(function (global) {
  'use strict';
  var KB = global.KB, P = KB.Board.prototype;
  var INK = '#9fd8ff', INK_W = 3.6, SEL = '#ffd84d', TXT_SIZE = 34;

  function inter(a, b) {
    var x0 = Math.max(a[0], b[0]), y0 = Math.max(a[1], b[1]), x1 = Math.min(a[0] + a[2], b[0] + b[2]), y1 = Math.min(a[1] + a[3], b[1] + b[3]);
    return (x1 > x0 && y1 > y0) ? [x0, y0, x1 - x0, y1 - y0] : null;
  }
  function area(b) { return Math.max(1, b[2] * b[3]); }
  function rtl(s) { return KB.RTL_RE && KB.RTL_RE.test(s); }

  function st(board) {
    if (!board.stu) board.stu = { tool: null, items: [], cur: null, hover: null, typing: null, idle: 0, fade: 0, undo: [], regions: {} };
    return board.stu;
  }

  /* ───────── coordinates ───────── */
  P.toLogical = function (e) {
    var r = this.wrap.getBoundingClientRect(), H = this.profile ? this.profile.H : 600;
    return [(e.clientX - r.left) / Math.max(1, r.width) * 1000, (e.clientY - r.top) / Math.max(1, r.height) * H];
  };

  /* ───────── tools ───────── */
  P.setTool = function (name) {
    var s = st(this), cv = this.cv.stu; if (!cv) return;
    if (name === s.tool) name = null;                                  // pressing the active tool again puts it down
    this.commitTyping();
    s.tool = name || null; s.hover = null;
    cv.style.pointerEvents = s.tool ? 'auto' : 'none';
    cv.style.touchAction = s.tool ? 'none' : 'auto';
    cv.style.cursor = s.tool === 'point' ? 'none' : (s.tool === 'pen' ? 'crosshair' : (s.tool === 'type' ? 'text' : 'default'));
    this.studentDraw();
    this.emit('tool', s.tool);
    return s.tool;
  };
  P.studentWire = function () {
    var self = this, cv = this.cv.stu, s = st(this); if (!cv || s.wired) return; s.wired = true;
    cv.addEventListener('pointerdown', function (e) { self.stuDown(e); });
    cv.addEventListener('pointermove', function (e) { self.stuMove(e); });
    cv.addEventListener('pointerup', function (e) { self.stuUp(e); });
    cv.addEventListener('pointercancel', function (e) { self.stuUp(e, true); });
    cv.addEventListener('pointerleave', function () { s.hover = null; self.studentDraw(); });
  };
  P.stuDown = function (e) {
    var s = st(this); if (!s.tool || (e.pointerType === 'mouse' && e.button !== 0)) return;
    e.preventDefault();
    var p = this.toLogical(e);
    if (s.tool === 'type') { this.openTyping(p); return; }
    this.commitTyping();
    try { this.cv.stu.setPointerCapture(e.pointerId); } catch (err) { }
    clearTimeout(s.idle);
    s.cur = { k: s.tool === 'pen' ? 'p' : 'm', pts: [p], t: performance.now() };
    s.hover = p; this.studentDraw();
  };
  P.stuMove = function (e) {
    var s = st(this); if (!s.tool) return;
    var p = this.toLogical(e); s.hover = p;
    if (s.cur) {
      var l = s.cur.pts[s.cur.pts.length - 1];
      if (Math.hypot(p[0] - l[0], p[1] - l[1]) > 1.6) s.cur.pts.push(p);
    }
    this.studentDraw(true);
  };
  P.stuUp = function (e, cancel) {
    var s = st(this), c = s.cur; if (!c) return;
    s.cur = null;
    try { this.cv.stu.releasePointerCapture(e.pointerId); } catch (err) { }
    if (cancel || !c.pts.length) { this.studentDraw(); return; }
    var self = this, b = this.ptsBox(c.pts);
    if (c.k === 'm') {                                                      // pointing: remember the marker for a while, tell the page what it covers
      c.t = performance.now(); c.sel = true; s.items = s.items.filter(function (it) { return !it.sel; }); s.items.push(c);
      clearTimeout(s.fade); s.fade = setTimeout(function () { self.studentFadeSel(); }, 14000);
      var rect = this.selectionRect(c.pts, b);
      this.studentDraw();
      this.emit('select', { rect: rect, desc: this.describeRect(rect), item: c });
    } else {                                                                  // writing: it stays; when the hand rests the page can offer to send it
      c.sent = false; s.items.push(c); s.undo.push(c);
      this.studentDraw();
      clearTimeout(s.idle); s.idle = setTimeout(function () { self.emit('ink', { count: self.unsent().length }); }, 1400);
    }
  };
  P.ptsBox = function (pts) {
    var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    pts.forEach(function (p) { x0 = Math.min(x0, p[0]); y0 = Math.min(y0, p[1]); x1 = Math.max(x1, p[0]); y1 = Math.max(y1, p[1]); });
    return [x0, y0, x1 - x0, y1 - y0];
  };
  // what rectangle does a marker stroke mean? a click means "this spot"; a flat stroke is an underline (the words are above it)
  P.selectionRect = function (pts, b) {
    var H = this.profile ? this.profile.H : 600, len = 0, i;
    for (i = 1; i < pts.length; i++) len += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    var r;
    if (len < 14) r = [b[0] - 50, b[1] - 34, 100, 68];
    else if (b[3] < 30 && b[2] > 40) r = [b[0] - 6, b[1] - 40, b[2] + 12, 52];
    else r = [b[0] - 8, b[1] - 8, Math.max(30, b[2] + 16), Math.max(30, b[3] + 16)];
    var x0 = KB.clamp(r[0], 0, 1000), y0 = KB.clamp(r[1], 0, H), x1 = KB.clamp(r[0] + r[2], 0, 1000), y1 = KB.clamp(r[1] + r[3], 0, H);
    return [x0, y0, Math.max(20, x1 - x0), Math.max(20, y1 - y0)];
  };
  P.studentFadeSel = function () {
    var s = st(this), self = this; s.items = s.items.filter(function (it) { return !it.sel; }); self.studentDraw();
  };

  /* ───────── typing onto the board ───────── */
  P.openTyping = function (p) {
    var s = st(this), self = this; this.commitTyping();
    var r = this.wrap.getBoundingClientRect(), H = this.profile.H, sc = r.width / 1000;
    var inp = document.createElement('textarea');
    inp.className = 'kb-typing'; inp.rows = 1; inp.setAttribute('dir', 'auto'); inp.setAttribute('aria-label', 'بنویسید و Enter بزنید');
    inp.style.cssText = 'position:absolute;left:' + Math.round(Math.min(p[0], 1000 - 160) * sc) + 'px;top:' + Math.round(Math.max(0, p[1] - 18) * sc) + 'px;min-width:' + Math.round(150 * sc) + 'px;max-width:' + Math.round((1000 - Math.min(p[0], 840)) * sc - 6) + 'px;font-size:' + Math.round(TXT_SIZE * sc * 1.05) + 'px;';
    this.wrap.appendChild(inp);
    s.typing = { el: inp, p: p };
    inp.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); self.commitTyping(); }
      else if (e.key === 'Escape') { e.preventDefault(); self.cancelTyping(); }
      e.stopPropagation();
    });
    inp.addEventListener('input', function () { inp.style.height = 'auto'; inp.style.height = inp.scrollHeight + 'px'; });
    setTimeout(function () { inp.focus(); }, 0);
  };
  P.cancelTyping = function () {
    var s = st(this); if (s.typing && s.typing.el.parentNode) s.typing.el.parentNode.removeChild(s.typing.el); s.typing = null;
  };
  P.commitTyping = function () {
    var s = st(this), t = s.typing; if (!t) return;
    var text = (t.el.value || '').trim(); this.cancelTyping();
    if (!text) return;
    text = text.slice(0, 300);
    var item = { k: 't', text: text, x: t.p[0], y: t.p[1], size: TXT_SIZE, sent: true };
    s.items.push(item); s.undo.push(item); this.studentDraw();
    var b = this.itemBox(item);
    var rect = [Math.max(0, b[0] - 12), Math.max(0, b[1] - 12), b[2] + 24, b[3] + 24];
    this.emit('typed', { text: text, rect: rect, desc: this.describeRect(rect) });
  };
  P.textLines = function (item) {
    var out = [], parts = String(item.text).split('\n'), maxW = 1000 - item.x - 12, lh = item.size * 1.5, self = this;
    parts.forEach(function (ln) {
      var words = ln.split(/\s+/).filter(Boolean), cur = '';
      if (!words.length) { out.push(''); return; }
      words.forEach(function (w) {
        var t = cur ? cur + ' ' + w : w;
        if (cur && KB.measure(t, item.size, 'body', 600) > maxW) { out.push(cur); cur = w; } else cur = t;
      });
      out.push(cur);
    });
    return { lines: out, lh: lh };
  };
  P.itemBox = function (it) {
    if (it.k === 't') {
      var tl = this.textLines(it), w = 0; tl.lines.forEach(function (l) { w = Math.max(w, KB.measure(l, it.size, 'body', 600)); });
      return [it.x, it.y - it.size * .2, Math.min(w, 1000 - it.x), tl.lines.length * tl.lh];
    }
    return this.ptsBox(it.pts);
  };

  /* ───────── drawing the student's layer ───────── */
  function strokePath(ctx, pts) {
    ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
    if (pts.length < 3) { ctx.lineTo(pts[pts.length - 1][0] + .01, pts[pts.length - 1][1]); return; }
    for (var i = 1; i < pts.length - 1; i++) { var mx = (pts[i][0] + pts[i + 1][0]) / 2, my = (pts[i][1] + pts[i + 1][1]) / 2; ctx.quadraticCurveTo(pts[i][0], pts[i][1], mx, my); }
    ctx.lineTo(pts[pts.length - 1][0], pts[pts.length - 1][1]);
  }
  P.drawStudentItems = function (ctx, items, o) {
    var self = this; o = o || {};
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    items.forEach(function (it) {
      if (o.onlyUnsent && it.sent) return;
      if (it.k === 'p') {
        ctx.strokeStyle = o.color || INK; ctx.lineWidth = INK_W * (o.wScale || 1); ctx.globalAlpha = o.color ? 1 : .96;
        strokePath(ctx, it.pts); ctx.stroke();
      } else if (it.k === 'm') {
        var age = (performance.now() - it.t) / 1000, a = age > 11 ? Math.max(0, 1 - (age - 11) / 3) : 1;
        ctx.globalAlpha = .28 * a; ctx.strokeStyle = SEL; ctx.lineWidth = 15; ctx.shadowColor = SEL; ctx.shadowBlur = 14; strokePath(ctx, it.pts); ctx.stroke();
        ctx.globalAlpha = .9 * a; ctx.lineWidth = 3.4; ctx.shadowBlur = 0; strokePath(ctx, it.pts); ctx.stroke();
      } else if (it.k === 't') {
        var tl = self.textLines(it); ctx.fillStyle = o.color || INK; ctx.globalAlpha = 1; ctx.textBaseline = 'top';
        ctx.font = '600 ' + it.size + 'px Vazirmatn, system-ui, sans-serif';
        tl.lines.forEach(function (l, i) {
          var r = rtl(l), w = ctx.measureText(l).width;
          ctx.direction = r ? 'rtl' : 'ltr'; ctx.textAlign = 'left';
          ctx.fillText(l, r ? it.x + Math.min(w, 1000 - it.x) - w : it.x, it.y + i * tl.lh - it.size * .2);
        });
      }
      ctx.globalAlpha = 1; ctx.shadowBlur = 0;
    });
  };
  P.studentDraw = function (hoverOnly) {
    var cx = this.cx && this.cx.stu, s = this.stu; if (!cx) return;
    cx.setTransform(1, 0, 0, 1, 0, 0); cx.clearRect(0, 0, cx.canvas.width, cx.canvas.height);
    if (!s) return;
    cx.setTransform(this.S, 0, 0, this.S, 0, 0);
    this.drawStudentItems(cx, s.items);
    if (s.cur) this.drawStudentItems(cx, [s.cur]);
    if (s.tool === 'point' && s.hover) this.drawTeacherCursor(cx, s.hover, s.cur ? s.cur.pts : null);
    var self = this;
    if (s.items.some(function (it) { return it.sel && performance.now() - it.t > 11000; }) && !s.raf) {            // let a fading marker animate out
      s.raf = requestAnimationFrame(function () { s.raf = 0; self.studentDraw(); });
    }
  };
  // "the teacher's pointer goes wherever my pointer goes": the gold chalk arrow sits on the student's cursor
  P.drawTeacherCursor = function (c, pos, trail) {
    c.save();
    c.translate(pos[0], pos[1]); c.rotate(-2.36);
    c.shadowColor = 'rgba(244,216,110,.8)'; c.shadowBlur = 14;
    c.fillStyle = '#f4d86e'; c.strokeStyle = 'rgba(8,28,22,.9)'; c.lineWidth = 2.2; c.lineJoin = 'round';
    var k = Math.max(1, (this.dpr / this.S) * .62);
    c.scale(k, k);
    c.beginPath(); c.moveTo(0, 0); c.lineTo(-30, -13); c.lineTo(-22, 0); c.lineTo(-30, 13); c.closePath(); c.fill(); c.shadowBlur = 0; c.stroke();
    c.restore();
  };

  /* ───────── controls ───────── */
  P.unsent = function () { return (this.stu ? this.stu.items : []).filter(function (it) { return (it.k === 'p') && !it.sent; }); };
  P.studentUndo = function () {
    var s = st(this), it = s.undo.pop(); if (!it) return false;
    s.items = s.items.filter(function (x) { return x !== it; }); this.studentDraw(); return true;
  };
  P.studentClear = function (all) {
    var s = this.stu; if (!s) return;
    this.cancelTyping(); clearTimeout(s.idle); clearTimeout(s.fade);
    s.items = []; s.undo = []; s.cur = null; this.studentDraw();
  };
  P.setPicInfo = function (key, regions) {
    st(this).regions[key] = (regions || []).filter(function (r) { return r && r.box && r.box.length === 4; });
  };

  /* ───────── what does this rectangle cover? ───────── */
  P.describeRect = function (r) {
    var out = { lines: [], formulas: [], pictures: [], things: [], near: [] }, self = this, regs = st(this).regions;
    (this.world || []).forEach(function (e) {
      var el = e.el; if (!el || el.kind === 'mark') return;
      if (el.kind === 'image' && el.img) {
        var im = el.img, ib = inter(r, [im.x, im.y, im.w, im.h]);
        if (!ib) return;
        var t = [(ib[0] - im.x) / im.w * 1000, (ib[1] - im.y) / im.h * 1000, ib[2] / im.w * 1000, ib[3] / im.h * 1000], cr = im.crop;
        if (cr) t = [cr[0] + t[0] * cr[2] / 1000, cr[1] + t[1] * cr[3] / 1000, t[2] * cr[2] / 1000, t[3] * cr[3] / 1000];
        t = t.map(function (v) { return Math.round(v); });
        var hit = [];
        (regs[im.key] || []).forEach(function (rg) {
          var i2 = inter(t, rg.box); if (!i2) return;
          var ov = area(i2) / Math.min(area(rg.box), area(t));
          if (ov > .3) hit.push({ id: rg.id, label: rg.label || '', text: rg.text || '', ov: ov });
        });
        hit.sort(function (a, b) { return b.ov - a.ov; });
        out.pictures.push({ id: el.id, key: im.key, area: t, regions: hit.slice(0, 5) });
      } else if (el.kind === 'text') {
        el.items.forEach(function (it) {
          if (it.t !== 'x' || it.mode !== 'text') return;
          it.lines.forEach(function (l) {
            var b = [l.x, l.y - it.lh * .1, Math.max(8, l.w), it.lh], i2 = inter(r, b);
            if (i2 && area(i2) / area(b) > .3) out.lines.push({ id: el.id, s: l.s });
          });
        });
      } else if (el.kind === 'math') {
        var bb = el.bbox, i3 = bb && inter(r, bb);
        if (i3 && area(i3) / area(bb) > .25) out.formulas.push({ id: el.id, m: el.label || '' });
      } else if (el.bbox) {
        var i4 = inter(r, el.bbox);
        if (i4 && area(i4) / Math.min(area(el.bbox), area(r)) > .3) out.things.push({ id: el.id, kind: el.kind || el.anim || '', label: el.label || '' });
      }
    });
    return out;
  };

  /* ───────── snapshots ───────── */
  // a picture of the board (or of a part of it) as a JPEG data URL. o.rect = logical rectangle; o.inkOnly = only the unsent writing, dark on white.
  P.snapshot = function (o) {
    o = o || {};
    var H = this.profile.H, S = this.S, rect = o.rect, sx = 0, sy = 0, sw = this.pxW, sh = this.pxH;
    if (rect) {
      var x0 = KB.clamp(rect[0], 0, 1000), y0 = KB.clamp(rect[1], 0, H), x1 = KB.clamp(rect[0] + rect[2], 0, 1000), y1 = KB.clamp(rect[1] + rect[3], 0, H);
      sx = x0 * S; sy = y0 * S; sw = Math.max(24, (x1 - x0) * S); sh = Math.max(24, (y1 - y0) * S);
    }
    var k = o.rect ? KB.clamp((o.targetW || 900) / sw, .6, 2.2) : Math.min(1, (o.targetW || 1100) / sw);
    var c = document.createElement('canvas'); c.width = Math.max(8, Math.round(sw * k)); c.height = Math.max(8, Math.round(sh * k));
    var g = c.getContext('2d');
    if (o.inkOnly) {
      g.fillStyle = '#fff'; g.fillRect(0, 0, c.width, c.height);
      g.setTransform(S * k, 0, 0, S * k, -sx * k, -sy * k);
      this.drawStudentItems(g, (this.stu ? this.stu.items : []), { color: '#111', wScale: 1.25, onlyUnsent: o.onlyUnsent !== false });
    } else {
      var self = this;
      ['bg', 'ink', 'anim', 'fx', 'stu'].forEach(function (n) { if (self.cv[n]) g.drawImage(self.cv[n], sx, sy, sw, sh, 0, 0, c.width, c.height); });
    }
    return c.toDataURL('image/jpeg', .82);
  };
  // the unsent writing: -> {rect, crop (dark on white), board (whole board), desc} and the strokes are marked as sent
  P.takeInk = function () {
    var s = st(this), items = this.unsent();
    if (!items.length) return null;
    var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, self = this;
    items.forEach(function (it) { var b = self.ptsBox(it.pts); x0 = Math.min(x0, b[0]); y0 = Math.min(y0, b[1]); x1 = Math.max(x1, b[0] + b[2]); y1 = Math.max(y1, b[1] + b[3]); });
    var H = this.profile.H, pad = 26;
    var rect = [Math.max(0, x0 - pad), Math.max(0, y0 - pad), Math.min(1000, x1 + pad) - Math.max(0, x0 - pad), Math.min(H, y1 + pad) - Math.max(0, y0 - pad)];
    var out = { rect: rect, crop: this.snapshot({ rect: rect, inkOnly: true, targetW: 900 }), board: this.snapshot({ targetW: 1100 }), desc: this.describeRect(rect), count: items.length };
    items.forEach(function (it) { it.sent = true; });
    clearTimeout(s.idle);
    return out;
  };
  P.studentHas = function () { return !!(this.stu && this.stu.items.length); };
})(window);
