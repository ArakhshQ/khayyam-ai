
/* Khayyam AI inline Live Voice: shared by Chat and Tutor. */
(() => {
  'use strict';
  let state = null;
  const status = () => document.getElementById('voice-status');
  const button = () => document.getElementById('voice-record-btn');
  const note = text => { if (status()) status().textContent = text; };
  const setButton = active => {
    const btn = button();
    if (!btn) return;
    btn.classList.toggle('voice-active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    btn.title = active ? 'پایان گفتگوی زنده' : 'شروع گفتگوی زنده';
  };
  const csrfHeaders = () => ({ 'Content-Type': 'application/json' });

  function appendBubble(role, content, pending = false) {
    const wrap = document.getElementById('chat-messages') ||
                 document.getElementById('messages') ||
                 document.getElementById('tutor-chat-messages') ||
                 document.querySelector('.chat-messages');
    if (!wrap) return null;
    const div = document.createElement('div');
    div.className = 'message ' + (role === 'user' ? 'user' : 'assistant') + ' voice-transcript';
    if (pending) div.classList.add('voice-pending');
    div.textContent = content;
    wrap.appendChild(div);
    wrap.scrollTop = wrap.scrollHeight;
    return div;
  }

  async function saveTurn(role, text, turnId) {
    if (!state || !text.trim()) return;
    const s = state;
    if (s.saved.has(turnId)) return;
    s.saved.add(turnId);
    const body = {
      mode: s.mode,
      role,
      content: text.trim(),
      conversation_id: s.conversationId,
      subject: s.subject,
      topic_key: s.topicKey,
      turn_id: turnId
    };
    try {
      const res = await fetch('/api/voice/turn', {
        method: 'POST',
        headers: csrfHeaders(),
        body: JSON.stringify(body),
        credentials: 'same-origin'
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'save failed');
      if (data.conversation_id) {
        s.conversationId = data.conversation_id;
        if (s.mode === 'chat') {
          window.currentConvId = data.conversation_id;
          if (typeof window.setCurrentConversationId === 'function')
            window.setCurrentConversationId(data.conversation_id);
        }
      }
    } catch (err) {
      console.warn('Voice transcript save failed:', err);
      s.saved.delete(turnId);
      note('گفتگو ادامه دارد؛ ذخیرهٔ یک پیام ناموفق بود.');
    }
  }

  function getMode() {
    const cfg = window.KHAYYAM_VOICE_CONFIG || {};
    return {
      mode: cfg.mode === 'tutor' ? 'tutor' : 'chat',
      conversationId: typeof cfg.getConversationId === 'function'
        ? cfg.getConversationId() : (cfg.conversationId || null),
      subject: typeof cfg.getSubject === 'function'
        ? cfg.getSubject() : (cfg.subject || ''),
      topicKey: typeof cfg.getTopicKey === 'function'
        ? cfg.getTopicKey() : (cfg.topicKey || '')
    };
  }

  function onEvent(ev) {
    if (!state) return;
    const s = state;
    if (ev.type === 'input_audio_buffer.speech_started') {
      note('در حال شنیدن صدای شما...');
    }
    if (ev.type === 'input_audio_buffer.speech_stopped') {
      note('در حال آماده‌سازی پاسخ...');
    }
    if (ev.type === 'conversation.item.input_audio_transcription.completed') {
      const text = (ev.transcript || '').trim();
      if (!text) return;
      const id = 'user:' + (ev.item_id || Date.now());
      appendBubble('user', text);
      saveTurn('user', text, id);
    }
    if (ev.type === 'response.created') {
      note('خیام در حال پاسخ دادن است...');
    }
    if (ev.type === 'response.output_audio_transcript.delta' ||
        ev.type === 'response.audio_transcript.delta') {
      const id = ev.item_id || ev.response_id || 'current';
      if (!s.assistantTurns.has(id)) {
        s.assistantTurns.set(id, { text: '', element: appendBubble('assistant', '', true) });
      }
      const turn = s.assistantTurns.get(id);
      turn.text += ev.delta || '';
      if (turn.element) turn.element.textContent = turn.text;
    }
    if (ev.type === 'response.output_audio_transcript.done' ||
        ev.type === 'response.audio_transcript.done') {
      const id = ev.item_id || ev.response_id || 'current';
      const existing = s.assistantTurns.get(id);
      const text = (ev.transcript || (existing && existing.text) || '').trim();
      if (!text) return;
      if (existing && existing.element) {
        existing.element.textContent = text;
        existing.element.classList.remove('voice-pending');
      } else {
        appendBubble('assistant', text);
      }
      saveTurn('assistant', text, 'assistant:' + id);
      s.assistantTurns.delete(id);
    }
    if (ev.type === 'response.done') {
      note('می‌توانید صحبت کنید.');
    }
    if (ev.type === 'error') {
      console.warn('Realtime event error:', ev.error);
      note('یک خطا در گفتگوی صوتی رخ داد.');
    }
  }

  async function start() {
    if (state) return;
    const config = getMode();
    state = {
      ...config,
      saved: new Set(),
      assistantTurns: new Map(),
      pc: null,
      dc: null,
      stream: null,
      audio: null,
      stopping: false
    };
    const s = state;
    setButton(true);
    note('در حال اتصال...');
    try {
      const res = await fetch('/api/voice/session', {
        method: 'POST',
        headers: csrfHeaders(),
        credentials: 'same-origin',
        body: JSON.stringify({
          mode: s.mode,
          subject: s.subject,
          topic_key: s.topicKey,
          conversation_id: s.conversationId
        })
      });
      const data = await res.json();
      if (!res.ok || !data.value) throw new Error(data.error || 'No session token');

      s.pc = new RTCPeerConnection();
      s.audio = document.createElement('audio');
      s.audio.autoplay = true;
      s.audio.style.display = 'none';
      document.body.appendChild(s.audio);
      s.pc.ontrack = event => {
        s.audio.srcObject = event.streams[0];
        s.audio.play().catch(console.warn);
      };
      s.pc.onconnectionstatechange = () => {
        if (state !== s) return;
        if (['failed', 'closed', 'disconnected'].includes(s.pc.connectionState)) {
          stop();
        }
      };
      s.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true }
      });
      s.stream.getTracks().forEach(track => s.pc.addTrack(track, s.stream));
      s.dc = s.pc.createDataChannel('oai-events');
      s.dc.onopen = () => {
        if (state !== s) return;
        note('وصل شدید. صحبت کنید!');
      };
      s.dc.onmessage = event => {
        try { onEvent(JSON.parse(event.data)); }
        catch (err) { console.warn('Voice event parsing failed', err); }
      };
      const offer = await s.pc.createOffer();
      await s.pc.setLocalDescription(offer);
      const answer = await fetch('https://api.openai.com/v1/realtime/calls', {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + data.value,
          'Content-Type': 'application/sdp'
        },
        body: offer.sdp
      });
      if (!answer.ok) throw new Error('WebRTC connection failed: ' + answer.status);
      await s.pc.setRemoteDescription({ type: 'answer', sdp: await answer.text() });
      note('وصل شدید. صحبت کنید!');
    } catch (err) {
      console.error('Inline Live Voice failed:', err);
      stop();
      note('اتصال برقرار نشد. دوباره تلاش کنید.');
    }
  }

  function stop() {
    if (!state) return;
    const s = state;
    if (s.stopping) return;
    s.stopping = true;
    state = null;
    try { if (s.dc) s.dc.close(); } catch (_) {}
    try { if (s.pc) s.pc.close(); } catch (_) {}
    try { if (s.stream) s.stream.getTracks().forEach(t => t.stop()); } catch (_) {}
    try {
      if (s.audio) {
        s.audio.pause();
        s.audio.srcObject = null;
        s.audio.remove();
      }
    } catch (_) {}
    setButton(false);
    note('گفتگوی صوتی پایان یافت.');
  }

  function toggle() {
    if (state) stop();
    else start();
  }

  window.KhayyamLiveVoice = { start, stop, toggle, isActive: () => !!state };
  window.addEventListener('pagehide', stop);
})();
