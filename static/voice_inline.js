/* Khayyam AI inline Live Voice: shared by Chat and Tutor. */
(() => {
  'use strict';
  let state = null;
  const status = () => document.getElementById('voice-status');
  const button = () => document.getElementById('voice-record-btn');
  let toastTimer = null;
  function showToast(text, isError = false) {
    let toast = document.getElementById('khayyam-voice-toast');
    if (!toast) {
      toast = document.createElement('div');
      toast.id = 'khayyam-voice-toast';
      toast.setAttribute('role', 'status');
      Object.assign(toast.style, {
        position: 'fixed', top: 'max(16px, env(safe-area-inset-top))',
        left: '50%', transform: 'translate(-50%, -12px)',
        zIndex: '99999', maxWidth: 'min(92vw, 440px)',
        padding: '11px 18px', borderRadius: '12px',
        background: '#0f3a2f', color: '#fbf7ec',
        border: '1px solid #d8b25a',
        boxShadow: '0 8px 28px rgba(0,0,0,.35)',
        fontFamily: 'Vazirmatn, Tahoma, sans-serif',
        fontSize: '13px', textAlign: 'center', direction: 'rtl',
        opacity: '0', pointerEvents: 'none',
        transition: 'opacity .2s ease, transform .2s ease'
      });
      document.body.appendChild(toast);
    }
    clearTimeout(toastTimer);
    toast.textContent = text;
    toast.style.borderColor = isError ? '#d77575' : '#d8b25a';
    toast.style.opacity = '1';
    toast.style.transform = 'translate(-50%, 0)';
    toastTimer = setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translate(-50%, -12px)';
    }, 2800);
  }
  const note = (text, popup = false) => {
    if (status()) status().textContent = text;
    if (popup) showToast(text, /خطا|ناموفق|قطع شد|نشد/.test(text));
  };
  const setButton = active => {
    const el = button(); if (!el) return;
    el.classList.toggle('recording', active);
    el.innerHTML = active
      ? '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6.5" y="6.5" width="11" height="11" rx="2.5" fill="currentColor"/></svg>'
      : '<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3"/></g></svg>';
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
    if (s.quotaInterval) clearInterval(s.quotaInterval);
    try { s.dc?.close(); } catch (_) {}
    try { s.pc?.close(); } catch (_) {}
    try { s.mic?.getTracks().forEach(t => t.stop()); } catch (_) {}
    try { s.audio?.pause(); s.audio?.remove(); } catch (_) {}
    if (state === s) state = null;
    setButton(false);
  }
  const STORAGE_KEY = 'khayyam_voice_pending_v3';
  function persist(s) {
    try {
      if (s.pending.length) localStorage.setItem(STORAGE_KEY, JSON.stringify({receipt:s.receipt, pending:s.pending}));
      else localStorage.removeItem(STORAGE_KEY);
    } catch (err) { console.warn('Could not persist voice queue:', err); }
  }
  async function flush(s) {
    if (s.saving || !s.receipt || !s.pending.length) return;
    s.saving = true;
    try {
      while (s.pending.length) {
        const batch = s.pending.slice(0, 12);
        const response = await fetch('/api/voice/save', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({receipt:s.receipt, turns:batch})
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Could not save voice messages');
        s.pending.splice(0, batch.length);
        persist(s);
        if (result.conversation_id && typeof window.khayyamVoiceSavedConversation === 'function')
          window.khayyamVoiceSavedConversation(result.conversation_id);
      }
    } catch (err) {
      console.error('Voice save:', err);
      persist(s);
      note('ذخیره پیام صوتی ناموفق بود؛ پیام‌ها برای تلاش دوباره نگه داشته شدند.', true);
    } finally {
      s.saving = false;
    }
  }
  function complete(s, role, key, text) {
    text = String(text || '').trim();
    const turn_id = role + ':' + String(key || (++s.seq)).replace(/[^A-Za-z0-9:_-]/g, '_').slice(0, 145);
    if (!text || s.completed.has(turn_id)) return;
    s.completed.add(turn_id);
    render(role, text);
    s.pending.push({turn_id, role, content:text});
    persist(s);
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
    await recoverPending();
    if (localStorage.getItem(STORAGE_KEY)) {
      note('پیام‌های قبلی هنوز ذخیره نشده‌اند. دوباره تلاش کنید.', true);
      return;
    }
    const s = {closed:false, ending:false, saving:false, pending:[], completed:new Set(),
      assistantDrafts:new Map(), seq:0, receipt:null, pc:null, dc:null, mic:null, audio:null};
    state = s;
    setButton(true);
    note('در حال اتصال...', true);
    try {
      s.mic = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true}});
      if (s.closed) return;
      const response = await fetch('/api/voice/session', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(context)});
      const session = await response.json();
      if (!response.ok || !session.value || !session.receipt) throw new Error(session.error || 'Voice session unavailable');
      if (s.closed) return;
      s.receipt = session.receipt;
      // The quota deadline is issued by the backend, not trusted client input.
      s.deadline = Number(session.allowance_deadline || 0) * 1000;
      s.warningSeconds = Number(session.warning_seconds || 120);
      s.warned = false;
      s.quotaInterval = setInterval(() => {
        if (s.closed || !s.deadline) return;
        const left = Math.max(0, Math.ceil((s.deadline - Date.now()) / 1000));
        if (!s.warned && left <= s.warningSeconds && left > 0) {
          s.warned = true;
          note(`از سهمیه صوتی شما ${Math.ceil(left / 60)} دقیقه باقی مانده است.`, true);
        }
        if (left === 0) {
          note('سهمیه صوتی این جلسه تمام شد.', true);
          stop(s);
        }
      }, 1000);
      s.pc = new RTCPeerConnection();
      s.audio = document.createElement('audio');
      s.audio.autoplay = true;
      document.body.appendChild(s.audio);
      s.pc.ontrack = event => { s.audio.srcObject = event.streams[0]; s.audio.play().catch(console.warn); };
      s.pc.onconnectionstatechange = () => {
        if (s.closed) return;
        if (['failed','disconnected','closed'].includes(s.pc.connectionState)) note('اتصال صوتی قطع شد. برای پایان میکروفون را بزنید.', true);
      };
      s.mic.getTracks().forEach(track => s.pc.addTrack(track, s.mic));
      s.dc = s.pc.createDataChannel('oai-events');
      s.dc.onopen = () => { if (!s.closed) note('متصل شد — صحبت کنید.', true); };
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
      if (!s.closed) note('متصل شد — صحبت کنید.', true);
    } catch (err) {
      console.error('Live Voice:', err);
      teardown(s);
      note('اتصال برقرار نشد: ' + err.message, true);
    }
  }
  async function stop(s) {
    if (!s || s.ending) return;
    s.ending = true;
    note('در حال پایان گفتگو...');
    // Give the transcription service a short opportunity to finish the last utterance.
    if (s.dc?.readyState === 'open') await new Promise(resolve => setTimeout(resolve, 1500));
    teardown(s);
    if (s.receipt) {
      try { await fetch('/api/voice/finish', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({receipt:s.receipt}), keepalive:true
      }); } catch (err) { console.warn('Voice quota finalize:', err); }
    }
    if (s.saving) {
      while (s.saving) await new Promise(resolve => setTimeout(resolve, 80));
    }
    await flush(s);
    if (s.pending.length) note('بعضی پیام‌ها هنوز ذخیره نشده‌اند؛ بعداً دوباره تلاش می‌شود.', true);
    else note('گفتگوی صوتی پایان یافت.', true);
  }
  window.KhayyamInlineVoice = {
    toggle: async context => {
      if (state) { await stop(state); return; }
      await start(context);
    },
    isActive: () => !!state,
    retrySave: () => { if (state) return flush(state); return recoverPending(); },
    stop: () => state ? stop(state) : Promise.resolve()
  };
  async function recoverPending() {
    try {
      const data = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null');
      if (!data?.receipt || !Array.isArray(data.pending) || !data.pending.length) return;
      const recovery = {receipt:data.receipt, pending:data.pending, saving:false};
      await flush(recovery);
    } catch (err) { console.warn('Voice recovery:', err); }
  }
  // Recover acknowledged-or-not saves after refresh. The server deduplicates turn IDs.
  recoverPending();
  window.addEventListener('online', recoverPending);
  window.addEventListener('pagehide', () => {
    if (state) {
      persist(state);
      if (state.receipt) {
        try { navigator.sendBeacon('/api/voice/finish',
          new Blob([JSON.stringify({receipt:state.receipt})], {type:'application/json'})); }
        catch (_) { /* server reserves abandoned time */ }
      }
      teardown(state);
    }
  });
})();
