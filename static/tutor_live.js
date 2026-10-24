/* Live voice class: one open speech-to-speech conversation with the teacher.
   The teacher talks, and calls tools while talking: "board" (write or draw), "progress" (plan notes) and "end_class".
   This file only manages the voice connection and the tool calls; tutor.js owns the page.

   Timing (draw while speaking): the model writes its words much faster than they are spoken, so the page keeps a "speech clock":
   the moment each response's audio starts, and about how long it lasts. A drawing called after N spoken characters starts a little
   before the speech reaches N (so the chalk moves while the word is being said). A drawing called before the teacher has said
   anything is held until the next words start, then drawn along with them. If the student interrupts, everything waiting is shown
   immediately, so the board always matches what the server has saved. */
(function (global) {
  'use strict';
  var CPS = 13;                       // spoken characters per second (Dari), used only to line drawings up with speech
  var LEAD = 16;                      // a drawing starts about this many characters (a little over a second) before the words that explain it end
  var CUE_LEAD = 5;                   // a cue tied to its words starts a few characters (a moment) before them: the pointer needs time to fly
  var HOLD_MAX = 4000;                // a drawing called before any speech waits at most this long (ms) for the next words

  function KB_clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { d.__status = r.status; return d; }); });
  }

  function Session(opts) {
    this.o = opts;                    // classId, resumed, onBeat(from, beat), onProgress(d), onUserText(t), onAiText(t), onState(s), onEnd(), onQuota(kind, minutes), onError(msg)
    this.pc = null; this.dc = null; this.mic = null; this.audio = null;
    this.receipt = null; this.deadline = 0; this.warned = false; this.timer = 0;
    this.active = false; this.responding = false; this.userSpeaking = false; this.needReply = false; this.ending = false; this.closed = false;
    this.cur = null; this.queue = []; this.resps = []; this.spEnd = 0; this.pumpT = 0; this.transcript = []; this.muted = false; this.endT = 0;
  }
  var P = Session.prototype;

  P.send = function (m) { if (this.dc && this.dc.readyState === 'open') this.dc.send(JSON.stringify(m)); };

  P.start = function () {
    var self = this, o = this.o;
    if (o.onStage) o.onStage('mic');
    return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }).then(function (mic) {
      self.mic = mic;
      if (o.onStage) o.onStage('session');
      return post('/api/voice/session', { mode: 'class', class_id: o.classId });
    }).then(function (s) {
      if (s.__status !== 200 || !s.value || !s.receipt) throw new Error(s.error || ('session ' + s.__status));
      self.receipt = s.receipt; self.deadline = Number(s.allowance_deadline || 0) * 1000; self.warnAt = Number(s.warning_seconds || 120);
      self.timer = setInterval(function () { self.tick(); }, 1000);
      var pc = self.pc = new RTCPeerConnection();
      self.audio = document.createElement('audio'); self.audio.autoplay = true; document.body.appendChild(self.audio);
      pc.ontrack = function (e) { self.audio.srcObject = e.streams[0]; self.audio.play().catch(function () {}); };
      pc.onconnectionstatechange = function () {
        if (self.closed) return;
        if (pc.connectionState === 'failed') o.onError('ارتباط صوتی قطع شد.');
        else if (o.onNet && self.active) { if (pc.connectionState === 'disconnected') o.onNet(false); else if (pc.connectionState === 'connected') o.onNet(true); }
      };
      self.mic.getTracks().forEach(function (t) { pc.addTrack(t, self.mic); });
      var dc = self.dc = pc.createDataChannel('oai-events');
      dc.onmessage = function (e) { try { self.onEvent(JSON.parse(e.data)); } catch (err) { if (global.console) console.warn('realtime event', err); } };
      var opened = new Promise(function (res) { dc.onopen = res; });
      if (o.onStage) o.onStage('connect');
      return pc.createOffer().then(function (offer) {
        return pc.setLocalDescription(offer).then(function () {
          return fetch('https://api.openai.com/v1/realtime/calls', { method: 'POST', headers: { 'Authorization': 'Bearer ' + s.value, 'Content-Type': 'application/sdp' }, body: offer.sdp });
        });
      }).then(function (r) {
        if (!r.ok) throw new Error('webrtc ' + r.status);
        return r.text();
      }).then(function (sdp) { return pc.setRemoteDescription({ type: 'answer', sdp: sdp }); }).then(function () { return opened; });
    }).then(function () {
      self.active = true; o.onState('connected');
      self.needReply = false;
      if (o.firstText) self.send({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text: '[نوشته شاگرد] ' + o.firstText }] } });
      if (o.firstPicture) self.putPicture(o.firstPicture);
      self.send({ type: 'response.create' });          // the teacher speaks first
    });
  };

  P.tick = function () {
    if (!this.deadline) return;
    var left = Math.max(0, Math.ceil((this.deadline - Date.now()) / 1000));
    if (!this.warned && left <= this.warnAt && left > 0) { this.warned = true; this.o.onQuota('warn', Math.ceil(left / 60)); }
    if (!left && !this.closed) this.o.onQuota('end', 0);
  };

  /* ───────── events from the model ───────── */
  P.onEvent = function (e) {
    var self = this, t = e.type;
    if (t === 'response.created') {
      this.responding = true; this.cur = { chars: 0, begin: 0, calls: [], text: '', items: [], started: false }; this.o.onState('speaking');
    } else if (t === 'output_audio_buffer.started') {
      this.audioStarted();
    } else if (t === 'response.output_audio_transcript.delta') {
      if (this.cur) {
        if (!this.cur.begin) this.beginSpeech(this.cur);
        this.cur.chars += (e.delta || '').length; this.cur.text += e.delta || '';
        this.spEnd = Math.max(this.spEnd, this.cur.begin + (this.cur.chars / CPS) * 1000);
      }
    } else if (t === 'response.output_audio_transcript.done') {
      if (e.transcript && e.transcript.trim()) { this.transcript.push({ role: 'assistant', text: e.transcript.trim() }); this.o.onAiText(e.transcript.trim()); }
    } else if (t === 'response.output_item.done') {
      var it = e.item;
      if (it && it.type === 'function_call' && this.cur) this.cur.calls.push({ item: it, atChars: this.cur.chars, resp: this.cur });
    } else if (t === 'response.done') {
      this.responding = false;
      var cur = this.cur; this.cur = null;
      this.afterResponse(cur, e.response);
    } else if (t === 'input_audio_buffer.speech_started') {
      this.userSpeaking = true; this.flush(); this.o.onState('listening');
    } else if (t === 'input_audio_buffer.speech_stopped') {
      this.userSpeaking = false; this.o.onState('thinking');
    } else if (t === 'conversation.item.input_audio_transcription.completed') {
      var tx = (e.transcript || '').trim();
      if (tx) { this.transcript.push({ role: 'user', text: tx }); this.o.onUserText(tx); }
    } else if (t === 'output_audio_buffer.stopped' || t === 'output_audio_buffer.cleared') {
      this.spEnd = Date.now();                         // nothing is playing now
      if (this.ending && !this.responding) this.finishSoon(900);
      else if (!this.responding) this.o.onState('idle');
    } else if (t === 'error') {
      var m = e.error && e.error.message || '';
      if (global.console) console.warn('realtime error', e.error);
      if (!/active response|cancel|no active/i.test(m)) this.o.onError('خطایی در گفتگوی صوتی رخ داد.');
    }
  };

  /* ───────── speech clock ───────── */
  // the first words of a response: they are heard after whatever is still playing
  P.beginSpeech = function (r) {
    r.begin = Math.max(Date.now(), this.spEnd);
    this.resps.push(r); if (this.resps.length > 8) this.resps.shift();
    var held = this.queue.filter(function (q) { return q.hold; });          // drawings that were waiting for words: draw along with these
    held.forEach(function (q, i) { q.hold = false; q.at = r.begin + i * 1200; r.items.push(q); });
    if (held.length) this.pump();
  };
  // real start of the audio of the oldest response not yet heard: correct our estimate (and the drawings tied to it)
  P.audioStarted = function () {
    var r = null, i;
    for (i = 0; i < this.resps.length; i++) if (!this.resps[i].started) { r = this.resps[i]; break; }
    if (!r) return;
    r.started = true;
    var d = Date.now() - r.begin;
    if (Math.abs(d) < 120) return;
    r.begin += d; r.items.forEach(function (q) { q.at += d; });
    for (i = this.resps.indexOf(r); i < this.resps.length; i++) {
      var x = this.resps[i];
      if (x !== r) { x.begin += d; x.items.forEach(function (q) { q.at += d; }); }
    }
    this.spEnd += d;
    this.pump();
  };

  // run the tool calls of a finished response, give the results back, and let the teacher continue
  P.afterResponse = function (cur, resp) {
    var self = this, calls = cur ? cur.calls : [];
    if (!calls.length) {
      if (this.needReply && !this.userSpeaking) { this.needReply = false; this.send({ type: 'response.create' }); }
      return;
    }
    var WORK = { look: 'look', find_poem: 'poem', start_class: 'start' };
    var jobs = calls.map(function (c) {
      var w = WORK[c.item.name], p = self.runCall(c, cur);
      if (w && self.o.onWork) { self.o.onWork(w, true); var off = function () { self.o.onWork(w, false); }; p.then(off, off); }
      return p;
    });
    Promise.all(jobs).then(function (outs) {
      outs.forEach(function (o) {
        self.send({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: o.call_id, output: JSON.stringify(o.output) } });
      });
      if (self.ending) return;                         // saying goodbye: nothing more to ask
      if (self.responding) { self.needReply = true; return; }
      if (!self.userSpeaking) { self.needReply = false; self.send({ type: 'response.create' }); } else self.needReply = true;
    });
  };

  P.runCall = function (c, cur) {
    var self = this, it = c.item, args = {};
    try { args = JSON.parse(it.arguments || '{}'); } catch (err) { return Promise.resolve({ call_id: it.call_id, output: { ok: false, error: 'invalid JSON arguments' } }); }
    var id = this.o.classId;
    if (it.name === 'board') return this.drawCues(c, args);
    if (it.name === 'look') {
      return post('/api/class/' + id + '/look', { image: args.image, find: args.find }).then(function (d) {
        delete d.__status; return { call_id: it.call_id, output: d };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false, error: 'could not look now, try once more' } }; });
    }
    if (it.name === 'start_class') {
      return post('/api/class/' + id + '/progress', { step: 0, note: 'شروع صنف', outline: args.outline, title: args.title, subject: args.subject }).then(function (d) {
        if (d.ok) { d.started = true; self.o.onProgress(d); }
        return { call_id: it.call_id, output: { ok: !!d.ok, note: d.ok ? 'class started: now write the title on the board and do the pre-check' : 'give a title and an outline of 4-7 steps' } };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false } }; });
    }
    if (it.name === 'find_poem') {
      return post('/api/class/' + id + '/poem', { query: args.query, poet: args.poet, whole: args.whole === true }).then(function (d) {
        delete d.__status; return { call_id: it.call_id, output: d };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false, error: 'search failed, try again' } }; });
    }
    if (it.name === 'progress') {
      return post('/api/class/' + id + '/progress', { step: args.step, note: args.note, outline: args.outline, title: args.title, subject: args.subject }).then(function (d) {
        if (d.ok) self.o.onProgress(d);
        return { call_id: it.call_id, output: { ok: !!d.ok } };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false } }; });
    }
    if (it.name === 'end_class') {
      this.ending = true;
      var wait = Math.max(0, this.spEnd - Date.now());
      this.finishSoon(wait + 5000);                    // normally ended earlier by the end of the speech
      return Promise.resolve({ call_id: it.call_id, output: { ok: true } });
    }
    return Promise.resolve({ call_id: it.call_id, output: { ok: false, error: 'unknown tool' } });
  };

  /* ───────── the cue sheet ─────────
     The teacher says a few sentences, then calls "board" once with a list of cues. A cue may carry "when": the words it was said with.
     Each cue is drawn when the speech reaches those words (found in the transcript of this very response), so the board follows the
     voice like a teacher's hand, with no beat per drawing. A cue without "when" goes where the call was made. */
  function norm(s) { return String(s || '').replace(/[\s‌‍‎‏ً-ٰٟ«»"'.,!?؟،؛:;()\-–—…]/g, ''); }
  function groupCues(ops, maxGroups) {
    var groups = [];
    (Array.isArray(ops) ? ops : []).forEach(function (op) {
      if (!op || typeof op !== 'object') return;
      var w = typeof op.when === 'string' ? op.when.trim() : '', o = Object.assign({}, op); delete o.when;
      if (w || !groups.length) groups.push({ when: w, ops: [o] }); else groups[groups.length - 1].ops.push(o);
    });
    return groups.slice(0, maxGroups || 8);
  }
  // where in this response's speech (in characters) a cue belongs
  function cueChars(r, when, from, k, n, calledAt) {
    var text = norm(r.text), w = norm(when);
    if (w && text) {
      var i = text.indexOf(w, from.i);
      if (i < 0) i = text.indexOf(w);
      if (i < 0 && w.length > 8) { w = w.slice(0, Math.ceil(w.length / 2)); i = text.indexOf(w, from.i); if (i < 0) i = text.indexOf(w); }
      if (i >= 0) { from.i = i; return { chars: Math.round(i * r.chars / Math.max(1, text.length)), lead: CUE_LEAD }; }
    }
    if (when) return { chars: Math.round(r.chars * k / Math.max(1, n)), lead: CUE_LEAD };          // words not found: spread evenly
    return { chars: calledAt, lead: LEAD };                                                          // no words given: where the call was made
  }

  P.drawCues = function (c, args) {
    var self = this, it = c.item, id = this.o.classId, r = c.resp;
    var groups = groupCues(args.ops, 8);
    if (!groups.length) groups = [{ when: '', ops: [] }];
    var from = { i: 0 }, errors = [], dropped = [], pv = null, kept = false, wiped = false, wipedP = false, queued = 0, held = 0;
    var spoke = !!(r && r.begin);
    var chain = Promise.resolve();
    groups.forEach(function (g, k) {
      chain = chain.then(function () {
        return post('/api/class/' + id + '/draw', { ops: g.ops, clear: k === 0 && args.clear === true }).then(function (d) {
          if (!d.ok) { errors.push(d.error || 'could not draw'); return; }
          if (d.from == null) { kept = true; return; }
          var q = { from: d.from, beat: d.beat, at: Date.now(), hold: false };
          if (spoke) {
            var pos = cueChars(r, g.when, from, k, groups.length, c.atChars);
            q.at = Math.max(Date.now(), r.begin + (Math.max(0, pos.chars - pos.lead) / CPS) * 1000); r.items.push(q);
          } else { q.hold = true; q.at = Date.now() + KB_clamp(self.spEnd - Date.now(), 1500, HOLD_MAX) + k * 900; held++; }   // nothing said yet: wait for the words this goes with
          self.queue.push(q); queued++;
          var p1 = null;
          try { p1 = self.o.preview ? self.o.preview(d.from, d.beat) : null; } catch (err) { p1 = null; }
          if (p1) {
            pv = p1; wiped = wiped || p1.wiped; wipedP = wipedP || p1.wiped_pictures;
            (p1.dropped || []).forEach(function (x) { dropped.push(x); });
          }
          self.pump();
        });
      }).catch(function () { errors.push('connection problem, try once more'); });
    });
    return chain.then(function () {
      var out = { ok: queued > 0 || kept };
      if (!out.ok) { out.error = errors[0] || 'nothing was drawn'; return { call_id: it.call_id, output: out }; }
      if (kept) out.page_kept = 'The page is already on the board and stays there. Do not show it again; just point at it.';
      if (pv) {
        out.board = pv.board; out.text_lines_left = pv.text_lines_left; out.picture_slots_free = pv.picture_slots_free;
        if (pv.page && pv.page.length) out.page_on_board = pv.page;
        if (wiped) out.wiped = 'The whole board was wiped before this was drawn (it was full). Everything drawn earlier is gone; only the items in "board" remain.';
        if (wipedP) out.wiped_pictures = 'The picture area was full and was wiped before this was drawn. The text is still there.';
        if (pv.text_lines_left <= 1 && !(pv.page && pv.page.length)) out.advice = 'The board is nearly full. Before you write more, call board with clear:true (with the next thing in the same call).';
        else if (pv.text_lines_left <= 1) out.advice = 'The notes area is nearly full. Before you write more notes, call board with clear:true (the page stays).';
      }
      if (dropped.length) out.not_drawn = dropped;
      if (errors.length) out.not_drawn = (out.not_drawn || []).concat(errors);
      if (!spoke) out.hint = 'You drew without speaking first. Say what you are showing, in words, and give each cue a "when" with the words it goes with.';
      return { call_id: it.call_id, output: out };
    });
  };

  P.finishSoon = function (ms) {
    var self = this; clearTimeout(this.endT);
    this.endT = setTimeout(function () { if (!self.closed) self.o.onEnd(); }, ms);
  };

  // show drawings in order, each when the speech has got to it
  P.pump = function () {
    var self = this; clearTimeout(this.pumpT);
    this.queue.sort(function (a, b) { return a.from - b.from; });
    while (this.queue.length && this.queue[0].at <= Date.now()) { var q = this.queue.shift(); this.o.onBeat(q.from, q.beat); }
    if (this.queue.length) this.pumpT = setTimeout(function () { self.pump(); }, Math.max(40, Math.min(1000, this.queue[0].at - Date.now())));
  };
  P.flush = function () {
    clearTimeout(this.pumpT); this.spEnd = Date.now();
    this.queue.sort(function (a, b) { return a.from - b.from; });
    while (this.queue.length) { var q = this.queue.shift(); this.o.onBeat(q.from, q.beat); }
  };

  /* ───────── the student ───────── */
  // put a picture on the table: the teacher gets the image itself and a note with its key and its named parts
  P.putPicture = function (pic) {
    var content = [{ type: 'input_text', text: pic.note }];
    if (pic.dataUrl) content.push({ type: 'input_image', image_url: pic.dataUrl });
    this.send({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: content } });
    this.transcript.push({ role: 'user', text: '[تصویر] ' + (pic.title || '') });
  };
  P.sendPicture = function (pic) {
    if (!this.active) return false;
    this.stopSpeaking();
    this.putPicture(pic);
    if (this.responding) this.needReply = true; else this.send({ type: 'response.create' });
    return true;
  };
  P.sendText = function (text) {
    if (!this.active) return false;
    this.send({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text: '[نوشته شاگرد] ' + text }] } });
    this.transcript.push({ role: 'user', text: text });
    this.stopSpeaking();
    if (this.responding) this.needReply = true; else this.send({ type: 'response.create' });
    return true;
  };
  P.stopSpeaking = function () {
    if (this.responding) this.send({ type: 'response.cancel' });
    this.send({ type: 'output_audio_buffer.clear' });
    this.flush();
  };
  P.interrupt = function () { this.stopSpeaking(); this.setMuted(false); };
  P.setMuted = function (m) {
    this.muted = !!m;
    if (this.mic) this.mic.getAudioTracks().forEach(function (t) { t.enabled = !m; });
  };

  /* ───────── closing ───────── */
  P.stop = function () {
    this.closed = true; this.flush();
    clearInterval(this.timer); clearTimeout(this.endT); clearTimeout(this.pumpT);
    try { if (this.dc) this.dc.close(); } catch (e) {}
    try { if (this.pc) this.pc.close(); } catch (e) {}
    if (this.mic) this.mic.getTracks().forEach(function (t) { t.stop(); });
    if (this.audio) { try { this.audio.srcObject = null; this.audio.remove(); } catch (e) {} }
    this.active = false;
    var receipt = this.receipt;
    if (receipt) {
      try { fetch('/api/voice/finish', { method: 'POST', credentials: 'same-origin', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ receipt: receipt }) }); } catch (e) {}
    }
    return this.transcript.slice(-40);
  };

  global.TutorLive = { Session: Session };
})(window);
