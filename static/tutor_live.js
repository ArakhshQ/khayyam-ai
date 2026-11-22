/* Live voice class: one open speech-to-speech conversation with the teacher.
   The teacher talks, and calls tools while talking: "board" (write or draw), "progress" (plan notes) and "end_class".
   This file only manages the voice connection and the tool calls; tutor.js owns the page.

   Timing (draw while speaking): the model writes its words much faster than they are spoken, so the page keeps a "speech clock":
   the moment each response's audio starts, and about how long it lasts. A drawing called after N spoken characters starts a little
   before the speech reaches N (so the marker moves while the word is being said). A drawing called before the teacher has said
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
    this.pendingAsk = 0; this.askWhy = ''; this.lastStop = 0;
    this.usage = { in_text: 0, in_audio: 0, in_image: 0, cin_text: 0, cin_audio: 0, cin_image: 0, out_text: 0, out_audio: 0, responses: 0 }; this.talkMs = 0; this.talkAt = 0; this.startedAt = 0; this.usageT = 0;
    this.cur = null; this.queue = []; this.resps = []; this.spEnd = 0; this.pumpT = 0; this.transcript = []; this.muted = false; this.endT = 0;
  }
  var P = Session.prototype;
  // Test switches, read once when the file loads (the page rewrites its address later and drops the query) and kept for this tab.
  var FLAGS = (function () {
    var f = { debug: false, aec: true, ws: null }, q = '';
    try { q = global.location.search || ''; } catch (e) {}
    try {
      if (/[?&]voicedebug=1/.test(q)) global.sessionStorage.setItem('khy_voicedebug', '1');
      if (/[?&]voicedebug=0/.test(q)) global.sessionStorage.removeItem('khy_voicedebug');
      if (/[?&]voiceaec=0/.test(q)) global.sessionStorage.setItem('khy_voiceaec', '0');
      if (/[?&]voiceaec=1/.test(q)) global.sessionStorage.removeItem('khy_voiceaec');
      if (/[?&]voicews=1/.test(q)) global.sessionStorage.setItem('khy_voicews', '1');      // ?voicews=1: voice over a reliable connection (TCP)
      if (/[?&]voicews=0/.test(q)) global.sessionStorage.setItem('khy_voicews', '0');      // ?voicews=0: the normal WebRTC voice
      if (/[?&]voicews=auto/.test(q)) global.sessionStorage.removeItem('khy_voicews');
      var w = global.sessionStorage.getItem('khy_voicews'); f.ws = w === '1' ? true : w === '0' ? false : null;
      f.debug = global.sessionStorage.getItem('khy_voicedebug') === '1';
      f.aec = global.sessionStorage.getItem('khy_voiceaec') !== '0';
    } catch (e) { f.debug = /[?&]voicedebug=1/.test(q); f.aec = !/[?&]voiceaec=0/.test(q); }
    return f;
  })();

  P.send = function (m) { if (this.dc && this.dc.readyState === 'open') this.dc.send(JSON.stringify(m)); };

  P.start = function () {
    var self = this, o = this.o;
    if (o.onStage) o.onStage('mic');
    // Test switches (not for students): ?voiceaec=0 opens the mic without echo cancellation / noise suppression (use headphones),
    // ?voicedebug=1 shows live audio quality numbers. They help tell a bad connection from a bad phone audio mode.
    var aec = FLAGS.aec;
    if (FLAGS.ws !== false && global.RealtimeWS) global.RealtimeWS.prepare();      // the sound system may only be started inside this tap on some phones
    return navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: aec, noiseSuppression: aec } }).then(function (mic) {
      self.mic = mic;
      if (o.onStage) o.onStage('session');
      // the previous session must be finished on the server first, or it still counts as "another session active"
      var prev = Promise.resolve(global.TutorLive && global.TutorLive.pendingFinish).catch(function () {});
      return prev.then(function () {
        var tries = 0;
        function go() {
          return post('/api/voice/session', { mode: 'class', class_id: o.classId }).then(function (r) {
            if (r.__status === 429 && /another session/i.test(String(r.error || '')) && tries++ < 4) {
              return new Promise(function (res) { setTimeout(res, 1500); }).then(go);
            }
            return r;
          });
        }
        return go();
      });
    }).then(function (s) {
      if (s.__status !== 200 || !s.value || !s.receipt) { var er = new Error('session ' + s.__status + ': ' + (s.detail || s.error || '')); er.stage = 'session'; er.status = s.__status; throw er; }
      self.manual = s.manual_turns === true;           // the page decides what counts as the student speaking (noise and echo do not start a reply)
      self.receipt = s.receipt; self.deadline = Number(s.allowance_deadline || 0) * 1000; self.warnAt = Number(s.warning_seconds || 120);
      self.timer = setInterval(function () { self.tick(); }, 1000);
      var viaWs = global.RealtimeWS && (FLAGS.ws === true || (FLAGS.ws === null && s.transport === 'ws'));
      if (viaWs) return self.connectWs(s);
      if (global.RealtimeWS) global.RealtimeWS.discard();
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
      var opened = new Promise(function (res, rej) { dc.onopen = res; setTimeout(function () { var e2 = new Error('data channel did not open'); e2.stage = 'channel'; rej(e2); }, 20000); });
      if (o.onStage) o.onStage('connect');
      return pc.createOffer().then(function (offer) {
        return pc.setLocalDescription(offer).then(function () {
          return fetch('https://api.openai.com/v1/realtime/calls', { method: 'POST', headers: { 'Authorization': 'Bearer ' + s.value, 'Content-Type': 'application/sdp' }, body: offer.sdp });
        });
      }).then(function (r) {
        if (!r.ok) { var e3 = new Error('webrtc ' + r.status); e3.stage = 'webrtc'; throw e3; }
        return r.text();
      }).then(function (sdp) { return pc.setRemoteDescription({ type: 'answer', sdp: sdp }); }).then(function () { return opened; });
    }).then(function () {
      self.active = true; self.startedAt = Date.now(); o.onState('connected');
      if (FLAGS.debug) self.debugStats();
      self.needReply = false; self.classOn = !!o.resumed;
      if (o.firstText) self.send({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text: '[نوشته شاگرد] ' + o.firstText }] } });
      if (o.firstPicture) self.putPicture(o.firstPicture);
      if (o.resumed) self.send({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text: '[ادامهٔ صنف] The student is back and presses continue. This is the same class as before, not a new one: greet them and carry on from what is in your notes and on the board. Do not ask what they want to learn.' }] } });
      self.ask('start');                               // the teacher speaks first
    });
  };

  // Voice over a WebSocket (TCP): same session, same events, but audio is sent and played by the page itself (see realtime_ws.js)
  P.connectWs = function (s) {
    var self = this, o = this.o;
    if (o.onStage) o.onStage('connect');
    return global.RealtimeWS.open({
      model: s.model, key: s.value, mic: self.mic, autoInterrupt: !self.manual,
      onEvent: function (ev) { self.onEvent(ev); },
      onClose: function () { if (!self.closed && self.active) o.onError('ارتباط صوتی قطع شد.'); }
    }).then(function (conn) { self.ws = conn; self.dc = conn; });
  };

  // Live audio quality readout (only with ?voicedebug=1). "Concealed" = audio the browser had to invent because packets
  // arrived late or not at all: high means network/CPU trouble; near zero while it still sounds bad means the sound itself
  // (the model or the phone's call audio mode) is the cause.
  P.debugStats = function () {
    var self = this, box = document.createElement('div'), last = null;
    box.style.cssText = 'position:fixed;z-index:9999;left:6px;bottom:6px;max-width:92vw;padding:6px 9px;border-radius:8px;background:rgba(0,0,0,.78);color:#9f9;font:11px/1.5 monospace;direction:ltr;white-space:pre-wrap;pointer-events:none';
    document.body.appendChild(box); this.dbgBox = box;
    var cp = document.createElement('button'); cp.textContent = 'copy class log'; cp.style.cssText = 'position:fixed;z-index:9999;left:6px;bottom:84px;padding:4px 9px;border-radius:8px;border:0;background:#222;color:#9f9;font:11px monospace';
    cp.onclick = function () { var t = (self._log || []).join('\n'); try { navigator.clipboard.writeText(t); cp.textContent = 'copied ✓'; } catch (e) { cp.textContent = 'copy failed'; } setTimeout(function () { cp.textContent = 'copy class log'; }, 1800); };
    document.body.appendChild(cp); this.dbgBtn = cp;
    this.dbgT = setInterval(function () {
      if (self.closed) return;
      if (self.ws) {
        var st = self.ws.stats();
        box.textContent = 'TCP websocket (reliable)\nqueued: ' + st.queuedMs + ' ms | underruns: ' + st.underruns + ' | gap max: ' + st.gapMax + ' ms\nmic sent: ' + st.sent + ' | stuck: ' + st.bufferedKB + ' KB | dropped: ' + st.dropped + ' | audio: ' + st.state + ' | aec: ' + (FLAGS.aec ? 'on' : 'off');
        return;
      }
      if (!self.pc) return;
      self.pc.getStats().then(function (rep) {
        var a = null, rtt = null, out = null, pair = null, cands = {};
        rep.forEach(function (r) {
          if (r.type === 'inbound-rtp' && (r.kind === 'audio' || r.mediaType === 'audio')) a = r;
          if (r.type === 'outbound-rtp' && (r.kind === 'audio' || r.mediaType === 'audio')) out = r;
          if (r.type === 'local-candidate' || r.type === 'remote-candidate') cands[r.id] = r;
          if (r.type === 'candidate-pair' && r.state === 'succeeded' && r.nominated) { pair = r; if (r.currentRoundTripTime != null) rtt = r.currentRoundTripTime; }
        });
        var path = '-';
        if (pair) { var lc = cands[pair.localCandidateId] || {}, rc = cands[pair.remoteCandidateId] || {}; path = (lc.candidateType || '?') + '/' + (lc.protocol || '?') + ' > ' + (rc.candidateType || '?') + (rc.address || rc.ip ? ' ' + (rc.address || rc.ip) : ''); }
        var sent = out ? 'mic sent: ' + out.packetsSent + ' pkts' : 'mic: -';
        if (!a) { box.textContent = 'no audio stats yet'; return; }
        var d = last ? { n: a.totalSamplesReceived - last.totalSamplesReceived, c: a.concealedSamples - last.concealedSamples, lost: a.packetsLost - last.packetsLost, rcv: a.packetsReceived - last.packetsReceived } : null;
        last = { totalSamplesReceived: a.totalSamplesReceived, concealedSamples: a.concealedSamples, packetsLost: a.packetsLost, packetsReceived: a.packetsReceived };
        var jb = a.jitterBufferEmittedCount ? Math.round(a.jitterBufferDelay / a.jitterBufferEmittedCount * 1000) : 0;
        var win = d && d.n > 0 ? (d.c / d.n * 100).toFixed(1) + '%' : '-';
        var tot = a.totalSamplesReceived ? (a.concealedSamples / a.totalSamplesReceived * 100).toFixed(1) + '%' : '-';
        box.textContent = 'concealed last 2s: ' + win + ' | total: ' + tot + '\nlost pkts: ' + a.packetsLost + ' (+' + (d ? d.lost : 0) + ') | jitter: ' + Math.round((a.jitter || 0) * 1000) + ' ms | buffer: ' + jb + ' ms | rtt: ' + (rtt == null ? '-' : Math.round(rtt * 1000) + ' ms') + '\npath: ' + path + '\n' + sent + ' | aec: ' + (FLAGS.aec ? 'on' : 'off');
      }).catch(function () {});
    }, 2000);
  };

  // what the realtime model says it used (tokens by kind): we add it up and tell the server, which prices it
  P.noteUsage = function (u) {
    if (!u) return;
    var U = this.usage, d = u.input_token_details || {}, c = d.cached_tokens_details || {}, o = u.output_token_details || {};
    function n(x) { return Number(x) > 0 ? Number(x) : 0; }
    U.in_text += n(d.text_tokens); U.in_audio += n(d.audio_tokens); U.in_image += n(d.image_tokens);
    if (c.text_tokens != null || c.audio_tokens != null || c.image_tokens != null) { U.cin_text += n(c.text_tokens); U.cin_audio += n(c.audio_tokens); U.cin_image += n(c.image_tokens); }
    else U.cin_text += n(d.cached_tokens);                       // no breakdown given: count the cached part as text (cached text and audio are priced alike)
    U.out_text += n(o.text_tokens); U.out_audio += n(o.audio_tokens); U.responses += 1;
    if (!d.text_tokens && !d.audio_tokens && !d.image_tokens && u.input_tokens) U.in_text += n(u.input_tokens);
    if (!o.text_tokens && !o.audio_tokens && u.output_tokens) U.out_audio += n(u.output_tokens);
    var self = this;
    if (!this.usageT) this.usageT = setTimeout(function () { self.usageT = 0; self.sendUsage(false); }, 20000);     // at most one report per 20 s
  };
  P.sendUsage = function (final) {
    if (!this.receipt || !this.usage.responses) return;
    var secs = this.startedAt ? Math.round((Date.now() - this.startedAt) / 1000) : 0;
    var talk = Math.round((this.talkMs + (this.talkAt ? Date.now() - this.talkAt : 0)) / 1000);
    var self = this;
    try {
      fetch('/api/voice/usage', { method: 'POST', credentials: 'same-origin', keepalive: !!final, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ receipt: this.receipt, usage: this.usage, seconds: secs, talk_seconds: talk }) })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          // The month's voice budget may run out before the day's time does: bring the deadline forward (never push it back).
          if (!d || final || self.closed || typeof d.cap_seconds !== 'number') return;
          var at = Date.now() + d.cap_seconds * 1000;
          if (!self.deadline || at < self.deadline) self.deadline = at;
        }).catch(function () {});
    } catch (e) {}
  };

  /* A gentle break offer after about 15 minutes of one session (and again at 30): a note only the teacher sees. The teacher
     finishes the idea it is on, then warmly suggests a rest; the class waits where it is. The student can always say "go on". */
  var BREAKS = (global.TUTOR_BREAK_MS && global.TUTOR_BREAK_MS.length) ? global.TUTOR_BREAK_MS : [15 * 60000, 30 * 60000];
  var BREAK_NOTE = '[SESSION NOTE for the teacher only, not said by the student. Never read it out or mention it.] The student has been in this class for about %MIN% minutes. ' +
    'At the NEXT natural stopping point (finish the idea or step you are on first, never cut one off) praise what they did, give a one-sentence recap, and warmly suggest a short break: say it is a good moment to rest the mind, ' +
    'and that the class will wait exactly here and continue from this very spot. If they agree, say one short warm closing sentence and call end_class. ' +
    'If they want to go on, just continue happily and do not bring the break up again for at least ten minutes.';
  P.breakNote = function (minutes) {
    if (!this.active || this.closed || this.ending) return false;
    this.send({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text: BREAK_NOTE.replace('%MIN%', String(minutes)) }] } });
    return true;
  };
  P.tick = function () {
    if (this.startedAt && this.active && !this.closed) {
      var ran = Date.now() - this.startedAt, k = this.breaksSent || 0;
      if (k < BREAKS.length && ran >= BREAKS[k]) { this.breaksSent = k + 1; this.breakNote(Math.round(BREAKS[k] / 60000)); }
    }
    this.watchdog();
    if (!this.deadline) return;
    var left = Math.max(0, Math.ceil((this.deadline - Date.now()) / 1000));
    if (!this.warned && left <= this.warnAt && left > 0) { this.warned = true; this.o.onQuota('warn', Math.ceil(left / 60)); }
    if (!left && !this.closed) this.o.onQuota('end', 0);
  };

  /* ───────── asking the teacher to answer ─────────
     Every reply goes through ask(). The model refuses a new reply while one is still being made or cancelled, so: never ask
     twice at once (pendingAsk), remember to ask again when the current one ends (needReply), and if a request is lost or
     refused, ask again (watchdog). Before, a refused request was silently dropped and the student's words went unanswered. */
  P.ask = function (why) {
    if (!this.active || this.closed) return;
    if (this.responding || this.pendingAsk) { this.needReply = true; return; }
    this.needReply = false; this.pendingAsk = Date.now(); this.askWhy = why || '';
    this.send({ type: 'response.create' });
  };
  // is the student really talking now? (a noise can hold the voice detector "on" for a long time: do not wait on it for ever)
  P.speakingNow = function () { return this.userSpeaking && Date.now() - this.talkAt < 8000; };
  P.watchdog = function () {
    if (!this.active || this.closed || this.ending) return;
    var now = Date.now();
    if (this.pendingAsk && !this.responding && now - this.pendingAsk > 8000) { this.log('ask-lost', this.askWhy); this.pendingAsk = 0; this.needReply = true; }
    if (this.needReply && !this.responding && !this.pendingAsk && !this.speakingNow() && now - this.lastStop > 900) { this.log('rescue', 'a reply was waiting'); this.ask('rescue'); }
  };

  /* ───────── events from the model ───────── */
  P.onEvent = function (e) {
    var self = this, t = e.type;
    if (t === 'response.created') {
      this.responding = true; this.pendingAsk = 0;
      this.cur = { chars: 0, begin: 0, calls: [], text: '', items: [], started: false, t0: Date.now(), why: this.askWhy }; this.askWhy = ''; this.o.onState('speaking');
    } else if (t === 'output_audio_buffer.started') {
      this.audioStarted();
    } else if (t === 'response.output_audio_transcript.delta') {
      if (this.cur) {
        if (!this.cur.begin) this.beginSpeech(this.cur);
        this.cur.chars += (e.delta || '').length; this.cur.text += e.delta || '';
        this.spEnd = Math.max(this.spEnd, this.cur.begin + (this.cur.chars / CPS) * 1000);
      }
    } else if (t === 'response.output_audio_transcript.done') {
      if (e.transcript && e.transcript.trim()) {
        var said = e.transcript.trim();
        this.transcript.push({ role: 'assistant', text: said });
        // cut off before a single word was played: the student heard nothing, so it does not go in the chat either
        if (this.cur && this.cur.cut && !this.cur.started) { this.log('tutor-unheard', said); }
        else { this.o.onAiText(said); this.log('tutor', said); this.dirTutor(said); }
      }
    } else if (t === 'response.output_item.done') {
      var it = e.item;
      if (it && it.type === 'function_call' && this.cur) this.cur.calls.push({ item: it, atChars: this.cur.chars, resp: this.cur });
    } else if (t === 'response.done') {
      this.responding = false;
      var cur = this.cur; this.cur = null;
      if (cur) this.log('done', ((e.response && e.response.status) || '?') + ' ' + (Date.now() - cur.t0) + 'ms' + (cur.why ? ' for ' + cur.why : '') +
        (cur.calls.length ? ' calls:' + cur.calls.map(function (c) { return c.item.name; }).join(',') : '') + (cur.cut ? ' (cut)' : ''));
      this.noteUsage(e.response && e.response.usage);
      this.afterResponse(cur, e.response);
    } else if (t === 'input_audio_buffer.speech_started') {
      this.userSpeaking = true; this.talkAt = Date.now();
      if (this.manual) { this.sp0 = Date.now(); this.o.onState('listening'); return; }      // maybe only noise: the tutor keeps going until it is clear this is the student
      this.nudged = false; this.flush(); this.o.onState('listening');
    } else if (t === 'input_audio_buffer.speech_stopped') {
      this.userSpeaking = false; this.lastStop = Date.now(); if (this.talkAt) { this.talkMs += Date.now() - this.talkAt; this.talkAt = 0; }
      if (this.manual) {
        var dur = Date.now() - (this.sp0 || Date.now()), me = this;
        this.o.onState(this.responding ? 'speaking' : 'idle');
        clearTimeout(this.tNoTx);
        // the words normally arrive within a second; if they do not (transcription failed), a long sound still counts as the student
        if (dur >= 1100) this.tNoTx = setTimeout(function () { me.realTurn(); }, 3000);
        return;
      }
      this.o.onState('thinking');
    } else if (t === 'conversation.item.input_audio_transcription.completed') {
      var tx = (e.transcript || '').trim();
      if (this.manual) {
        clearTimeout(this.tNoTx);
        if (!this.isStudent(tx)) { this.log('ignored', tx); if (e.item_id) this.send({ type: 'conversation.item.delete', item_id: e.item_id }); this.afterNoise(); return; }
        this.log('student', tx); this.dirStudent(tx);
        this.transcript.push({ role: 'user', text: tx }); this.o.onUserText(tx); this.realTurn(); return;
      }
      if (tx) { this.transcript.push({ role: 'user', text: tx }); this.o.onUserText(tx); }
    } else if (t === 'output_audio_buffer.stopped' || t === 'output_audio_buffer.cleared') {
      this.spEnd = Date.now();                         // nothing is playing now
      if (this.ending && !this.responding) this.finishSoon(900);
      else if (!this.responding) this.o.onState('idle');
    } else if (t === 'error') {
      var m = e.error && e.error.message || '';
      if (global.console) console.warn('realtime error', e.error);
      if (/active response/i.test(m)) { this.pendingAsk = 0; this.needReply = true; this.log('busy', 'a reply was still running: will ask again when it ends'); return; }
      this.log('error', m);
      if (!/cancel|no active|item.*(not found|does not exist)|conversation\.item\.delete/i.test(m)) this.o.onError('خطایی در گفتگوی صوتی رخ داد.');
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
    if (r.t0) this.log('voice', 'heard ' + (Date.now() - r.t0) + 'ms after the reply was asked');
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
      if (this.needReply && !this.speakingNow()) { this.ask('waiting'); return; }
      if (this.needsBoard(cur)) this.boardNudge();
      return;
    }
    if (calls.some(function (c) { return c.item.name === 'board'; })) { this.nudged = false; this.classOn = true; }
    var WORK = { look: 'look', find_poem: 'poem', start_class: 'start', web_search: 'search', make_picture: 'picture', expert: 'expert', make_quiz: 'quiz', make_test: 'quiz' };
    var jobs = calls.map(function (c) {
      var w = WORK[c.item.name], t0 = Date.now(), p = self.runCall(c, cur);
      p.then(function () { self.log('tool', c.item.name + ' ' + (Date.now() - t0) + 'ms'); }, function () { self.log('tool', c.item.name + ' failed'); });
      if (w && self.o.onWork) { self.o.onWork(w, true); var off = function () { self.o.onWork(w, false); }; p.then(off, off); }
      return p;
    });
    Promise.all(jobs).then(function (outs) {
      // the teacher already spoke in this reply: after the tool result it tended to start over (the greeting twice). Say so, once.
      var spoke = cur && cur.chars > 40 && !cur.cut;
      outs.forEach(function (o, i) {
        if (spoke && i === 0 && !o.quiet && o.output && typeof o.output === 'object') o.output.already_said = 'You already said your words aloud. Do not repeat them; continue from there.';
        self.send({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: o.call_id, output: JSON.stringify(o.output) } });
      });
      if (self.ending) return;                         // saying goodbye: nothing more to ask
      if (outs.length && outs.every(function (o) { return o.quiet; })) return;      // a quiz card is on screen: the teacher waits in silence
      if (self.responding || self.speakingNow()) { self.needReply = true; return; }
      self.ask('after ' + calls.map(function (c) { return c.item.name; }).join(','));
    });
  };

  /* ───────── class director ─────────
     Tracks the lesson state (step, turns, questions asked, quiz status) HERE, not in the model's memory.
     Before each student turn the teacher gets one short context note. The note guides but does not command:
     it tells the tutor where they are, what the student said, and whether a quiz is due.
     The previous note is deleted each time so they do not pile up. */
  var DIR_SAME = 0.45, DIR_NUDGE = 10, DIR_FORCE = 16;
  P.dirState = function () { return this._dir || (this._dir = { step: -1, outline: null, turns: 0, qs: [], lastQ: '', asked: 0, stuAfter: 0, lastStu: '', noteId: '', n: 0, moved: '', forcing: false, quizDone: false, quizResult: '' }); };
  P.dirPlan = function (d) {
    var D = this.dirState();
    if (d && Array.isArray(d.outline) && d.outline.length) D.outline = d.outline;
    if (d && typeof d.step === 'number' && d.step !== D.step) { D.step = d.step; D.turns = 0; D.asked = 0; D.stuAfter = 0; D.lastQ = ''; D.quizDone = false; D.quizResult = ''; }
  };
  P.dirQuiz = function (result) { var D = this.dirState(); D.quizDone = true; D.quizResult = result || 'done'; };
  function lastQuestion(text) {
    var m = String(text || '').match(/[^.!؟?\n]*[؟?]/g); if (!m) return '';
    var q = m[m.length - 1].trim(); return words(q).length >= 3 ? q : '';
  }
  function similar(a, b) {
    var x = words(a), y = words(b); if (!x.length || !y.length) return 0;
    var sy = {}, inter = 0, uni = 0; y.forEach(function (w) { sy[w] = 1; });
    var sx = {}; x.forEach(function (w) { sx[w] = 1; if (sy[w]) inter++; });
    uni = Object.keys(sx).length + Object.keys(sy).length - inter; return uni ? inter / uni : 0;
  }
  P.dirTutor = function (text) {
    if (!this.classOn) return;
    var D = this.dirState(), q = lastQuestion(text);
    D.turns++;
    if (q) {
      var again = D.qs.some(function (p) { return similar(p, q) >= DIR_SAME; });
      D.asked = again ? D.asked + 1 : 1; D.lastQ = q; D.stuAfter = 0;
      D.qs.push(q); if (D.qs.length > 8) D.qs.shift();
      if (again) this.log('repeat-question', q);
    }
    this.dirForce();
  };
  P.dirStudent = function (tx) { var D = this.dirState(); D.stuAfter++; D.lastStu = String(tx || '').slice(0, 90); };
  P.dirForce = function () {
    var D = this.dirState(), me = this;
    if (D.forcing || !D.outline || D.turns < DIR_FORCE || D.step < 0 || D.step + 1 >= D.outline.length) return;
    D.forcing = true; this.log('auto-advance', 'step ' + D.step + ' -> ' + (D.step + 1));
    post('/api/class/' + this.o.classId + '/progress', { step: D.step + 1, note: 'ادامه به گام بعدی' }).then(function (d) {
      D.forcing = false;
      if (d && d.ok) { me.o.onProgress(d); me.dirPlan(d); D.moved = d.where || ''; }
    }).catch(function () { D.forcing = false; });
  };
  P.dirNote = function () {
    if (!this.classOn) return '';
    var D = this.dirState();
    if ((!D.outline || D.step < 0) && this.o.getPlan) { var pl = this.o.getPlan(); if (pl) this.dirPlan(pl); }
    if (!D.outline || D.outline.length < 2 || D.step < 0) return '';
    var n = D.outline.length, i = Math.min(D.step, n - 1);
    // base: where we are
    var s = '[وضعیت صنف] Step ' + (i + 1) + ' of ' + n + ': «' + D.outline[i] + '»' + (i + 1 < n ? '. Next: «' + D.outline[i + 1] + '»' : '. This is the last step') + '.';
    // forced advance happened
    if (D.moved) { s += ' The page moved the class to this new step. Start teaching it.'; D.moved = ''; }
    // student just answered: react first
    if (D.lastQ && D.stuAfter > 0) {
      s += ' The student answered «' + D.lastStu + '» to your question «' + D.lastQ + '». REACT TO THEIR ANSWER FIRST: say whether they are right or wrong, explain if wrong with a different example.';
      if (D.asked >= 2) s += ' You already asked this question ' + D.asked + ' times — do not ask it again. Give the answer in one sentence and continue.';
    }
    // repeat detection (even without student answer)
    else if (D.asked >= 2) { s += ' You already asked the same question ' + D.asked + ' times. Stop asking it. Give the answer and explain differently.'; }
    // quiz guidance
    if (!D.quizDone && D.turns >= 3) { s += ' The student has not had a quiz on this step yet. When they show basic understanding, call make_quiz before moving on.'; }
    if (D.quizDone && !D.moved) { s += ' A quiz was completed on this step. If the student understood, call progress with step=' + (i + 1) + ' to move on.'; }
    // gentle nudge after a while (not a command to wrap up)
    if (D.turns >= DIR_NUDGE && !D.quizDone) { s += ' You have been on this step for ' + D.turns + ' turns. Consider giving the quiz now and moving on.'; }
    s += ' Do not read this note aloud.';
    return s;
  };
  P.dirInject = function () {
    var note = this.dirNote(); if (!note) return;
    var D = this.dirState();
    if (D.noteId) this.send({ type: 'conversation.item.delete', item_id: D.noteId });
    D.n++; D.noteId = 'dir' + D.n;
    this.send({ type: 'conversation.item.create', item: { id: D.noteId, type: 'message', role: 'user', content: [{ type: 'input_text', text: note }] } });
    this.log('note', note);
  };

  /* class log: what happened, in order (for finding problems: ?voicedebug=1 shows a copy button) */
  P.log = function (kind, text) {
    var L = this._log || (this._log = []); L.push((Math.round((Date.now() - (this.startedAt || Date.now())) / 1000)) + 's ' + kind + ': ' + String(text || '').replace(/\s+/g, ' ').slice(0, 220));
    if (L.length > 300) L.shift(); global.__khyLog = L;
  };

  /* ───────── who is speaking: the student, or noise / the speaker's own echo ─────────
     The server does not answer or interrupt by itself (manual turns). A sound counts as the student only when its words are real. */
  function words(s) { return String(s || '').toLowerCase().replace(/[.,!?؟،؛:;"'«»()\-–—…]/g, ' ').split(/\s+/).filter(Boolean); }
  var JUNK = /^(thank you|thanks|thanks for watching|bye|you|okay|ok|yeah|uh|um|hmm|mm|ah|oh|\.+)$/i;
  P.isEcho = function (tx) {
    var w = words(tx); if (w.length < 2) return false;
    var recent = {}, n = 0;
    this.transcript.slice(-4).forEach(function (t) { if (t.role === 'assistant') words(t.text).forEach(function (x) { recent[x] = 1; }); });
    if (this.cur && this.cur.text) words(this.cur.text).forEach(function (x) { recent[x] = 1; });
    w.forEach(function (x) { if (recent[x]) n++; });
    return n / w.length >= 0.75;
  };
  P.isStudent = function (tx) {
    var w = words(tx);
    if (!w.length) return false;
    if (this.isEcho(tx)) return false;
    var talking = this.responding || Date.now() < this.spEnd;          // the tutor is speaking right now: be stricter
    if (talking && w.length < 2) return false;
    if (talking && JUNK.test(String(tx).trim().replace(/[.!?]+$/, ''))) return false;
    if (!talking && w.length === 1 && w[0].length < 2) return false;
    return true;
  };
  // the student really spoke: stop the tutor, show what was waiting, and let it answer
  P.realTurn = function () {
    clearTimeout(this.tNoTx);
    if (!this.active || this.ending) return;
    this.nudged = false; this.o.onState('thinking');
    var cur = this.cur;
    if (this.responding && cur && !cur.begin && !cur.started) {
      // the teacher is still preparing an answer and has not said a word (often a board drawing). Cancelling it would throw that
      // work away and start again, so a student who repeats themselves while waiting made it slower each time. Let it finish,
      // then answer what was just said.
      this.flush(); this.dirInject(); this.needReply = true; this.log('wait', 'teacher still preparing: answer after it');
      return;
    }
    this.flush(); this.stopSpeaking();
    this.dirInject();                                  // where the class is, before the teacher answers
    this.ask('student');
  };
  // a noise was ignored: if something was waiting for a quiet moment (a tool result), carry on
  P.afterNoise = function () {
    if (this.userSpeaking || this.responding) return;
    if (this.needReply) this.ask('after noise');
  };

  // The teacher spoke a real stretch in a class without changing the board: ask once for the picture/example that goes with it.
  // (Questions to the student, goodbyes and short replies are left alone.)
  P.needsBoard = function (cur) {
    if (!cur || this.ending || this.nudged || !this.classOn || this.userSpeaking) return false;
    var t = String(cur.text || '').trim();
    if (cur.chars < 90) return false;
    if (/[؟?]\s*$/.test(t)) return false;
    return true;
  };
  P.boardNudge = function () {
    if (!this.active || this.responding) return false;
    this.nudged = true;
    this.send({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text:
      '[یادداشت سیستم] You spoke but the board did not change. Do NOT repeat or re-say anything. Now call board once with a cue sheet: a picture or a small worked example for what you just said (a few words, numbers, a drawing). Then say at most ONE short sentence, and ask a tiny practice question.' }] } });
    this.ask('board nudge');
    return true;
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
    if (it.name === 'web_search') {
      return post('/api/class/' + id + '/search', { query: args.query }).then(function (d) {
        delete d.__status; return { call_id: it.call_id, output: d };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false, error: 'search failed, teach without it' } }; });
    }
    if (it.name === 'make_picture') {
      return post('/api/class/' + id + '/make_picture', { prompt: args.prompt, aspect_ratio: args.aspect_ratio }).then(function (d) {
        delete d.__status; delete d.item; return { call_id: it.call_id, output: d };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false, error: 'could not make the picture, draw it on the board instead' } }; });
    }
    if (it.name === 'expert') {
      return post('/api/class/' + id + '/expert', { question: args.question, need: args.need }).then(function (d) {
        delete d.__status; return { call_id: it.call_id, output: d };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false, error: 'the expert is not available now; solve it step by step yourself, carefully' } }; });
    }
    if (it.name === 'make_quiz' || it.name === 'make_test') {
      if (!self.o.onQuiz) return Promise.resolve({ call_id: it.call_id, output: { ok: false, error: 'ask the questions aloud instead' } });
      return post('/api/class/' + id + (it.name === 'make_test' ? '/test' : '/quiz'), { topic: args.topic, count: args.count }).then(function (d) {
        if (!d.ok || !d.quiz) return { call_id: it.call_id, output: { ok: false, error: d.error || 'could not make the quiz now; ask the questions aloud instead' } };
        self.o.onQuiz(d.quiz, function (res) { self.quizDone(d.quiz, res); });
        return { call_id: it.call_id, quiet: true, output: { ok: true, note: 'The ' + (d.quiz.kind === 'test' ? 'final test' : 'quiz (' + d.quiz.level + ')') + ' is on screen with ' + d.quiz.questions.length + ' questions. Say nothing more now and wait: you will get the result as a message.' } };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false, error: 'could not make the quiz now; ask the questions aloud instead' } }; });
    }
    if (it.name === 'start_class') {
      return post('/api/class/' + id + '/progress', { step: 0, note: 'شروع صنف', outline: args.outline, title: args.title, subject: args.subject }).then(function (d) {
        if (d.ok) { d.started = true; self.classOn = true; self.o.onProgress(d); self.dirPlan(d); self.log('start', d.title || ''); }
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
        if (d.ok) { self.o.onProgress(d); self.dirPlan(d); self.log('progress', 'step ' + d.step); }
        var out = { ok: !!d.ok }; if (d.where) out.where = d.where; if (d.note) out.note = d.note;
        return { call_id: it.call_id, output: out };
      }).catch(function () { return { call_id: it.call_id, output: { ok: false } }; });
    }
    if (it.name === 'end_class') {
      this.ending = true;
      if (args.next_title) this.nextClass = { title: String(args.next_title).slice(0, 120), request: String(args.next_request || args.next_title).slice(0, 400) };
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
    this.putPicture(pic); this.log('picture', pic.title || '');
    this.ask('picture');
    return true;
  };
  // something the student did on the board (marked, wrote, typed): the teacher gets a note and pictures.
  // respond=false: it only becomes part of what the teacher knows (a marked part waits for the student's question)
  P.sendBoardNote = function (note, images, respond, label) {
    if (!this.active) return false;
    var content = [{ type: 'input_text', text: note + (respond ? SHORT_REMINDER : '') }];
    if (respond) { this.nudged = false; this.classOn = true; }
    (images || []).forEach(function (u) { if (u) content.push({ type: 'input_image', image_url: u }); });
    this.send({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: content } });
    this.transcript.push({ role: 'user', text: label || '[تخته]' });
    this.log('board-note', (label || '[تخته]') + (respond ? '' : ' (quiet)'));
    if (respond) { this.stopSpeaking(); this.ask('board note'); }
    return true;
  };
  // the student marked something and then said nothing: the teacher asks which part they mean
  P.nudge = function () {
    if (!this.active || this.responding || this.speakingNow()) return false;
    this.ask('nudge'); return true;
  };
  // the student finished a quiz card: the teacher gets the score and the mistakes
  P.quizDone = function (quiz, res) {
    if (!this.active) return;
    var wrong = [], skipped = 0, answers = (res && res.answers) || [];
    quiz.questions.forEach(function (q, i) {
      var a = answers[i];
      if (a === q.answer) return;
      if (a == null || a < 0) skipped++;
      wrong.push('Q' + (i + 1) + (q.topic ? ' [' + q.topic + ']' : '') + ' «' + q.q + '» ' + (a == null || a < 0 ? 'skipped' : 'student chose «' + q.options[a] + '»') + ', right is «' + q.options[q.answer] + '»');
    });
    var n = quiz.questions.length, ok = n - wrong.length, isTest = quiz.kind === 'test';
    var head = (isTest ? '[نتیجهٔ آزمون پایانی]' : '[نتیجهٔ تمرین ' + (quiz.level || '') + ']') + ' The student finished the ' + (isTest ? 'final test' : quiz.level + ' quiz') + ': ' + ok + ' of ' + n + ' correct' + (skipped ? ', ' + skipped + ' skipped' : '') + '.';
    var next = isTest
      ? ' React warmly in ONE short sentence. Then take the weakest topic(s) from the mistakes and re-teach ONE of them on the board with a fresh example (clear first if needed), then offer to continue with the next class or to practice the weak part. Do not read the whole list.'
      : ' React in ONE short warm sentence. If there were mistakes, show the main one on the board with a small example (clear first if needed), then give one new tiny practice step. If all correct, praise briefly and move to the next idea with a new example on the board.';
    var note = head + (wrong.length ? ' Mistakes: ' + wrong.join('; ') + '.' : ' All correct.') + next;
    this.dirQuiz(ok + '/' + n + (wrong.length ? ' weak' : ' perfect'));
    this.log('quiz-result', ok + '/' + n);
    this.nudged = false;
    this.sendBoardNote(note, [], true, '[' + (isTest ? 'آزمون' : 'تمرین') + '] ' + ok + '/' + n);
  };
  var SHORT_REMINDER = '\n(Answer in ONE or TWO short sentences and show it ON THE BOARD with a picture or example; then a tiny practice question.)';
  // the student asks the teacher to explain one quiz question (the card stays open)
  P.explainQuestion = function (q, n) {
    if (!this.active) return false;
    this.nudged = false;
    this.sendBoardNote('[پرسش شاگرد از تمرین] The student is stuck on quiz question ' + n + ': «' + q.q + '» with options ' + q.options.map(function (o, i) { return (i + 1) + ') ' + o; }).join(' ') +
      '. Do NOT tell the answer. Explain the idea behind it on the board with a tiny example (one or two short sentences of speech), then say they can try again.', [], true, '[توضیح] پرسش ' + n);
    return true;
  };
  // which quiz question the student is looking at (quiet: it only becomes part of what the teacher knows)
  P.quizAt = function (q, n, total) {
    if (!this.active) return false;
    return this.sendBoardNote('[تمرین] The student is now on quiz question ' + n + ' of ' + total + ': «' + q.q + '» options ' + q.options.map(function (o, i) { return (i + 1) + ') ' + o; }).join(' ') +
      ' (right answer: ' + (q.options[q.answer] || '?') + ' — NEVER say it. Stay quiet. If the student asks about this question, explain the idea on the board without giving the answer.)', [], false, '[تمرین] پرسش ' + n);
  };
  P.sendText = function (text) {
    if (!this.active) return false;
    this.send({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text: '[نوشته شاگرد] ' + text + SHORT_REMINDER }] } });
    this.nudged = false; this.classOn = this.classOn || !!this.o.resumed;
    this.transcript.push({ role: 'user', text: text }); this.log('typed', text);
    this.stopSpeaking();
    this.ask('typed');
    return true;
  };
  P.stopSpeaking = function () {
    var playing = Date.now() < this.spEnd, last = this.resps[this.resps.length - 1];
    if (this.cur) this.cur.cut = true;
    if (playing && last) last.cut = true;
    if (this.responding || playing) this.log('cut', this.responding ? 'reply stopped' : 'sound stopped');
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
    if (this.usageT) { clearTimeout(this.usageT); this.usageT = 0; }
    if (!this.reported) { this.reported = true; this.sendUsage(true); }
    clearInterval(this.timer); clearTimeout(this.endT); clearTimeout(this.pumpT);
    if (this.dbgT) { clearInterval(this.dbgT); this.dbgT = 0; } if (this.dbgBox) { try { this.dbgBox.remove(); } catch (e) {} this.dbgBox = null; } if (this.dbgBtn) { try { this.dbgBtn.remove(); } catch (e) {} this.dbgBtn = null; }
    try { if (this.ws) this.ws.close(); } catch (e) {}
    try { if (this.dc) this.dc.close(); } catch (e) {}
    try { if (this.pc) this.pc.close(); } catch (e) {}
    if (this.mic) this.mic.getTracks().forEach(function (t) { t.stop(); });
    if (this.audio) { try { this.audio.srcObject = null; this.audio.remove(); } catch (e) {} }
    this.active = false;
    var receipt = this.receipt;
    if (receipt) {
      try { global.TutorLive.pendingFinish = fetch('/api/voice/finish', { method: 'POST', credentials: 'same-origin', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ receipt: receipt }) }).catch(function () {}); } catch (e) {}
    }
    return this.transcript.slice(-40);
  };

  global.TutorLive = { Session: Session };
})(window);
