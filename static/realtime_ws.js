/* Realtime voice over a WebSocket (TCP) instead of WebRTC (UDP).
 *
 * Why: on some networks UDP packets are lost or arrive late, so the voice breaks up and the model mishears. A WebSocket is
 * reliable: lost data is sent again, so what is said and heard arrives intact (at worst a little later).
 *
 * The browser has to do what WebRTC did for us: capture the mic as 24 kHz PCM16 and send it in small pieces, play the
 * model's PCM16 audio as it arrives, and, when the student interrupts, stop the sound and tell the model how much of its
 * answer was really heard (conversation.item.truncate) so it does not think it said things the student never heard.
 *
 * The object returned by open() behaves like the WebRTC data channel the pages already use: send(), readyState, close().
 * It also produces the output_audio_buffer.started / .stopped / .cleared events that WebRTC sent by itself.
 */
(function (global) {
  'use strict';
  var RATE = 24000, CHUNK = 1200;           // 1200 samples = 50 ms

  // mic samples (any rate) -> 24 kHz PCM16 in 50 ms pieces. Written as one plain function so the audio worklet can use the same code.
  function makeResampler(inRate) {
    var step = inRate / 24000, pos = 0, prev = 0, out = new Int16Array(1200), n = 0;
    return function (ch) {
      var res = [], len = ch.length;
      while (pos < len - 1) {
        var i = Math.floor(pos), f = pos - i;
        var a = i < 0 ? prev : ch[i], b = ch[i + 1];
        var v = a + (b - a) * f;
        v = v < -1 ? -1 : v > 1 ? 1 : v;
        out[n++] = v < 0 ? v * 32768 : v * 32767;
        if (n === 1200) { res.push(out); out = new Int16Array(1200); n = 0; }
        pos += step;
      }
      pos -= len; prev = ch[len - 1];
      return res;
    };
  }
  function toB64(i16) {
    var u = new Uint8Array(i16.buffer, i16.byteOffset, i16.byteLength), s = '', k;
    for (k = 0; k < u.length; k += 0x8000) s += String.fromCharCode.apply(null, u.subarray(k, k + 0x8000));
    return btoa(s);
  }
  function fromB64(b) {
    var s = atob(b), n = s.length & ~1, u = new Uint8Array(n), k;
    for (k = 0; k < n; k++) u[k] = s.charCodeAt(k);
    return new Int16Array(u.buffer, 0, n >> 1);
  }

  var WORKLET = '(function(){var mk=' + makeResampler.toString() + ';' +
    'class Cap extends AudioWorkletProcessor{constructor(){super();this.rs=mk(sampleRate);}' +
    'process(inputs){var c=inputs[0]&&inputs[0][0];if(c&&c.length){var o=this.rs(c);for(var i=0;i<o.length;i++)this.port.postMessage(o[i],[o[i].buffer]);}return true;}}' +
    'registerProcessor("khy-cap",Cap);})();';

  var ctx = null;
  function prepare() {                      // call inside the tap that starts voice: some phones only allow sound after a tap
    var AC = global.AudioContext || global.webkitAudioContext;
    if (!AC) return null;
    if (!ctx || ctx.state === 'closed') { try { ctx = new AC({ latencyHint: 'interactive' }); } catch (e) { ctx = new AC(); } }
    if (ctx.state === 'suspended') { try { ctx.resume().catch(function () {}); } catch (e) {} }
    return ctx;
  }
  function discard() { if (ctx && ctx.state !== 'closed' && !ctx.__inUse) { try { ctx.close(); } catch (e) {} ctx = null; } }

  function Conn(ac, ws, opts) {
    this.ac = ac; this.ws = ws; this.o = opts; this.closed = false;
    this.out = ac.createGain(); this.out.connect(ac.destination);
    this.next = 0; this.playing = false; this.sources = []; this.item = null; this.curResp = ''; this.ignore = {}; this.startedFor = {};
    this.underruns = 0; this.gapMax = 0; this.lastDelta = 0; this.gapWin = []; this.sentChunks = 0; this.dropped = 0; this.chunks = 0;
    this.capNodes = [];
  }
  var C = Conn.prototype;
  Object.defineProperty(C, 'readyState', { get: function () { return this.ws && this.ws.readyState === 1 && !this.closed ? 'open' : 'closed'; } });

  C.emit = function (ev) { try { this.o.onEvent(ev); } catch (e) { if (global.console) console.warn('realtime event', e); } };

  // client events from the page
  C.send = function (m) {
    if (typeof m === 'string') { try { m = JSON.parse(m); } catch (e) { return; } }
    if (!m || this.closed) return;
    if (m.type === 'output_audio_buffer.clear') { this.clearPlayback(true); return; }          // WebRTC-only event: we do it ourselves
    if (this.ws.readyState === 1) this.ws.send(JSON.stringify(m));
  };

  /* ── mic ── */
  C.startCapture = function (mic) {
    var self = this, ac = this.ac;
    var src = ac.createMediaStreamSource(mic), mute = ac.createGain(); mute.gain.value = 0; mute.connect(ac.destination);
    this.capNodes.push(src, mute);
    function onChunk(i16) {
      if (self.closed || self.ws.readyState !== 1) return;
      // the link is slow: drop mic audio rather than queue it. Everything else we send ("answer now", notes) waits behind this
      // queue, so a long one (it was ~9 s) made the teacher answer late. ~200 KB is about 3 s of speech.
      if (self.ws.bufferedAmount > 200000) { self.dropped++; return; }
      self.ws.send('{"type":"input_audio_buffer.append","audio":"' + toB64(i16) + '"}'); self.sentChunks++;
    }
    function fallback() {
      var sp = ac.createScriptProcessor(2048, 1, 1), rs = makeResampler(ac.sampleRate);
      sp.onaudioprocess = function (e) { var o = rs(e.inputBuffer.getChannelData(0)); for (var i = 0; i < o.length; i++) onChunk(o[i]); };
      src.connect(sp); sp.connect(mute); self.capNodes.push(sp);
    }
    if (ac.audioWorklet && global.AudioWorkletNode && global.Blob && global.URL) {
      var url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }));
      return ac.audioWorklet.addModule(url).then(function () {
        URL.revokeObjectURL(url);
        var node = new AudioWorkletNode(ac, 'khy-cap', { numberOfInputs: 1, numberOfOutputs: 1, channelCount: 1 });
        node.port.onmessage = function (e) { onChunk(e.data); };
        src.connect(node); node.connect(mute); self.capNodes.push(node);
      }).catch(function () { fallback(); });
    }
    fallback();
    return Promise.resolve();
  };

  /* ── incoming ── */
  C.onMessage = function (raw) {
    var e; try { e = JSON.parse(raw); } catch (err) { return; }
    var t = e.type;
    if (t === 'response.output_audio.delta' || t === 'response.audio.delta') { this.playChunk(e); return; }
    if (t === 'response.created') { this.curResp = (e.response && e.response.id) || ''; }
    else if (t === 'input_audio_buffer.speech_started' && this.o.autoInterrupt) { this.clearPlayback(true); }
    else if (t === 'error' && /truncat|audio_end_ms|already shorter|item.*(not found|does not exist)/i.test(String(e.error && e.error.message || ''))) return;
    this.emit(e);
  };

  /* ── speaker ── */
  C.playChunk = function (e) {
    if (this.closed || (e.response_id && this.ignore[e.response_id])) return;
    var i16 = fromB64(e.delta || ''); if (!i16.length) return;
    var ac = this.ac, now = ac.currentTime;
    this.chunks++;
    var nowMs = Date.now(); if (this.lastDelta) { var gap = nowMs - this.lastDelta; this.gapWin.push([nowMs, gap]); } this.lastDelta = nowMs;
    var buf = ac.createBuffer(1, i16.length, RATE), f = buf.getChannelData(0), k;
    for (k = 0; k < i16.length; k++) f[k] = i16[k] / 32768;
    var t = this.next;
    if (!this.playing || t < now) {                 // the start of speech, or the audio ran dry: wait a moment so it does not stutter
      if (this.playing && t < now) this.underruns++;
      t = now + (this.playing ? 0.08 : 0.12); this.playing = true;
    }
    var s = ac.createBufferSource(); s.buffer = buf; s.connect(this.out); s.start(t); this.next = t + buf.duration;
    var self = this; this.sources.push(s);
    s.onended = function () {
      var i = self.sources.indexOf(s); if (i >= 0) self.sources.splice(i, 1);
      if (!self.sources.length && !self.closed && self.next <= self.ac.currentTime + 0.06) { self.playing = false; self.emit({ type: 'output_audio_buffer.stopped' }); }
    };
    var rid = e.response_id || '';
    if (!this.item || this.item.id !== e.item_id) this.item = { id: e.item_id, ci: e.content_index || 0, t0: t, dur: 0 };
    this.item.dur += buf.duration;
    if (!this.startedFor[rid]) {
      this.startedFor[rid] = true;
      setTimeout(function () { if (!self.closed) self.emit({ type: 'output_audio_buffer.started', response_id: rid }); }, Math.max(0, (t - now) * 1000));
    }
  };

  // stop the sound now; tell the model how much was really heard
  C.clearPlayback = function (truncate) {
    var ac = this.ac, it = this.item, had = this.sources.length > 0 || this.playing, ms = 0, i;
    if (it) ms = Math.max(0, Math.min(it.dur, ac.currentTime - it.t0)) * 1000;
    for (i = 0; i < this.sources.length; i++) { this.sources[i].onended = null; try { this.sources[i].stop(0); } catch (e) {} }
    this.sources = []; this.next = 0; this.playing = false;
    if (this.curResp) this.ignore[this.curResp] = true;
    this.item = null;
    if (truncate && it && it.id && had && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ type: 'conversation.item.truncate', item_id: it.id, content_index: it.ci, audio_end_ms: Math.floor(ms) }));
    }
    var me = this; Promise.resolve().then(function () { if (!me.closed) me.emit({ type: 'output_audio_buffer.cleared' }); });     // like WebRTC: after the call returns
  };

  C.stats = function () {
    var now = Date.now(), win = this.gapWin = this.gapWin.filter(function (g) { return now - g[0] < 5000; }), mx = 0;
    win.forEach(function (g) { if (g[1] > mx) mx = g[1]; });
    return { underruns: this.underruns, gapMax: mx, queuedMs: Math.max(0, Math.round((this.next - this.ac.currentTime) * 1000)),
             bufferedKB: Math.round((this.ws.bufferedAmount || 0) / 1024), sent: this.sentChunks, dropped: this.dropped, chunks: this.chunks, state: this.ac.state };
  };

  C.close = function () {
    if (this.closed) return; this.closed = true;
    try { this.ws.close(); } catch (e) {}
    this.capNodes.forEach(function (n) { try { n.disconnect(); } catch (e) {} });
    for (var i = 0; i < this.sources.length; i++) { this.sources[i].onended = null; try { this.sources[i].stop(0); } catch (e) {} }
    this.sources = [];
    var ac = this.ac; ac.__inUse = false; if (ctx === ac) ctx = null; try { ac.close(); } catch (e) {}
  };

  // opts: { model, key, mic, autoInterrupt, onEvent(ev), onClose() } -> Promise<Conn>
  function open(opts) {
    return new Promise(function (resolve, reject) {
      var ac = prepare();
      function fail(msg, stage) { var er = new Error(msg); er.stage = stage || 'channel'; reject(er); }
      if (!ac || !global.WebSocket) return fail('no web audio', 'channel');
      ac.__inUse = true;
      var ws;
      try { ws = new WebSocket('wss://api.openai.com/v1/realtime?model=' + encodeURIComponent(opts.model), ['realtime', 'openai-insecure-api-key.' + opts.key]); }
      catch (e) { ac.__inUse = false; return fail('websocket ' + e.message); }
      var conn = new Conn(ac, ws, opts), ready = false, timer = 0, giveUp = setTimeout(function () { try { ws.close(); } catch (e) {} fail('websocket did not open'); }, 20000);
      function go() {
        if (ready) return; ready = true; clearTimeout(timer); clearTimeout(giveUp);
        conn.startCapture(opts.mic).then(function () { resolve(conn); }, function (e) { try { ws.close(); } catch (e2) {} fail('mic: ' + (e && e.message)); });
      }
      ws.onmessage = function (m) { if (!ready) { try { if (JSON.parse(m.data).type === 'session.created') go(); } catch (e) {} } conn.onMessage(m.data); };
      ws.onopen = function () { timer = setTimeout(go, 1500); };           // normally session.created comes first
      ws.onerror = function () { if (!ready) { clearTimeout(giveUp); fail('websocket error'); } };
      ws.onclose = function (ev) {
        clearTimeout(giveUp);
        if (!ready) { fail('websocket closed ' + (ev && ev.code)); return; }
        if (!conn.closed && opts.onClose) opts.onClose(ev);
      };
    });
  }

  global.RealtimeWS = { open: open, prepare: prepare, discard: discard, _makeResampler: makeResampler, _toB64: toB64, _fromB64: fromB64 };
})(window);
