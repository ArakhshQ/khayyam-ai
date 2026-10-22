/* Live voice class: one open speech-to-speech conversation with the teacher.
   The teacher talks, and calls tools while talking: "board" (write or draw), "progress" (plan notes) and "end_class".
   This file only manages the voice connection and the tool calls; tutor.js owns the page.

   Timing: the model writes its words faster than they are spoken, so a drawing that is called after N spoken characters is shown
   when the speech has reached about N characters, not at once. If the student interrupts, everything waiting is shown immediately,
   so the board always matches what the server has saved. */
(function (global) {
  'use strict';
  var CPS = 13;                       // spoken characters per second (Dari), used only to line drawings up with speech

  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { d.__status = r.status; return d; }); });
  }

  function Session(opts) {
    this.o = opts;                    // classId, resumed, onBeat(from, beat), onProgress(d), onUserText(t), onAiText(t), onState(s), onEnd(), onQuota(kind, minutes), onError(msg)
    this.pc = null; this.dc = null; this.mic = null; this.audio = null;
    this.receipt = null; this.deadline = 0; this.warned = false; this.timer = 0;
    this.active = false; this.responding = false; this.userSpeaking = false; this.needReply = false; this.ending = false; this.closed = false;
    this.cur = null; this.queue = []; this.pumpT = 0; this.transcript = []; this.muted = false; this.endT = 0;
  }
  var P = Session.prototype;

  P.send = function (m) { if (this.dc && this.dc.readyState === 'open') this.dc.send(JSON.stringify(m)); };

  P.start = function () {
    var self = this, o = this.o;
    return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }).then(function (mic) {
      self.mic = mic;
      return post('/api/voice/session', { mode: 'class', class_id: o.classId });
    }).then(function (s) {
      if (s.__status !== 200 || !s.value || !s.receipt) throw new Error(s.error || ('session ' + s.__status));
      self.receipt = s.receipt; self.deadline = Number(s.allowance_deadline || 0) * 1000; self.warnAt = Number(s.warning_seconds || 120);
      self.timer = setInterval(function () { self.tick(); }, 1000);
      var pc = self.pc = new RTCPeerConnection();
      self.audio = document.createElement('audio'); self.audio.autoplay = true; document.body.appendChild(self.audio);
      pc.ontrack = function (e) { self.audio.srcObject = e.streams[0]; self.audio.play().catch(function () {}); };
      pc.onconnectionstatechange = function () { if (pc.connectionState === 'failed' && !self.closed) o.onError('ارتباط صوتی قطع شد.'); };
      self.mic.getTracks().forEach(function (t) { pc.addTrack(t, self.mic); });
      var dc = self.dc = pc.createDataChannel('oai-events');
      dc.onmessage = function (e) { try { self.onEvent(JSON.parse(e.data)); } catch (err) { if (global.console) console.warn('realtime event', err); } };
      var opened = new Promise(function (res) { dc.onopen = res; });
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
      this.responding = true; this.cur = { chars: 0, start: 0, calls: [], text: '' }; this.o.onState('speaking');
    } else if (t === 'output_audio_buffer.started') {
      if (this.cur && !this.cur.start) this.cur.start = Date.now();
    } else if (t === 'response.output_audio_transcript.delta') {
      if (this.cur) { this.cur.chars += (e.delta || '').length; this.cur.text += e.delta || ''; if (!this.cur.start) this.cur.start = Date.now(); }
    } else if (t === 'response.output_audio_transcript.done') {
      if (e.transcript && e.transcript.trim()) { this.transcript.push({ role: 'assistant', text: e.transcript.trim() }); this.o.onAiText(e.transcript.trim()); }
    } else if (t === 'response.output_item.done') {
      var it = e.item;
      if (it && it.type === 'function_call' && this.cur) this.cur.calls.push({ item: it, atChars: this.cur.chars, at: this.cur.start });
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
      if (this.ending && !this.responding) this.finishSoon(900);
      else if (!this.responding) this.o.onState('idle');
    } else if (t === 'error') {
      var m = e.error && e.error.message || '';
      if (global.console) console.warn('realtime error', e.error);
      if (!/active response|cancel|no active/i.test(m)) this.o.onError('خطایی در گفتگوی صوتی رخ داد.');
    }
  };

  // run the tool calls of a finished response, give the results back, and let the teacher continue
  P.afterResponse = function (cur, resp) {
    var self = this, calls = cur ? cur.calls : [];
    if (!calls.length) {
      if (this.needReply && !this.userSpeaking) { this.needReply = false; this.send({ type: 'response.create' }); }
      return;
    }
    var jobs = calls.map(function (c) { return self.runCall(c, cur); });
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
    if (it.name === 'board') {
      return post('/api/class/' + id + '/draw', { ops: args.ops, clear: args.clear === true }).then(function (d) {
        if (!d.ok) return { call_id: it.call_id, output: { ok: false, error: d.error || 'could not draw' } };
        var when = c.at ? c.at + (c.atChars / CPS) * 1000 : Date.now();
        self.queue.push({ from: d.from, beat: d.beat, at: when });
        self.pump();
        var out = { ok: true };
        var pv = null;
        try { pv = self.o.preview ? self.o.preview(d.from, d.beat) : null; } catch (err) { pv = null; }
        if (pv) {
          out.board = pv.board; out.text_lines_left = pv.text_lines_left; out.picture_slots_free = pv.picture_slots_free;
          if (pv.wiped) out.wiped = 'The whole board was wiped before this was drawn (it was full). Everything drawn earlier is gone; only the items in "board" remain.';
          if (pv.wiped_pictures) out.wiped_pictures = 'The picture area was full and was wiped before this was drawn. The text is still there.';
          if (pv.dropped && pv.dropped.length) out.not_drawn = pv.dropped;
          if (pv.text_lines_left <= 1 || (pv.picture_slots_free && !pv.picture_slots_free.length)) out.advice = 'The board is nearly full. Before you write or draw anything more, call board with clear:true (with the next thing in the same call).';
        } else out.on_board = d.on_board;
        return { call_id: it.call_id, output: out };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false, error: 'connection problem, try once more' } }; });
    }
    if (it.name === 'progress') {
      return post('/api/class/' + id + '/progress', { step: args.step, note: args.note, outline: args.outline, title: args.title, subject: args.subject }).then(function (d) {
        if (d.ok) self.o.onProgress(d);
        return { call_id: it.call_id, output: { ok: !!d.ok } };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false } }; });
    }
    if (it.name === 'end_class') {
      this.ending = true;
      var wait = c.at ? Math.max(0, c.at + (c.atChars / CPS) * 1000 - Date.now()) : 0;
      this.finishSoon(wait + 5000);                    // normally ended earlier by the end of the speech
      return Promise.resolve({ call_id: it.call_id, output: { ok: true } });
    }
    return Promise.resolve({ call_id: it.call_id, output: { ok: false, error: 'unknown tool' } });
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
    clearTimeout(this.pumpT);
    this.queue.sort(function (a, b) { return a.from - b.from; });
    while (this.queue.length) { var q = this.queue.shift(); this.o.onBeat(q.from, q.beat); }
  };

  /* ───────── the student ───────── */
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
