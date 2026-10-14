
/* Khayyam AI inline Live Voice: shared by Chat and Tutor. */
(() => {
  'use strict';
  let state = null;
  const status = () => document.getElementById('voice-status');
  const button = () => document.getElementById('voice-record-btn');
  const note = text => { if (status()) status().textContent = text; };
  const setButton = active => {
    const el = button(); if (!el) return;
    el.classList.toggle('recording', active);
    el.textContent = active ? '⏹' : '🎙';
    el.title = active ? 'پایان گفتگوی زنده' : 'شروع گفتگوی زنده';
    el.setAttribute('aria-label', el.title);
  };
  const render = (role, content) => {
    if (!content) return;
    if (typeof window.khayyamVoiceRender === 'function') window.khayyamVoiceRender(role, content);
  };
  function teardown(s) {
    if (!s || s.closed) return;
    s.closed = true;
    try { s.dc?.close(); } catch (_) {}
    try { s.pc?.close(); } catch (_) {}
    try { s.mic?.getTracks().forEach(t => t.stop()); } catch (_) {}
    try { s.audio?.pause(); s.audio?.remove(); } catch (_) {}
    if (state === s) state = null;
    setButton(false);
  }
  async function flush(s) {
    if (s.saving || !s.receipt || !s.pending.length) return;
    s.saving = true;
    try {
      while (s.pending.length) {
        const batch = s.pending.slice(0, 12);
        const response = await fetch('/api/voice/save', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({receipt: s.receipt, turns: batch.map(t => ({role:t.role, content:t.content}))})
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Could not save voice messages');
        s.pending.splice(0, batch.length);
        if (result.conversation_id && typeof window.khayyamVoiceSavedConversation === 'function')
          window.khayyamVoiceSavedConversation(result.conversation_id);
      }
      if (s.ending) note('گفتگوی صوتی پایان یافت.');
    } catch (err) {
      console.error('Voice save:', err);
      note('ذخیره بعضی پیام‌های صوتی ناموفق بود. برای تلاش دوباره میکروفون را بزنید.');
    } finally {
      s.saving = false;
    }
  }
  function complete(s, role, key, text) {
    text = String(text || '').trim();
    if (!text || s.completed.has(role + ':' + key)) return;
    s.completed.add(role + ':' + key);
    render(role, text);
    s.pending.push({role, content:text});
    flush(s);
  }
  function onEvent(s, d) {
    if (d.type === 'input_audio_buffer.speech_started') note('در حال شنیدن...');
    if (d.type === 'conversation.item.input_audio_transcription.completed') {
      complete(s, 'user', d.item_id || 'u' + (++s.seq), d.transcript);
    }
    if (d.type === 'response.created') note('خیام در حال پاسخ دادن است...');
    if (d.type === 'response.output_audio_transcript.delta' || d.type === 'response.audio_transcript.delta') {
      const id = d.response_id || 'current';
      s.assistantDrafts.set(id, (s.assistantDrafts.get(id) || '') + (d.delta || ''));
    }
    if (d.type === 'response.output_audio_transcript.done' || d.type === 'response.audio_transcript.done') {
      const id = d.response_id || 'current';
      s.assistantDrafts.set(id, d.transcript || s.assistantDrafts.get(id) || '');
    }
    if (d.type === 'response.done') {
      const id = d.response?.id || 'a' + (++s.seq);
      const fromOutput = (d.response?.output || []).flatMap(o => o.content || [])
        .map(c => c.transcript || c.text || '').filter(Boolean).join(' ');
      const text = fromOutput || s.assistantDrafts.get(id) || '';
      // A cancelled/interrupted reply may be partial. Save only if there is actual text.
      if (text) complete(s, 'assistant', id, text);
      note('متصل است — صحبت کنید.');
    }
    if (d.type === 'error') {
      console.error('Realtime:', d.error);
      note('خطای صوتی رخ داد. اتصال را بررسی کنید.');
    }
  }
  async function start(context) {
    const s = {closed:false, ending:false, saving:false, pending:[], completed:new Set(),
      assistantDrafts:new Map(), seq:0, receipt:null, pc:null, dc:null, mic:null, audio:null};
    state = s;
    setButton(true);
    note('در حال اتصال...');
    try {
      s.mic = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
      if (s.closed) return;
      const response = await fetch('/api/voice/session', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(context)});
      const session = await response.json();
      if (!response.ok || !session.value || !session.receipt) throw new Error(session.error || 'Voice session unavailable');
      if (s.closed) return;
      s.receipt = session.receipt;
      s.pc = new RTCPeerConnection();
      s.audio = document.createElement('audio');
      s.audio.autoplay = true;
      document.body.appendChild(s.audio);
      s.pc.ontrack = event => { s.audio.srcObject = event.streams[0]; s.audio.play().catch(console.warn); };
      s.pc.onconnectionstatechange = () => {
        if (s.closed) return;
        if (['failed','disconnected','closed'].includes(s.pc.connectionState)) note('اتصال صوتی قطع شد. برای پایان میکروفون را بزنید.');
      };
      s.mic.getTracks().forEach(track => s.pc.addTrack(track, s.mic));
      s.dc = s.pc.createDataChannel('oai-events');
      s.dc.onopen = () => { if (!s.closed) note('متصل شد — صحبت کنید.'); };
      s.dc.onmessage = e => { if (!s.closed) { try { onEvent(s, JSON.parse(e.data)); } catch (err) { console.warn(err); } } };
      const offer = await s.pc.createOffer();
      await s.pc.setLocalDescription(offer);
      const answer = await fetch('https://api.openai.com/v1/realtime/calls', {
        method:'POST', headers:{Authorization:'Bearer ' + session.value,'Content-Type':'application/sdp'}, body:offer.sdp
      });
      if (!answer.ok) throw new Error('Realtime connection failed: ' + answer.status);
      const sdp = await answer.text();
      if (s.closed) return;
      await s.pc.setRemoteDescription({type:'answer',sdp});
      if (!s.closed) note('متصل شد — صحبت کنید.');
    } catch (err) {
      console.error('Live Voice:', err);
      teardown(s);
      note('اتصال برقرار نشد: ' + err.message);
    }
  }
  async function stop(s) {
    if (!s || s.ending) return;
    s.ending = true;
    note('در حال پایان گفتگو...');
    // Give the transcription service a short opportunity to finish the last utterance.
    if (s.dc?.readyState === 'open') await new Promise(resolve => setTimeout(resolve, 1000));
    teardown(s);
    await flush(s);
    if (s.pending.length) note('بعضی پیام‌ها ذخیره نشدند. برای تلاش دوباره میکروفون را بزنید.');
    else note('گفتگوی صوتی پایان یافت.');
  }
  window.KhayyamInlineVoice = {
    toggle: async context => {
      if (state) { await stop(state); return; }
      await start(context);
    },
    isActive: () => !!state,
    retrySave: () => { if (state) return flush(state); },
    stop: () => state ? stop(state) : Promise.resolve()
  };
  window.addEventListener('pagehide', () => { if (state) teardown(state); });
})();
