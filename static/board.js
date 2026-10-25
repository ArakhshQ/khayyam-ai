/* Khayyam chalkboard — renderer and lesson player.
   Four stacked canvases: board (green slate + eraser ghosts), ink (everything written), anim (moving examples), fx (chalk tip, dust, eraser, pointer).
   Needs board_core.js, board_math.js, board_model.js. */
(function (global) {
  'use strict';
  var KB = global.KB;
  var TAU = Math.PI * 2;

  function Board(host, opts) {
    opts = opts || {};
    this.host = host;
    this.dpr = Math.min(global.devicePixelRatio || 1, 1.75);
    this.listeners = {};
    this.speed = 1;
    this.playing = false;
    this.lesson = null; this.model = null; this.profile = null;
    this.sceneIdx = 0; this.st = 0; this.clock = 0; this.run = [];
    this.world = []; this.anims = []; this.ghosts = []; this.particles = []; this.fxs = []; this.ptr = null;
    this.stack = [];
    this.live = false; this.atEnd = false; this.askDone = {}; this.waitingAsk = -1;
    this.last = 0; this.raf = 0; this.animDirty = false; this.fxBusy = false;
    this.grains = {};
    this.wrap = document.createElement('div'); this.wrap.className = 'kb-wrap';
    this.cv = {}; this.cx = {};
    var self = this;
    ['bg', 'ink', 'anim', 'fx', 'stu'].forEach(function (n) {
      var c = document.createElement('canvas'); c.className = 'kb-' + n; self.wrap.appendChild(c); self.cv[n] = c; self.cx[n] = c.getContext('2d');
    });
    var tray = document.createElement('div'); tray.className = 'kb-tray';
    tray.innerHTML = '<i class="kb-chalk" style="width:34px;inset-inline-start:34px;background:#f4f1e2"></i><i class="kb-chalk" style="width:25px;inset-inline-start:76px;background:#f0cf6a;transform:rotate(-3deg)"></i><i class="kb-chalk" style="width:19px;inset-inline-start:110px;background:#8fcbf2"></i><i class="kb-eraser"></i>';
    this.wrap.appendChild(tray);
    host.appendChild(this.wrap);
    this._tick = this.tick.bind(this);
    if (global.ResizeObserver) { this.ro = new ResizeObserver(function () { self.onResize(); }); this.ro.observe(host); }
    else global.addEventListener('resize', function () { self.onResize(); });
    this.chooseProfile();
    this.sizeCanvases();
    this.paintBg();
    if (this.studentWire) this.studentWire();
  }
  KB.Board = Board;
  var P = Board.prototype;

  P.on = function (n, fn) { (this.listeners[n] = this.listeners[n] || []).push(fn); return this; };
  P.emit = function (n, a, b) { (this.listeners[n] || []).forEach(function (f) { try { f(a, b); } catch (e) { if (global.console) console.error(e); } }); };

  /* ───────── sizing ───────── */
  P.hostBox = function () {
    var cs = global.getComputedStyle(this.host), px = function (v) { return parseFloat(v) || 0; };
    return { w: Math.max(200, (this.host.clientWidth || 800) - px(cs.paddingLeft) - px(cs.paddingRight)), h: Math.max(150, (this.host.clientHeight || 500) - px(cs.paddingTop) - px(cs.paddingBottom)) };
  };
  P.chooseProfile = function () {
    var hb = this.hostBox(), cw = hb.w, ch = hb.h, asp = cw / ch, W = 1000;
    var wide = asp >= 1.2;
    var H = wide ? KB.clamp(W / asp, 520, 700) : KB.clamp(W / asp, 820, 1300);
    var next = { wide: wide, H: Math.round(H), ts: wide ? (cw < 700 ? .96 : .78) : 1.04, cw: cw, ch: ch };
    var changed = !this.profile || this.profile.wide !== next.wide || Math.abs(this.profile.H - next.H) > 24 || this.profile.ts !== next.ts;
    this.nextProfile = next;
    return changed;
  };
  P.sizeCanvases = function () {
    var pr = this.profile || this.nextProfile, W = 1000, H = pr.H;
    var hb = this.hostBox(), cw = hb.w, ch = hb.h;
    var wpx = Math.min(cw, ch * W / H), hpx = wpx * H / W;
    this.wrap.style.width = Math.round(wpx) + 'px'; this.wrap.style.height = Math.round(hpx) + 'px';
    this.pxW = Math.round(wpx * this.dpr); this.pxH = Math.round(hpx * this.dpr);
    this.S = this.pxW / W;
    var self = this;
    ['bg', 'ink', 'anim', 'fx', 'stu'].forEach(function (n) { self.cv[n].width = self.pxW; self.cv[n].height = self.pxH; });
  };
  P.onResize = function () {
    var self = this;
    clearTimeout(this._rt);
    this._rt = setTimeout(function () {
      if (!self.lesson) { self.chooseProfile(); self.profile = self.nextProfile; self.sizeCanvases(); self.paintBg(); return; }
      var rebuild = self.chooseProfile();
      if (rebuild) { self.profile = self.nextProfile; self.rebuild(true); }
      else { self.sizeCanvases(); self.paintBg(); self.redrawAll(); }
    }, 120);
  };

  /* ───────── slate ───────── */
  P.paintBg = function () {
    var c = this.cx.bg, w = this.pxW, h = this.pxH, r = KB.rng(5150);
    var g = c.createLinearGradient(0, 0, w * .6, h);
    g.addColorStop(0, '#265a49'); g.addColorStop(.5, '#1d4b3d'); g.addColorStop(1, '#163c31');
    c.fillStyle = g; c.fillRect(0, 0, w, h);
    // cloth wipes: long faint streaks, the way a real board looks after it has been cleaned
    for (var i = 0; i < 14; i++) {
      var y = r() * h, x = r() * w * .5, len = w * (.3 + r() * .6), t = (14 + r() * 60) * this.S / 2, a0 = .004 + r() * .006;
      var vg = c.createLinearGradient(0, 0, 0, t);
      vg.addColorStop(0, 'rgba(235,244,238,0)'); vg.addColorStop(.5, 'rgba(235,244,238,' + a0 + ')'); vg.addColorStop(1, 'rgba(235,244,238,0)');
      c.save(); c.translate(0, y); c.rotate((r() - .5) * .05); c.fillStyle = vg;
      var segs = 10;
      for (var q = 0; q < segs; q++) { c.globalAlpha = Math.sin(Math.PI * (q + .5) / segs); c.fillRect(x + len * q / segs, 0, len / segs + 1, t); }
      c.restore();
    }
    c.globalAlpha = 1;
    // fine grain
    var tile = document.createElement('canvas'); tile.width = tile.height = 160;
    var tc = tile.getContext('2d'), img = tc.createImageData(160, 160), d = img.data, rr = KB.rng(77);
    for (var k = 0; k < d.length; k += 4) { var v = rr(); d[k] = d[k + 1] = d[k + 2] = v < .5 ? 0 : 255; d[k + 3] = v < .5 ? 10 + rr() * 22 : (v > .9 ? 8 + rr() * 14 : 0); }
    tc.putImageData(img, 0, 0);
    c.fillStyle = c.createPattern(tile, 'repeat'); c.fillRect(0, 0, w, h);
    // light from the upper left, dark edges
    var rg = c.createRadialGradient(w * .3, h * .1, 0, w * .5, h * .5, Math.max(w, h) * .85);
    rg.addColorStop(0, 'rgba(255,255,255,.09)'); rg.addColorStop(.55, 'rgba(255,255,255,0)'); rg.addColorStop(1, 'rgba(0,0,0,.34)');
    c.fillStyle = rg; c.fillRect(0, 0, w, h);
    var self = this;
    this.ghosts.forEach(function (gh) { self.drawGhost(gh); });
  };
  P.drawGhost = function (gh) {
    var c = this.cx.bg, S = this.S;
    c.save(); c.setTransform(S, 0, 0, S, 0, 0);
    var x = gh[0], y = gh[1], w = gh[2], h = gh[3], r = KB.rng(gh[4]);
    for (var i = 0; i < 4; i++) {
      var yy = y + h * (.05 + i * .24), hh = h * (.14 + r() * .16);
      var g = c.createLinearGradient(x, 0, x + w, 0);
      g.addColorStop(0, 'rgba(245,250,244,0)'); g.addColorStop(.12, 'rgba(245,250,244,' + (.05 + r() * .05) + ')'); g.addColorStop(.85, 'rgba(245,250,244,' + (.04 + r() * .04) + ')'); g.addColorStop(1, 'rgba(245,250,244,0)');
      c.fillStyle = g; c.fillRect(x, yy, w, hh);
    }
    c.restore();
  };

  /* ───────── chalk pens ───────── */
  P.pattern = function (ctx, hex, device) {
    var key = hex + (device ? 'd' : 'u');
    if (!this.grains[key]) {
      var tile = KB.makeGrain(hex.length === 7 ? hex : '#f6f3e6');
      var pat = ctx.createPattern(tile, 'repeat');
      if (!device && pat.setTransform && global.DOMMatrix) { try { pat.setTransform(new DOMMatrix([1 / this.S, 0, 0, 1 / this.S, 0, 0])); } catch (e) { } }
      this.grains[key] = pat;
    }
    return this.grains[key];
  };
  P.setLogical = function (ctx) { ctx.setTransform(this.S, 0, 0, this.S, 0, 0); };

  // draw vertices i0..i1 of polyline `it.pl`
  P.strokeSeg = function (ctx, it, i0, i1, startCap, endCap) {
    var pts = it.pl.pts, pat = this.pattern(ctx, it.color), w = it.w;
    ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.strokeStyle = pat; ctx.fillStyle = pat;
    ctx.globalAlpha = 1; ctx.lineWidth = w;
    ctx.beginPath(); ctx.moveTo(pts[i0][0], pts[i0][1]);
    for (var k = i0 + 1; k <= i1; k++) ctx.lineTo(pts[k][0], pts[k][1]);
    ctx.stroke();
    // fuzzy second pass, slightly off, thinner: gives the dragged-chalk edge
    ctx.globalAlpha = .5; ctx.lineWidth = w * .5;
    ctx.beginPath(); ctx.moveTo(pts[i0][0] + .5, pts[i0][1] - .4);
    for (var j = i0 + 1; j <= i1; j++) ctx.lineTo(pts[j][0] + .5, pts[j][1] - .4);
    ctx.stroke();
    ctx.globalAlpha = 1;
  };

  /* ───────── sprites (text and formulas) ───────── */
  P.raster = function (it) {
    if (it._r && it._r.S === this.S) return it._r;
    var S = this.S, pad, minX, minY, maxX, maxY, c, x, r = KB.rng(KB.hash(it.mode + (it.lines ? it.lines[0].s : it.lay.ops.length)));
    if (it.mode === 'text') {
      pad = it.size * .4;
      minX = 1e9; maxX = -1e9; minY = it.lines[0].y; maxY = it.lines[it.lines.length - 1].y + it.lh;
      it.lines.forEach(function (l) { minX = Math.min(minX, l.x); maxX = Math.max(maxX, l.x + l.w); });
    } else {
      pad = it.size * .35; minX = it.x; maxX = it.x + it.boxW; minY = it.y - it.lay.asc; maxY = it.y + it.lay.desc;
    }
    c = document.createElement('canvas');
    c.width = Math.ceil((maxX - minX + 2 * pad) * S); c.height = Math.ceil((maxY - minY + 2 * pad) * S);
    x = c.getContext('2d');
    var pat = this.pattern(x, it.color, true);
    x.fillStyle = pat; x.strokeStyle = pat;
    var fam = KB.own(KB.FAM, it.fam) ? KB.FAM[it.fam] : KB.FAM.body;
    if (it.mode === 'text') {
      it.lines.forEach(function (l) {
        if (!l.s) return;
        x.save();
        var bx = (l.rtl ? l.x + l.w : l.x) - minX + pad, by = l.y - minY + pad + it.lh * .64;
        x.translate(bx * S, by * S); x.rotate((r() - .5) * .006);
        x.font = it.weight + ' ' + (it.size * S) + 'px ' + fam; x.direction = l.rtl ? 'rtl' : 'ltr'; x.textAlign = l.rtl ? 'right' : 'left'; x.textBaseline = 'alphabetic';
        x.shadowColor = 'rgba(246,243,230,.38)'; x.shadowBlur = 2.2 * S;
        x.globalAlpha = 1; x.fillText(l.s, 0, 0);
        x.shadowBlur = 0; x.globalAlpha = .45; x.fillText(l.s, .7 * S / 1.2, -.5 * S / 1.2);
        x.restore();
      });
    } else {
      var lay = it.lay;
      lay.ops.forEach(function (o) {
        if (o.k === 't') {
          x.font = '500 ' + (o.size * S) + 'px ' + KB.FAM.body; x.direction = 'ltr'; x.textAlign = 'left'; x.textBaseline = 'alphabetic';
          x.globalAlpha = .94; x.fillText(o.s, (o.x + pad) * S, (o.y + lay.asc + pad) * S);
          x.globalAlpha = .4; x.fillText(o.s, (o.x + pad) * S + .6, (o.y + lay.asc + pad) * S - .5);
        } else {
          x.lineWidth = o.w * S; x.lineCap = 'round'; x.lineJoin = 'round'; x.globalAlpha = .94;
          x.beginPath(); o.pts.forEach(function (p, i) { var px = (p[0] + pad) * S, py = (p[1] + lay.asc + pad) * S; if (i) x.lineTo(px, py); else x.moveTo(px, py); }); x.stroke();
        }
      });
    }
    // rough the edges and speckle the body: chalk never covers a board evenly
    if (!this._erode) {
      var et = document.createElement('canvas'); et.width = et.height = 128; var ex = et.getContext('2d'), eimg = ex.createImageData(128, 128), ed = eimg.data, er = KB.rng(4242);
      for (var q = 0; q < ed.length; q += 4) { var v = er(); ed[q] = ed[q + 1] = ed[q + 2] = 0; ed[q + 3] = v < .09 ? 150 + er() * 105 : (v < .2 ? 40 + er() * 60 : 0); }
      ex.putImageData(eimg, 0, 0); this._erode = et;
    }
    x.save(); x.globalCompositeOperation = 'destination-out'; x.globalAlpha = .85; x.fillStyle = x.createPattern(this._erode, 'repeat'); x.fillRect(0, 0, c.width, c.height); x.restore();
    it._r = { S: S, canvas: c, ox: minX - pad, oy: minY - pad, pad: pad };
    return it._r;
  };
  // blit the newly revealed part of a sprite. Returns pen position (logical) or null.
  P.revealSprite = function (ctx, it, st, p) {
    var rs = this.raster(it), S = this.S, pad = rs.pad, pen = null;
    ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0);
    var blit = function (lo, hi, top, bottom) {          // logical x range and y range
      var sx = Math.max(0, Math.floor((lo - rs.ox) * S)), ex = Math.min(rs.canvas.width, Math.ceil((hi - rs.ox) * S));
      var sy = Math.max(0, Math.floor((top - rs.oy) * S)), ey = Math.min(rs.canvas.height, Math.ceil((bottom - rs.oy) * S));
      if (ex - sx < 1 || ey - sy < 1) return;
      ctx.drawImage(rs.canvas, sx, sy, ex - sx, ey - sy, Math.round(rs.ox * S) + sx, Math.round(rs.oy * S) + sy, ex - sx, ey - sy);
    };
    if (it.mode === 'text') {
      var total = 0; it.lines.forEach(function (l) { l._c = Math.max(1, l.s.replace(/\s/g, '').length); total += l._c; });
      var acc = 0; st.lr = st.lr || [];
      for (var i = 0; i < it.lines.length; i++) {
        var l = it.lines[i], f = KB.clamp((p * total - acc) / l._c, 0, 1); acc += l._c;
        var prev = st.lr[i] || 0;
        if (f > prev || (f >= 1 && prev < 1)) {
          var top = l.y - pad * .6, bot = l.y + it.lh + pad * .6;
          var cov = function (fr, left) {
            if (l.rtl) return left ? (fr >= 1 ? l.x - pad : l.x + l.w * (1 - fr)) : l.x + l.w + pad;
            return left ? l.x - pad : (fr >= 1 ? l.x + l.w + pad : l.x + l.w * fr);
          };
          if (l.rtl) blit(cov(f, true), prev === 0 ? cov(0, false) : cov(prev, true), top, bot);
          else blit(prev === 0 ? cov(0, true) : cov(prev, false), cov(f, false), top, bot);
          st.lr[i] = f;
        }
        if (f > 0 && f < 1) pen = [l.rtl ? l.x + l.w * (1 - f) : l.x + l.w * f, l.y + it.lh * .55];
      }
    } else {
      var prevM = st.m || 0;
      if (p > prevM) {
        var lo = prevM === 0 ? it.x - pad : it.x + it.boxW * prevM, hi = p >= 1 ? it.x + it.boxW + pad : it.x + it.boxW * p;
        blit(lo, hi, it.y - it.lay.asc - pad, it.y + it.lay.desc + pad); st.m = p;
        if (p < 1) pen = [it.x + it.boxW * p, it.y - it.lay.asc * .3];
      }
    }
    ctx.restore();
    return pen;
  };

  /* ───────── drawing an element up to local time t ───────── */
  P.drawEl = function (entry, t, instant) {
    var el = entry.el, ctx = this.cx.ink, pen = null, self = this;
    if (!entry.sts) entry.sts = el.items.map(function () { return { idx: 0, started: false, ended: false }; });
    el.items.forEach(function (it, i) {
      var st = entry.sts[i];
      if (it.t === 's') {
        var p = instant ? 1 : KB.clamp((t - it.t0) / it.d, 0, 1);
        if (p <= 0) return;
        var pts = it.pl.pts, target = pts.length - 1;
        if (p < 1) { target = 0; var d = p * it.pl.len; while (target < pts.length - 1 && it.pl.cum[target + 1] <= d) target++; }
        self.setLogical(ctx);
        if (target > st.idx || (!st.started && target >= 0 && pts.length > 1 && p > 0)) {
          if (target === st.idx && st.idx === 0) { /* nothing yet beyond the start */ }
          else { self.strokeSeg(ctx, it, st.idx, target, !st.started, p >= 1 && !st.ended); st.idx = target; st.started = true; if (p >= 1) st.ended = true; }
        }
        if (p < 1) pen = KB.pointAt(it.pl, p * it.pl.len);
      } else if (it.t === 'i') {
        var qi = instant ? 1 : KB.clamp((t - it.t0) / it.d, 0, 1);
        if (qi <= 0) return;
        var ip = self.paintImage(ctx, it, st, qi);
        if (ip) pen = ip;
      } else if (it.t === 'x') {
        var q = instant ? 1 : KB.clamp((t - it.t0) / it.d, 0, 1);
        if (q <= 0) return;
        var pp = self.revealSprite(ctx, it, st, q);
        if (pp) pen = pp;
      }
    });
    return pen;
  };
  P.finishEl = function (entry) { this.drawEl(entry, 1e6, true); };

  // a picture the student brought: pinned to the board, revealed top to bottom (it waits for the file if it has not loaded yet)
  P.paintImage = function (ctx, it, st, p) {
    var self = this, img = KB.getImage(it.src, function () { if (self.model) self.redrawAll(); });
    if (!img) return null;
    var prev = st.ih || 0, hh = it.h * p;
    if (hh <= prev + .5 && p < 1) return null;
    var cr = it.crop, sx = 0, sy = 0, sw = img.naturalWidth, sh = img.naturalHeight;
    if (cr) { sx = img.naturalWidth * cr[0] / 1000; sy = img.naturalHeight * cr[1] / 1000; sw = img.naturalWidth * cr[2] / 1000; sh = img.naturalHeight * cr[3] / 1000; }
    self.setLogical(ctx);
    ctx.save();
    ctx.beginPath(); ctx.rect(it.x - 1, it.y + prev - 1, it.w + 2, hh - prev + 2); ctx.clip();
    ctx.shadowColor = 'rgba(0,0,0,.4)'; ctx.shadowBlur = 14; ctx.shadowOffsetY = 3;
    ctx.fillStyle = '#f4f1e4'; ctx.fillRect(it.x - 3, it.y - 3, it.w + 6, it.h + 6);     // a little paper border
    ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;
    ctx.globalAlpha = .97;
    ctx.drawImage(img, sx, sy, sw, sh, it.x, it.y, it.w, it.h);
    ctx.restore();
    st.ih = hh;
    return p < 1 ? [it.x + it.w * (.15 + .7 * ((hh / 40) % 1)), it.y + hh] : null;
  };

  /* ───────── fx: chalk tip, dust, eraser, pointer ───────── */
  P.dust = function (x, y) {
    if (this.particles.length > 70) return;
    var r = Math.random;
    for (var i = 0; i < 2; i++) this.particles.push({ x: x + (r() - .5) * 5, y: y + (r() - .5) * 3, vx: (r() - .5) * 14, vy: 6 + r() * 20, life: .8 + r() * .7, age: 0, s: .8 + r() * 1.3 });
  };
  P.drawFx = function (dt) {
    var c = this.cx.fx, S = this.S, any = false;
    c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, this.pxW, this.pxH);
    c.setTransform(S, 0, 0, S, 0, 0);
    if (this.pen && this.playing) {
      var g = c.createRadialGradient(this.pen[0], this.pen[1], 0, this.pen[0], this.pen[1], 13);
      g.addColorStop(0, 'rgba(255,253,240,.85)'); g.addColorStop(.35, 'rgba(255,253,240,.28)'); g.addColorStop(1, 'rgba(255,253,240,0)');
      c.fillStyle = g; c.beginPath(); c.arc(this.pen[0], this.pen[1], 13, 0, TAU); c.fill();
      this.dust(this.pen[0], this.pen[1] + 3); any = true;
    }
    var live = [];
    for (var i = 0; i < this.particles.length; i++) {
      var p = this.particles[i]; p.age += dt; if (p.age >= p.life) continue;
      p.x += p.vx * dt; p.y += p.vy * dt; p.vy += 40 * dt;
      c.fillStyle = 'rgba(246,243,230,' + (.5 * (1 - p.age / p.life)) + ')'; c.beginPath(); c.arc(p.x, p.y, p.s, 0, TAU); c.fill();
      live.push(p);
    }
    this.particles = live; if (live.length) any = true;
    var now = this.clock, fl = [];
    for (var j = 0; j < this.fxs.length; j++) {
      var f = this.fxs[j];
      if (now > f.end) { if (f.done) f.done(); continue; }
      f.draw(c, (now - f.start) / (f.end - f.start)); fl.push(f); any = true;
    }
    this.fxs = fl;
    var pm = false;
    if (this.ptr) { c.setTransform(S, 0, 0, S, 0, 0); pm = this.drawPointer(c); if (pm) any = true; }
    this.fxBusy = any;
    if (!any && !this.ptr) { c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, this.pxW, this.pxH); }
  };

  /* ───────── live animations ───────── */
  P.strokesAt = function (ctx, strokes, cx, cy, size, rot, color, w) {
    var pat = this.pattern(ctx, color), k = size / 100, cs = Math.cos(rot), sn = Math.sin(rot);
    ctx.strokeStyle = pat; ctx.lineWidth = w || 3.2; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    for (var i = 0; i < strokes.length; i++) {
      var pts = strokes[i];
      ctx.globalAlpha = .93; ctx.beginPath();
      for (var j = 0; j < pts.length; j++) {
        var x = (pts[j][0] - 50) * k, y = (pts[j][1] - 50) * k, rx = x * cs - y * sn, ry = x * sn + y * cs;
        if (j) ctx.lineTo(cx + rx, cy + ry); else ctx.moveTo(cx + rx, cy + ry);
      }
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  };
  P.drawAnims = function () {
    var c = this.cx.anim, S = this.S;
    c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, this.pxW, this.pxH); c.setTransform(S, 0, 0, S, 0, 0);
    var moving = false, self = this;
    this.anims.forEach(function (a) {
      var t = Math.max(0, self.clock - a.t0), p = KB.clamp(t / a.dur, 0, 1), f = a.f, A = a.a;
      if (a.loop || p < 1) moving = true;
      var L = function (x, y) { return [f.ox + x * f.s, f.oy + y * f.s]; };
      var col = a.color, k = a.anim, sz = a.size * f.s;
      if (k === 'move') {
        var q = KB.ease(p), fr = A.from || [10, 50], to = A.to || [90, 50], pos = L(fr[0] + (to[0] - fr[0]) * q, fr[1] + (to[1] - fr[1]) * q);
        self.strokesAt(c, a.iconStrokes, pos[0], pos[1] + (A.hop ? -Math.abs(Math.sin(p * Math.PI * (A.hop | 0))) * 10 : 0), sz, (A.spin ? p * TAU * A.spin : 0), col, 3.2);
      } else if (k === 'fall') {
        var x0 = A.x != null ? A.x : 50, y0 = A.from_y != null ? A.from_y : 10, y1 = A.to_y != null ? A.to_y : 82, y;
        var fall = .6, tt = p;
        if (tt < fall) { var u = tt / fall; y = y0 + (y1 - y0) * u * u; }
        else { var b = (tt - fall) / (1 - fall); y = y1 - Math.abs(Math.sin(b * Math.PI * 2.5)) * (y1 - y0) * .22 * (1 - b); }
        var gp = L(x0, y1 + a.size * .55);
        var gs = [[[50 - 30, 50], [50 + 30, 50]]]; self.strokesAt(c, gs, gp[0], gp[1], 100 * f.s * .6, 0, '#a8c4b7', 2.4);
        var pp = L(x0, y); self.strokesAt(c, a.iconStrokes, pp[0], pp[1], sz, tt < fall ? p * 3 : 0, col, 3.2);
      } else if (k === 'swing') {
        var piv = L(50, 8), len = (A.len != null ? A.len : 62) * f.s, amp = (A.angle != null ? A.angle : 35) * Math.PI / 180, per = A.period != null ? A.period : 2.4;
        var th = amp * Math.cos(TAU * t / per);
        var bx = piv[0] + Math.sin(th) * len, by = piv[1] + Math.cos(th) * len;
        self.strokesAt(c, [[[0, 0], [100, 0]]], piv[0], piv[1] - 2, 36 * f.s, 0, '#e8e4d2', 3.4);
        c.strokeStyle = self.pattern(c, col); c.lineWidth = 2.6; c.globalAlpha = .9; c.beginPath(); c.moveTo(piv[0], piv[1]); c.lineTo(bx, by); c.stroke(); c.globalAlpha = 1;
        self.strokesAt(c, a.iconStrokes, bx, by + sz * .35, sz * .75, 0, col, 3.2);
      } else if (k === 'orbit') {
        var ctr = L(50, 50);
        var cst = KB.iconStrokes(A.icon && KB.own(KB.ICONS, A.icon) ? A.icon : 'sun', 3);
        a.bodies.forEach(function (bd) {
          var r = bd.r * f.s;
          c.strokeStyle = self.pattern(c, '#8fb3a3'); c.lineWidth = 1.8; c.globalAlpha = .55; c.setLineDash([6, 8]); c.beginPath(); c.arc(ctr[0], ctr[1], r, 0, TAU); c.stroke(); c.setLineDash([]); c.globalAlpha = 1;
          var ang = TAU * t / bd.period - Math.PI / 2;
          self.strokesAt(c, bd.strokes, ctr[0] + Math.cos(ang) * r, ctr[1] + Math.sin(ang) * r, bd.size * f.s * 2, ang, col, 3);
        });
        self.strokesAt(c, cst, ctr[0], ctr[1], 24 * f.s, 0, KB.color('yellow'), 3.2);
      } else if (k === 'wave') {
        var amp2 = (A.amp != null ? A.amp : 22) * f.s, cyc = A.cycles != null ? A.cycles : 2, per2 = A.period != null ? A.period : 2, pts = [], n = 90, mid = L(0, 50)[1], x0w = f.ox, wd = 100 * f.s;
        for (var i = 0; i <= n; i++) { var u2 = i / n; pts.push([x0w + wd * u2, mid + Math.sin(TAU * (cyc * u2 - t / per2)) * amp2 * (A.fade ? (1 - u2 * .6) : 1)]); }
        c.strokeStyle = self.pattern(c, col); c.lineWidth = 3.6; c.lineJoin = 'round'; c.globalAlpha = .93; c.beginPath(); pts.forEach(function (pt, i2) { if (i2) c.lineTo(pt[0], pt[1]); else c.moveTo(pt[0], pt[1]); }); c.stroke(); c.globalAlpha = 1;
        c.lineWidth = 1.6; c.globalAlpha = .45; c.setLineDash([5, 7]); c.beginPath(); c.moveTo(x0w, mid); c.lineTo(x0w + wd, mid); c.stroke(); c.setLineDash([]); c.globalAlpha = 1;
      } else if (k === 'grow') {
        var cg = L(50, 50), q2 = KB.ease(p);
        self.strokesAt(c, a.iconStrokes, cg[0], cg[1] + (1 - q2) * sz * .3, sz * (.15 + .85 * q2), 0, col, 3.2);
      } else if (k === 'spin') {
        var cs2 = L(A.at ? A.at[0] : 50, A.at ? A.at[1] : 50);
        self.strokesAt(c, a.iconStrokes, cs2[0], cs2[1], sz, TAU * (A.turns != null ? A.turns : 1) * (a.loop ? t / (A.period || 2) : p), col, 3.2);
      } else if (k === 'count') {
        var v = a.from + (a.to - a.from) * KB.ease(p), s = String(Math.round(v)), fs = (A.fontSize != null ? A.fontSize : 80) * f.s, ct = L(50, 50);
        var txt = (A.digits === 'en') ? s : KB.faDigits(s);
        c.font = '700 ' + fs + 'px ' + KB.FAM.body; c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillStyle = self.pattern(c, col); c.globalAlpha = .95; c.fillText(txt, ct[0], ct[1]); c.globalAlpha = 1;
      } else if (k === 'fill') {
        var lv = KB.clamp(A.to != null ? A.to : 1, 0, 1) * KB.ease(p), shape = a.shape, col2 = KB.color(A.fillColor || 'blue');
        c.save();
        if (shape === 'bar') {
          var tl = L(5, 38), bw = 90 * f.s, bh = 24 * f.s;
          c.beginPath(); c.rect(tl[0], tl[1], bw * lv, bh); c.fillStyle = self.pattern(c, col2); c.globalAlpha = .75; c.fill(); c.globalAlpha = 1;
          c.strokeStyle = self.pattern(c, col); c.lineWidth = 3.4; c.strokeRect(tl[0], tl[1], bw, bh);
        } else if (shape === 'pie') {
          var cp = L(50, 50), R = 40 * f.s;
          c.beginPath(); c.moveTo(cp[0], cp[1]); c.arc(cp[0], cp[1], R * .96, -Math.PI / 2, -Math.PI / 2 + TAU * lv); c.closePath(); c.fillStyle = self.pattern(c, col2); c.globalAlpha = .75; c.fill(); c.globalAlpha = 1;
          c.strokeStyle = self.pattern(c, col); c.lineWidth = 3.4; c.beginPath(); c.arc(cp[0], cp[1], R, 0, TAU); c.stroke();
        } else {
          var a1 = L(24, 16), a2 = L(76, 16), b2 = L(68, 88), b1 = L(32, 88);
          c.beginPath(); c.moveTo(a1[0], a1[1]); c.lineTo(a2[0], a2[1]); c.lineTo(b2[0], b2[1]); c.lineTo(b1[0], b1[1]); c.closePath(); c.save(); c.clip();
          var top = b1[1] - (b1[1] - a1[1]) * lv; c.fillStyle = self.pattern(c, col2); c.globalAlpha = .75; c.fillRect(a1[0] - 5, top, (a2[0] - a1[0]) + 10, b1[1] - top + 2); c.restore();
          c.strokeStyle = self.pattern(c, col); c.lineWidth = 3.6; c.lineJoin = 'round'; c.beginPath(); c.moveTo(a1[0], a1[1]); c.lineTo(b1[0], b1[1]); c.lineTo(b2[0], b2[1]); c.lineTo(a2[0], a2[1]); c.stroke();
        }
        c.restore();
      }
    });
    this.animDirty = false;
    return moving;
  };

  /* ───────── player ───────── */
  P.load = function (lesson) {
    var self = this;
    this.lesson = lesson;
    var fonts = ['500 30px Vazirmatn', '700 30px Vazirmatn', '700 30px Amiri', '400 30px Amiri'];
    var wait = Promise.race([Promise.all(fonts.map(function (f) { return document.fonts ? document.fonts.load(f, 'ابپ۱۲۳abc') : Promise.resolve(); })), new Promise(function (r) { setTimeout(r, 2500); })]);
    return wait.then(function () {
      self.chooseProfile(); self.profile = self.nextProfile; self.sizeCanvases(); self.paintBg();
      self.rebuild(false);
      self.emit('loaded', self.model);
    });
  };
  P.rebuild = function (keepPlace) {
    var idx = keepPlace ? this.sceneIdx : 0, was = this.playing, ended = keepPlace && !!this.sceneEnded;
    this.sizeCanvases(); this.paintBg();
    this.model = KB.buildModel(this.lesson, this.profile);
    this.pause();
    this.gotoScene(Math.min(idx, Math.max(0, this.model.scenes.length - 1)), false);
    if (ended && !was) this.completeScene();          // a scene that was finished stays finished when the screen is resized (nothing vanishes)
    if (was) this.play();
  };
  // draw everything of the current scene at once
  P.completeScene = function () {
    var sc = this.model && this.model.scenes[this.sceneIdx]; if (!sc || !this.run) return;
    for (var j = 0; j < sc.acts.length; j++) {
      var rr = this.run[j]; if (!rr) continue;
      if (rr.state === 0) this.beginAct(sc.acts[j], rr);
      if (rr.state === 1) { if (sc.acts[j].type === 'draw') this.finishEl(rr.entry); else if (sc.acts[j].type === 'erase') this.finishErase(sc.acts[j], rr); }
      rr.state = 2;
    }
    this.st = sc.dur; this.sceneEnded = true; this.pen = null;
  };
  P.redrawAll = function () {
    var ctxs = this.cx;
    ['ink', 'anim', 'fx'].forEach(function (n) { ctxs[n].setTransform(1, 0, 0, 1, 0, 0); ctxs[n].clearRect(0, 0, ctxs[n].canvas.width, ctxs[n].canvas.height); });
    var self = this;
    this.world.forEach(function (e) { e.sts = null; e.el.items.forEach(function (it) { it._r = null; }); self.finishEl(e); });
    this.drawAnims();
    if (this.ptr) this.drawFx(0);
    if (this.studentDraw) this.studentDraw();
  };

  // state at the start of scene i: every earlier scene is applied instantly
  P.gotoScene = function (i, autoplay) {
    if (!this.model || !this.model.scenes.length) return;
    i = KB.clamp(i, 0, this.model.scenes.length - 1);
    var ink = this.cx.ink, fx = this.cx.fx, an = this.cx.anim;
    [ink, fx, an].forEach(function (c) { c.setTransform(1, 0, 0, 1, 0, 0); c.clearRect(0, 0, c.canvas.width, c.canvas.height); });
    this.world = []; this.anims = []; this.fxs = []; this.particles = []; this.pen = null; this.ghosts = []; if (!this.live) this.ptr = null; this.paintBg();
    var alive = [], liveAnims = [], self = this;
    for (var s = 0; s < i; s++) {
      this.model.scenes[s].acts.forEach(function (a) {
        if (a.type === 'draw') alive.push(a.el);
        else if (a.type === 'anim') liveAnims.push(a.anim);
        else if (a.type === 'erase') {
          if (a.targets === 'all') { alive = []; liveAnims = []; }
          else { alive = alive.filter(function (e) { return a.targets.indexOf(e.id) < 0; }); liveAnims = liveAnims.filter(function (e) { return a.targets.indexOf(e.id) < 0; }); }
        }
      });
    }
    alive.forEach(function (el) { var e = { el: el, sts: null }; self.world.push(e); self.finishEl(e); });
    liveAnims.forEach(function (a) { self.anims.push(self.liveAnim(a, true)); });
    this.sceneIdx = i; this.st = 0; this.atEnd = false; this.waitingAsk = -1; this.startScene();
    if (this.anims.length) this.drawAnims();
    if (this.ptr) this.drawFx(0);
    this.emit('scene', i, this.model.scenes[i]);
    this.emit('say', this.model.scenes[i].say, i);
    if (autoplay) this.play(); else this.emit('state');
  };
  /* Replace everything from scene `from` onward (a class grows, or the student interrupts and the unplayed part is rewritten).
     Scenes before `from` keep their built objects, so a scene that is being drawn right now is never disturbed. */
  P.setTimeline = function (from, scenes) {
    if (!this.lesson || !this.model) return;
    var old = this.model, self = this, cur = this.lesson.scenes || [];
    from = KB.clamp(from | 0, 0, cur.length);
    this.lesson.scenes = cur.slice(0, from).concat(scenes || []);
    var m = KB.buildModel(this.lesson, this.profile);
    var keep = Math.min(from, old.scenes.length, m.scenes.length);
    for (var i = 0; i < keep; i++) m.scenes[i] = old.scenes[i];
    this.model = m;
    Object.keys(this.askDone).forEach(function (k) { if (+k >= from) delete self.askDone[k]; });
    this.emit('timeline', m.scenes.length, from);
    if (this.atEnd && this.sceneIdx < m.scenes.length - 1) { this.atEnd = false; this.gotoScene(this.sceneIdx + 1, true); }
  };
  P.liveAnim = function (a, instant) {
    return { id: a.id, anim: a.anim, a: a.a, f: a.f, size: a.size, dur: a.dur, loop: a.loop, color: a.color, iconStrokes: a.iconStrokes, shape: a.shape, from: a.from, to: a.to, bodies: a.bodies, t0: instant ? this.clock - a.dur - 1 : this.clock };
  };
  P.startScene = function () {
    var sc0 = this.model.scenes[this.sceneIdx];
    this.run = sc0.acts.map(function () { return { state: 0 }; });
    this.sceneEnded = false;
    if (sc0.turn && this.live) this.emit('turn', sc0.turn);
  };
  P.play = function () {
    if (!this.model) return;
    var last = this.model.scenes.length - 1;
    if (this.sceneEnded && this.sceneIdx < last) {            // a scene that was held at its end (question asked, or paused there)
      if (this.waitingAsk === this.sceneIdx) { this.askDone[this.sceneIdx] = true; this.waitingAsk = -1; }
      this.gotoScene(this.sceneIdx + 1, true); return;
    }
    if (this.sceneEnded && this.sceneIdx >= last) {
      if (this.live) {                                         // the class is waiting for the teacher's next lines
        if (this.waitingAsk === this.sceneIdx) { this.askDone[this.sceneIdx] = true; this.waitingAsk = -1; }
        this.atEnd = true; this.emit('end'); return;
      }
      this.gotoScene(0, false);
    }
    this.playing = true; this.last = 0; this.emit('state');
    if (!this.raf) this.raf = requestAnimationFrame(this._tick);
  };
  P.pause = function () { this.playing = false; this.emit('state'); };
  P.toggle = function () { if (this.playing) this.pause(); else this.play(); };
  P.setSpeed = function (v) { this.speed = KB.clamp(+v || 1, .5, 2); };
  P.next = function () { if (this.model && this.sceneIdx < this.model.scenes.length - 1) this.gotoScene(this.sceneIdx + 1, this.playing); };
  P.prev = function () { if (this.model) this.gotoScene(this.st > 2 ? this.sceneIdx : this.sceneIdx - 1, this.playing); };
  P.restart = function () { this.gotoScene(0, true); };

  P.beginAct = function (a, r) {
    var self = this;
    r.state = 1;
    if (a.type === 'draw') { r.entry = { el: a.el, sts: null }; this.world.push(r.entry); }
    else if (a.type === 'anim') { this.anims.push(this.liveAnim(a.anim, false)); this.animDirty = true; }
    else if (a.type === 'erase') {
      var bb = this.eraseBox(a);
      r.bb = bb; r.removed = false;
      if (bb[2] > 1) this.fxs.push({ start: this.clock, end: this.clock + a.dur, draw: function (c, q) { self.drawEraser(c, bb, q); }, done: null });
    } else if (a.type === 'point') {
      var b = a.box, self2 = this;
      this.flyPointer(b, a.label);
      this.fxs.push({ start: this.clock, end: this.clock + a.dur, draw: function (c, q) {
        if (q < .3) return;                                    // the ring appears when the pointer arrives
        var u = (q - .3) / .7, rad = 14 + Math.sin(u * Math.PI * 4) * 5, cx = b[0] + b[2] / 2, cy = b[1] + b[3] / 2;
        c.strokeStyle = 'rgba(244,216,110,' + (.85 * (1 - u * .7)) + ')'; c.lineWidth = 3.4; c.beginPath(); c.ellipse(cx, cy, b[2] / 2 + rad, b[3] / 2 + rad * .7, 0, 0, TAU); c.stroke();
      } });
    }
  };
  /* ───────── the teacher's pointer: flies to a part of the board, like a hand reaching to show you something ───────── */
  P.flyPointer = function (box, label) {
    var tx = box[0] + box[2] * .55, ty = box[1] + box[3] * .6, W = this.model ? this.model.W : 1000, H = this.model ? this.model.H : 600;
    var from = this.ptr ? [this.ptr.tx, this.ptr.ty] : [W * .62, H + 30];
    var dist = Math.hypot(tx - from[0], ty - from[1]);
    this.ptr = { fx: from[0], fy: from[1], tx: tx, ty: ty, t0: performance.now() / 1000, fly: KB.clamp(.35 + dist / 900, .45, 1.0), arc: Math.min(110, dist * .22), label: label || '', seen: 0 };
    this.fxBusy = true; this.animDirty = true; if (!this.raf) this.raf = requestAnimationFrame(this._tick);
  };
  P.drawPointer = function (c) {
    var p = this.ptr; if (!p) return false;
    var now = performance.now() / 1000, q = KB.clamp((now - p.t0) / p.fly, 0, 1);
    var ease = function (v) { return v < .5 ? 2 * v * v : 1 - Math.pow(-2 * v + 2, 2) / 2; };
    var at = function (v) { var e = ease(v); return [p.fx + (p.tx - p.fx) * e, p.fy + (p.ty - p.fy) * e - Math.sin(Math.PI * e) * p.arc]; };
    var pos = at(q), nxt = at(Math.min(1, q + .03)), flying = q < 1;
    var ang = Math.atan2(nxt[1] - pos[1], nxt[0] - pos[0]), rest = -2.36;
    var k = KB.clamp((q - .7) / .3, 0, 1), rot = flying && q < 1 ? ang * (1 - k) + rest * k : rest;
    if (!flying) { var bob = Math.sin((now - p.t0 - p.fly) * 3.2) * 1.6; pos = [p.tx + bob, p.ty + bob]; }
    var ps = KB.clamp((this.dpr / this.S) * .62, 1, 2.4);                // the pointer and its label stay readable on a small phone board
    var sc = (1 + .28 * Math.sin(Math.PI * ease(q))) * ps;
    c.save(); c.translate(pos[0], pos[1]); c.rotate(rot); c.scale(sc, sc);
    c.shadowColor = 'rgba(244,216,110,.75)'; c.shadowBlur = 16;
    c.fillStyle = '#f4d86e'; c.strokeStyle = 'rgba(8,28,22,.9)'; c.lineWidth = 2.2; c.lineJoin = 'round';
    c.beginPath(); c.moveTo(0, 0); c.lineTo(-30, -13); c.lineTo(-22, 0); c.lineTo(-30, 13); c.closePath(); c.fill(); c.shadowBlur = 0; c.stroke();
    c.restore();
    if (flying && Math.random() < .5) this.dust(pos[0], pos[1]);
    if (!flying && p.label) {
      var W = this.model ? this.model.W : 1000, H = this.model ? this.model.H : 600, fs = 22 * ps;
      c.save(); c.font = '700 ' + fs + 'px Vazirmatn, system-ui, sans-serif';
      var tw = c.measureText(p.label).width + 26 * ps, th = fs + 18 * ps;
      var bx = KB.clamp(pos[0] + 14 * ps, 6, W - tw - 6), by = KB.clamp(pos[1] + 26 * ps, 6, H - th - 6);
      var a = KB.clamp((now - p.t0 - p.fly) / .25, 0, 1);
      c.globalAlpha = a; c.fillStyle = 'rgba(244,216,110,.96)'; c.strokeStyle = 'rgba(8,28,22,.85)'; c.lineWidth = 2;
      var r = 12; c.beginPath(); c.moveTo(bx + r, by); c.lineTo(bx + tw - r, by); c.quadraticCurveTo(bx + tw, by, bx + tw, by + r); c.lineTo(bx + tw, by + th - r); c.quadraticCurveTo(bx + tw, by + th, bx + tw - r, by + th); c.lineTo(bx + r, by + th); c.quadraticCurveTo(bx, by + th, bx, by + th - r); c.lineTo(bx, by + r); c.quadraticCurveTo(bx, by, bx + r, by); c.closePath(); c.fill(); c.stroke();
      c.fillStyle = '#10241d'; c.textBaseline = 'middle'; c.textAlign = 'center'; c.direction = KB.RTL_RE.test(p.label) ? 'rtl' : 'ltr';
      c.fillText(p.label, bx + tw / 2, by + th / 2 + 1); c.restore();
    }
    return flying || (p.label && now - p.t0 - p.fly < .3);
  };
  P.eraseBox = function (a) {
    if (a.targets === 'all') return [0, 0, this.model.W, this.model.H];
    var x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, ids = this.model.ids;
    a.targets.forEach(function (t) { var e = ids[t]; if (e && e.bbox && e.kind !== 'mark') { x0 = Math.min(x0, e.bbox[0]); y0 = Math.min(y0, e.bbox[1]); x1 = Math.max(x1, e.bbox[0] + e.bbox[2]); y1 = Math.max(y1, e.bbox[1] + e.bbox[3]); } });
    if (x0 > x1) return [0, 0, 1, 1];
    return [Math.max(0, x0 - 20), Math.max(0, y0 - 16), x1 - x0 + 40, y1 - y0 + 32];
  };
  P.drawEraser = function (c, bb, q) {
    var ink = this.cx.ink, S = this.S, passes = 2, qq = q * passes, pass = Math.min(passes - 1, Math.floor(qq)), u = qq - pass;
    var ew = Math.min(120, Math.max(60, bb[2] * .16)), eh = Math.min(70, Math.max(46, bb[3] / 3));
    var bandH = bb[3] / passes, y = bb[1] + pass * bandH + bandH / 2 - eh / 2;
    var dir = pass % 2 === 0 ? -1 : 1;                      // first pass right to left
    var x = dir < 0 ? bb[0] + bb[2] - u * (bb[2] + ew) : bb[0] - ew + u * (bb[2] + ew);
    // wipe what the eraser has crossed
    ink.save(); ink.setTransform(S, 0, 0, S, 0, 0); ink.globalCompositeOperation = 'destination-out';
    var wx = dir < 0 ? x : bb[0], ww = dir < 0 ? bb[0] + bb[2] - x + ew * .3 : x + ew - bb[0];
    ink.globalAlpha = .96; ink.fillRect(wx, bb[1] + pass * bandH - 4, Math.max(0, ww), bandH + 8);
    if (pass > 0) ink.fillRect(bb[0], bb[1], bb[2], pass * bandH);
    ink.restore();
    // eraser block
    c.save(); c.translate(x + ew / 2, y + eh / 2); c.rotate(dir * .05);
    c.fillStyle = '#e7dcc0'; c.strokeStyle = '#3a2b17'; c.lineWidth = 1.6;
    var r = 9; c.beginPath(); c.moveTo(-ew / 2 + r, -eh / 2); c.lineTo(ew / 2 - r, -eh / 2); c.quadraticCurveTo(ew / 2, -eh / 2, ew / 2, -eh / 2 + r); c.lineTo(ew / 2, eh / 2 - r); c.quadraticCurveTo(ew / 2, eh / 2, ew / 2 - r, eh / 2); c.lineTo(-ew / 2 + r, eh / 2); c.quadraticCurveTo(-ew / 2, eh / 2, -ew / 2, eh / 2 - r); c.lineTo(-ew / 2, -eh / 2 + r); c.quadraticCurveTo(-ew / 2, -eh / 2, -ew / 2 + r, -eh / 2); c.fill(); c.stroke();
    c.fillStyle = '#4a3a22'; c.fillRect(-ew / 2 + 3, -eh / 2 + 3, ew - 6, eh * .34);
    c.fillStyle = 'rgba(255,255,255,.18)'; c.fillRect(-ew / 2 + 6, eh * .08, ew - 12, 3);
    c.restore();
    if (u > .05 && Math.random() < .6) this.dust(dir < 0 ? x : x + ew, y + eh * Math.random());
  };
  P.finishErase = function (a, r) {
    var self = this;
    if (this.studentClear) { var ids_ = this.model.ids; if (a.targets === 'all' || a.targets.filter(function (t) { return (ids_[t] || {}).kind !== 'mark'; }).length >= 2) this.studentClear(); }     // the teacher wiped the page: the student's writing on it goes too
    if (a.targets === 'all') { this.world = []; this.anims = []; this.ptr = null; }
    else { this.world = this.world.filter(function (e) { return a.targets.indexOf(e.el.id) < 0; }); this.anims = this.anims.filter(function (e) { return a.targets.indexOf(e.id) < 0; }); }
    var ink = this.cx.ink; ink.setTransform(1, 0, 0, 1, 0, 0); ink.clearRect(0, 0, this.pxW, this.pxH);
    this.world.forEach(function (e) { e.sts = null; self.finishEl(e); });
    var bb = r.bb; if (bb && bb[2] > 5) { this.ghosts.push([bb[0], bb[1], bb[2], bb[3], this.ghosts.length * 7 + 3]); if (this.ghosts.length > 8) this.ghosts.shift(); this.drawGhost(this.ghosts[this.ghosts.length - 1]); }
    this.animDirty = true;
  };

  P.tick = function (now) {
    this.raf = 0;
    var dt = this.last ? Math.min(.06, (now - this.last) / 1000) : 0; this.last = now;
    var m = this.model;
    if (this.playing && m) {
      dt *= this.speed; this.clock += dt; this.st += dt;
      var sc = m.scenes[this.sceneIdx], pen = null, self = this;
      for (var i = 0; i < sc.acts.length; i++) {
        var a = sc.acts[i], r = this.run[i];
        if (r.state === 0 && this.st >= a.start) this.beginAct(a, r);
        if (r.state === 1) {
          var p = KB.clamp((this.st - a.start) / a.dur, 0, 1);
          if (a.type === 'draw') { var pp = this.drawEl(r.entry, p * a.el.dur, false); if (pp) pen = pp; if (p >= 1) { this.finishEl(r.entry); r.state = 2; } }
          else if (a.type === 'erase') { if (p >= 1) { this.finishErase(a, r); r.state = 2; } }
          else if (p >= 1) r.state = 2;
        }
      }
      this.pen = pen;
      if (this.st >= sc.dur && !this.sceneEnded) {
        // make sure nothing is left half drawn
        for (var j = 0; j < sc.acts.length; j++) { var rr = this.run[j]; if (rr.state === 0) this.beginAct(sc.acts[j], rr); if (rr.state === 1) { if (sc.acts[j].type === 'draw') this.finishEl(rr.entry); else if (sc.acts[j].type === 'erase') this.finishErase(sc.acts[j], rr); rr.state = 2; } }
        this.sceneEnded = true; this.pen = null;
        this.emit('sceneEnd', this.sceneIdx);
        var lsc = this.lesson && this.lesson.scenes ? this.lesson.scenes[this.sceneIdx] : null;
        if (lsc && lsc.ask && !this.askDone[this.sceneIdx]) {          // the teacher asked a question: wait for the student
          this.playing = false; this.waitingAsk = this.sceneIdx; this.emit('ask', this.sceneIdx); this.emit('state');
        } else if (this.sceneIdx < m.scenes.length - 1) { if (!this.holdAtEnd) this.gotoScene(this.sceneIdx + 1, true); }
        else { this.playing = false; this.atEnd = true; this.emit('end'); this.emit('state'); }
      }
      this.emit('time', this.st, sc.dur);
    }
    var moving = false;
    if ((this.anims.length && this.playing) || this.animDirty) moving = this.drawAnims();
    if (this.playing || this.fxBusy || this.particles.length) this.drawFx(dt);
    if (this.playing || moving || this.fxBusy || this.animDirty) { this.raf = requestAnimationFrame(this._tick); }
  };

  /* A detour: the teacher stops the lesson and draws something else (an example the student asked for).
     The lesson's board is shelved and brought back by resume(). */
  P.detour = function (lesson) {
    var self = this, snapshot = { lesson: this.lesson, model: this.model, idx: this.sceneIdx, profile: this.profile };
    this.pause(); this.stack.push(snapshot);
    this.lesson = lesson;
    this.model = KB.buildModel(lesson, this.profile);
    this.gotoScene(0, true);
    this.emit('detour', true);
  };
  P.resume = function () {
    var s = this.stack.pop(); if (!s) return;
    this.lesson = s.lesson; this.model = s.model;
    this.gotoScene(s.idx, false);
    this.emit('detour', false);
  };
  P.destroy = function () { this.pause(); if (this.ro) this.ro.disconnect(); if (this.wrap.parentNode) this.wrap.parentNode.removeChild(this.wrap); };
})(window);
