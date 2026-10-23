/* Tutor classroom: one continuous class on the chalkboard.
   The teacher writes the class a few short steps at a time (/api/class/*); this file plays them on the board, keeps the next steps
   ready before they are needed, takes the student's questions and answers at any moment, and saves the place so the class can
   continue another day. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var isMobile = function () { return window.matchMedia('(max-width: 860px)').matches; };
  var sidebarOpen = !isMobile();
  var board = null;
  var cls = null;            // the class on the board: { id, title, outline, step, status, done }
  var sampleOn = false;      // the built-in sample is showing (no server)
  var handUp = false;
  var busy = false;          // a message from the student is being answered
  var inflight = false;      // the next steps are being written
  var failures = 0;
  var spoken = {};           // step index -> true when its line should also appear in the conversation
  var stopAt = -1;           // step after which the class is saved and closed
  var closing = false;       // the class is being saved: nothing more is fetched or played
  var posTimer = 0, retryTimer = 0, delTimer = 0;
  var saved = [];            // the student's saved classes
  var live = null;           // the live voice session, when the class is spoken
  var liveAll = [];          // every beat the server has accepted for the live class, in order (the board may still be catching up)
  var liveWanted = !!window.__LIVE_VOICE && !!window.TutorLive;

  /* ───────── helpers ───────── */
  function toast(text) {
    var el = $('toast');
    el.textContent = text; el.classList.add('show');
    clearTimeout(toast._t); toast._t = setTimeout(function () { el.classList.remove('show'); }, 3600);
  }
  function faNum(n) { return String(n).replace(/\d/g, function (d) { return '۰۱۲۳۴۵۶۷۸۹'[d]; }); }
  function api(url, body, method) {
    var init = { method: method || (body ? 'POST' : 'GET'), credentials: 'same-origin', headers: {} };
    if (body) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    return fetch(url, init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { d.__status = r.status; return d; });
    });
  }
  window.toggleSidebar = function () { sidebarOpen = !sidebarOpen; applySidebar(); };
  function applySidebar() {
    $('sidebar').classList.toggle('collapsed', !sidebarOpen);
    $('scrim').classList.toggle('show', sidebarOpen && isMobile());
  }
  window.showPane = function (name) {
    $('classroom').dataset.pane = name;
    ['board', 'chat'].forEach(function (n) {
      var t = $('tab-' + n); t.classList.toggle('active', n === name); t.setAttribute('aria-selected', n === name ? 'true' : 'false');
    });
    if (name === 'board' && board) setTimeout(function () { board.onResize(); }, 60);
  };
  window.voiceSoon = function () {
    if (live) { var m = !live.muted; live.setMuted(m); $('voice-record-btn').classList.toggle('muted', m); toast(m ? 'میکروفون خاموش شد.' : 'میکروفون روشن شد.'); return; }
    if (liveWanted && !busy && !closing) { if (cls && !cls.voiceClass && cls.id) { toast('برای ادامه با صدا، صنف را باز کنید و «ادامهٔ صنف» را بزنید.'); return; } startLiveClass(''); return; }
    toast('گفتگوی صوتی هنوز برای حساب شما باز نشده است.');
  };

  /* ───────── conversation ───────── */
  function svgIcon(id) {
    var s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    var u = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    u.setAttribute('href', '#' + id); s.appendChild(u); return s;
  }
  function addMessage(role, text, extra) {
    var wrap = document.createElement('div'); wrap.className = 'message ' + role;
    var av = document.createElement('div'); av.className = 'avatar ' + role;
    if (role === 'ai') av.appendChild(svgIcon('i-cap'));
    var bubble = document.createElement('div'); bubble.className = 'bubble';
    var c = document.createElement('div'); c.className = 'bubble-content';
    c.textContent = text;
    if (extra) c.appendChild(extra);
    bubble.appendChild(c); wrap.appendChild(av); wrap.appendChild(bubble);
    $('messages').appendChild(wrap); scrollDown(); return wrap;
  }
  function linkRow(label, fn, cname) {
    var row = document.createElement('div'); row.style.marginTop = '10px'; if (cname) row.className = cname;
    var a = document.createElement('a'); a.href = '#'; a.textContent = label;
    a.addEventListener('click', function (e) { e.preventDefault(); fn(a); });
    row.appendChild(a); return row;
  }
  function typing() {
    if ($('typing')) return;
    var wrap = document.createElement('div'); wrap.className = 'message ai'; wrap.id = 'typing';
    var av = document.createElement('div'); av.className = 'avatar ai'; av.appendChild(svgIcon('i-cap'));
    var b = document.createElement('div'); b.className = 'bubble';
    var d = document.createElement('div'); d.className = 'typing-dots';
    d.innerHTML = '<span></span><span></span><span></span>';
    b.appendChild(d); wrap.appendChild(av); wrap.appendChild(b);
    $('messages').appendChild(wrap); scrollDown();
  }
  function untyping() { var t = $('typing'); if (t) t.remove(); }
  function scrollDown() { var w = $('chat-wrapper'); w.scrollTop = w.scrollHeight; }
  function placeholder(t) { $('user-input').placeholder = t; }

  function showStart() {
    var m = $('messages'); m.textContent = '';
    m.appendChild($('start-tpl').content.cloneNode(true));
    m.querySelectorAll('[data-say]').forEach(function (b) {
      b.addEventListener('click', function () { $('user-input').value = b.dataset.say; sendMessage(); });
    });
    renderResumeBox();
  }
  function renderResumeBox() {
    var box = document.getElementById('resume-box'); if (!box) return;
    box.textContent = '';
    saved.slice(0, 3).forEach(function (c) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'resume-card';
      var t = document.createElement('span'); t.className = 'rc-title'; t.textContent = c.title;
      var m = document.createElement('span'); m.className = 'rc-meta'; m.textContent = (c.status === 'done' ? 'تمام‌شده' : 'ادامه از همان‌جا') + ' · ' + faNum(Math.max(1, Math.round(c.minutes))) + ' دقیقه';
      b.appendChild(t); b.appendChild(m);
      b.addEventListener('click', function () { openSaved(c.id); });
      box.appendChild(b);
    });
  }

  /* ───────── the sidebar ───────── */
  function renderPath() {
    var box = $('path-box');
    if (!cls || cls.lobby || !(cls.outline || []).length) { box.hidden = true; return; }
    box.hidden = false;
    $('course-name').textContent = cls.title;
    var list = $('lesson-list'); list.textContent = '';
    var out = cls.outline || [];
    out.forEach(function (title, i) {
      var b = document.createElement('div');
      b.className = 'lesson' + (i < cls.step ? ' done' : '') + (i === cls.step ? ' current' : '');
      b.style.setProperty('--i', i);
      var dot = document.createElement('span'); dot.className = 'lesson-dot';
      if (i < cls.step) dot.appendChild(svgIcon('i-check')); else dot.textContent = faNum(i + 1);
      var t = document.createElement('span'); t.textContent = title;
      b.appendChild(dot); b.appendChild(t); list.appendChild(b);
    });
    var n = Math.max(1, out.length);
    $('course-count').textContent = 'مرحلهٔ ' + faNum(Math.min(cls.step + 1, n)) + ' از ' + faNum(n);
    $('course-progress-fill').style.width = (Math.min(cls.step, n) / n * 100) + '%';
  }
  function renderSaved() {
    var list = $('class-list'); list.textContent = '';
    if (!saved.length) {
      var e = document.createElement('div'); e.className = 'sidebar-empty'; e.id = 'class-empty';
      e.innerHTML = 'هنوز صنفی شروع نشده است.<br>به استاد بگویید چه چیزی می‌خواهید بیاموزید؛ صنف‌هایتان همین‌جا ذخیره می‌شوند و هر روز از همان‌جا ادامه می‌دهید.';
      list.appendChild(e); return;
    }
    saved.forEach(function (c, i) {
      var row = document.createElement('div'); row.className = 'class-item' + (cls && cls.id === c.id ? ' current' : ''); row.style.setProperty('--i', i);
      var b = document.createElement('button'); b.type = 'button'; b.className = 'class-open';
      var t = document.createElement('span'); t.className = 'ci-title'; t.textContent = c.title;
      var m = document.createElement('span'); m.className = 'ci-meta';
      m.textContent = (c.status === 'done' ? 'تمام‌شده' : c.status === 'paused' ? 'ذخیره‌شده' : 'در جریان') + ' · ' + faNum(Math.max(1, Math.round(c.minutes))) + ' دقیقه';
      b.appendChild(t); b.appendChild(m);
      b.addEventListener('click', function () { openSaved(c.id); if (isMobile()) { sidebarOpen = false; applySidebar(); } });
      var d = document.createElement('button'); d.type = 'button'; d.className = 'class-del'; d.setAttribute('aria-label', 'حذف صنف');
      d.appendChild(svgIcon('i-close'));
      d.addEventListener('click', function () {
        if (d.classList.contains('sure')) { removeClass(c.id); return; }
        d.classList.add('sure'); d.textContent = 'مطمئنید؟';
        clearTimeout(delTimer); delTimer = setTimeout(function () { d.classList.remove('sure'); d.textContent = ''; d.appendChild(svgIcon('i-close')); }, 3000);
      });
      row.appendChild(b); row.appendChild(d); list.appendChild(row);
    });
  }
  function loadSaved() {
    return api('/api/class/list').then(function (d) {
      if (d.__status !== 200) return;
      saved = d.classes || []; renderSaved(); renderResumeBox();
    }).catch(function () {});
  }
  function removeClass(id) {
    api('/api/class/' + id, null, 'DELETE').then(function (d) {
      if (d.ok) { if (cls && cls.id === id) newClass(); loadSaved(); } else toast('حذف ممکن نشد.');
    });
  }

  /* ───────── the board ───────── */
  function ensureBoard() {
    if (board) return board;
    board = new KB.Board($('board-stage')); window.__board = board;
    board.on('say', onSay).on('scene', onScene).on('time', onTime).on('state', onState).on('end', onEnd)
      .on('ask', onAsk).on('timeline', buildScrub).on('sceneEnd', onSceneEnd);
    return board;
  }
  function stepCount() { return board && board.model ? board.model.scenes.length : 0; }
  function buildScrub() {
    var box = $('scrub'); if (!board || !board.model) return;
    var n = stepCount();
    box.textContent = ''; box.classList.toggle('dense', n > 40);
    for (var i = 0; i < n; i++) {
      (function (i) {
        var b = document.createElement('button'); b.type = 'button'; b.setAttribute('aria-label', 'بخش ' + faNum(i + 1));
        b.appendChild(document.createElement('i'));
        b.addEventListener('click', function () { board.gotoScene(i, board.playing); setHand(false); });
        box.appendChild(b);
      })(i);
    }
    markScrub(board.sceneIdx);
  }
  function markScrub(i) {
    var kids = $('scrub').children;
    for (var k = 0; k < kids.length; k++) {
      kids[k].classList.toggle('now', k === i); kids[k].classList.toggle('done', k < i);
      kids[k].firstChild.style.setProperty('--p', k < i ? 1 : 0);
    }
    $('btn-prev').disabled = i === 0 && board.st < 2;
    $('btn-next').disabled = i >= stepCount() - 1;
  }
  function setWaiting(on, text) {
    var cap = $('caption');
    cap.classList.toggle('waiting', !!on);
    if (on) { cap.textContent = ''; var s = document.createElement('span'); s.textContent = text || 'استاد در حال نوشتن است…'; cap.appendChild(s); }
  }
  function onScene(i) {
    setWaiting(false);
    markScrub(i);
    if (!cls || live || cls.voiceClass) return;
    savePosSoon(i);
    var left = stepCount() - 1 - i;
    if (left <= 1) fetchNext();
  }
  function onSay(text, i) {
    if (live || (cls && cls.voiceClass)) return;
    var cap = $('caption'); cap.classList.remove('waiting'); cap.textContent = '';
    var s = document.createElement('span'); s.textContent = text; cap.appendChild(s);
    var tr = $('transcript'); var p = document.createElement('p'); p.textContent = text; tr.appendChild(p);
    var sc = board.lesson && board.lesson.scenes[i];
    if (cls && (spoken[i] || (sc && sc.ask))) addMessage('ai', text);
  }
  function onTime(t, dur) {
    var kids = $('scrub').children, i = board.sceneIdx;
    if (kids[i]) kids[i].firstChild.style.setProperty('--p', Math.min(1, t / dur));
  }
  function onState() {
    var b = $('btn-play'); if (!b) return;
    b.classList.toggle('playing', !!board.playing);
    b.setAttribute('aria-label', board.playing ? 'ایست' : 'پخش');
  }
  function onSceneEnd(i) {
    if (cls && stopAt >= 0 && i >= stopAt) { stopAt = -1; closing = true; board.holdAtEnd = true; board.pause(); setTimeout(function () { saveAndClose(true); }, 700); }
  }
  function onAsk() {
    placeholder('جواب شما…');
    if (isMobile()) showPane('chat');
    $('user-input').focus();
    var row = linkRow('ادامه بدون جواب', function (a) { a.parentNode.remove(); placeholder('سؤال یا جوابتان را بنویسید…'); showPane('board'); board.play(); }, 'skiprow');
    var last = $('messages').lastChild;
    if (last && last.querySelector('.bubble-content')) last.querySelector('.bubble-content').appendChild(row);
  }
  function onEnd() {
    if (closing || live || (cls && cls.voiceClass)) return;
    if (!cls) { if (sampleOn) addMessage('ai', 'درس نمونه تمام شد. بنویسید چه می‌خواهید یاد بگیرید تا استاد برایتان یک صنف واقعی بچیند.'); return; }
    if (cls.done) {
      if (cls.endNoted) return; cls.endNoted = true;
      addMessage('ai', 'این مسیر تمام شد. اگر سؤالی دارید همین‌جا بنویسید، یا «امروز کافی است» را بزنید تا صنف ذخیره شود.');
      return;
    }
    if (inflight) setWaiting(true); else { setWaiting(true); fetchNext(); }
  }
  window.boardToggle = function () { if (board) { if (board.playing) board.pause(); else board.play(); setHand(false); } };
  window.boardNext = function () { if (board) board.next(); };
  window.boardPrev = function () { if (board) board.prev(); };
  window.boardSpeed = function () {
    if (!board) return;
    var steps = [1, 1.25, 1.5, .75], cur = steps.indexOf(board.speed), v = steps[(cur + 1) % steps.length];
    board.setSpeed(v);
    $('btn-speed').textContent = { 1: '۱', 1.25: '۱٫۲۵', 1.5: '۱٫۵', .75: '۰٫۷۵' }[v] + '×';
  };
  function setHand(on) { handUp = on; $('btn-hand').classList.toggle('on', on); }
  window.raiseHand = function () {
    if (live) { live.interrupt(); $('voice-record-btn').classList.remove('muted'); toast('استاد گوش می‌دهد؛ صحبت کنید.'); return; }
    if (!board || !board.model) { toast('اول یک صنف شروع کنید.'); return; }
    if (handUp) { setHand(false); board.play(); return; }
    board.pause(); setHand(true);
    toast('درس ایستاد. سؤالتان را بنویسید.');
    if (isMobile()) showPane('chat');
    $('user-input').focus();
  };

  /* ───────── writing the class ahead of the student ───────── */
  function fetchNext() {
    if (live || (cls && cls.voiceClass) || !cls || inflight || cls.done || cls.limited || busy || stopAt >= 0 || closing) return;
    inflight = true;
    var have = board.lesson.scenes.length, id = cls.id;
    api('/api/class/' + id + '/next', { have: have }).then(function (d) {
      inflight = false;
      if (!cls || cls.id !== id) return;
      if (d.busy) { clearTimeout(retryTimer); retryTimer = setTimeout(fetchNext, 2500); return; }
      if (d.resync) { reloadFromServer(); return; }
      if (d.limited) { limitNotice(d.reset_ts); cls.limited = true; setWaiting(false); return; }
      if (d.__status !== 200 || d.error) { failNext(); return; }
      failures = 0;
      if (typeof d.step === 'number') { cls.step = d.step; renderPath(); }
      if (d.action === 'done') cls.done = true;
      if (d.beats && d.beats.length) board.setTimeline(d.from, d.beats);
      else if (board.atEnd && !cls.done) { clearTimeout(retryTimer); retryTimer = setTimeout(fetchNext, 2500); }
      if (cls.done && board.atEnd) onEnd();
      var left = stepCount() - 1 - board.sceneIdx;
      if (left <= 1 && !cls.done) setTimeout(fetchNext, 400);
    }).catch(function () { inflight = false; failNext(); });
  }
  function failNext() {
    failures++;
    if (failures <= 2) { clearTimeout(retryTimer); retryTimer = setTimeout(fetchNext, 4000); return; }
    setWaiting(false);
    addMessage('ai', 'ارتباط با استاد قطع شد.', linkRow('دوباره امتحان کنید', function (a) { a.parentNode.remove(); failures = 0; fetchNext(); }));
  }
  function limitNotice(ts) {
    var when = '';
    if (ts) { try { when = ' دوباره از ' + new Date(ts * 1000).toLocaleString('fa-AF', { hour: '2-digit', minute: '2-digit', day: 'numeric', month: 'long' }) + ' می‌توانید ادامه بدهید.'; } catch (e) {} }
    addMessage('ai', 'سهمیهٔ امروز استاد تمام شد.' + when + ' صنف شما ذخیره است. می‌توانید پلان‌ها را در صفحهٔ قیمت‌ها ببینید.',
      linkRow('دیدن پلان‌ها', function () { location.href = '/pricing'; }));
  }
  function reloadFromServer() {
    if (!cls) return;
    api('/api/class/' + cls.id).then(function (d) { if (d.__status === 200) openClass(d, { keepPlace: true }); });
  }
  var posPending = -1;
  function savePosSoon(i) {
    posPending = i; clearTimeout(posTimer);
    posTimer = setTimeout(function () { if (cls && posPending >= 0) { api('/api/class/' + cls.id + '/save', { pos: posPending }); posPending = -1; } }, 2500);
  }
  window.addEventListener('pagehide', function () {
    if (live && cls) {
      try { var tr = live.stop(); fetch('/api/class/' + cls.id + '/pause', { method: 'POST', credentials: 'same-origin', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pos: Math.max(0, stepCount() - 1), transcript: tr }) }); } catch (e) {}
      live = null; return;
    }
    if (cls && board && board.model) {
      try { fetch('/api/class/' + cls.id + '/save', { method: 'POST', credentials: 'same-origin', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pos: board.sceneIdx }) }); } catch (e) {}
    }
  });

  /* ───────── starting, opening, stopping a class ───────── */
  function setPrep(on, title, text) {
    var box = $('board-empty');
    var h = box.querySelector('h2'), p = box.querySelector('p');
    if (on) { h.textContent = title; p.innerHTML = text; box.classList.add('prep'); }
    else { h.textContent = 'به صنف خیام خوش آمدید'; p.innerHTML = 'هر چیزی که می‌خواهید بیاموزید را بگویید.<br>استاد درس را روی همین تخته می‌نویسد و می‌کشد و برایتان توضیح می‌دهد.'; box.classList.remove('prep'); }
  }
  function loginNotice() {
    addMessage('ai', 'برای شروع صنف باید وارد حساب‌تان شوید.', linkRow('ورود', function () { location.href = '/login'; }));
  }
  function startClass(text, classic) {
    if (liveWanted && !classic) { startLiveClass(text); return; }
    var start = document.querySelector('.class-start'); if (start) start.remove();
    addMessage('user', text);
    $('user-input').value = ''; $('user-input').style.height = 'auto';
    busy = true; $('send-btn').disabled = true; typing();
    setPrep(true, 'استاد درس را آماده می‌کند…', 'چند لحظه صبر کنید؛ مسیر درس چیده می‌شود.');
    showPane('board');
    api('/api/class/start', { topic: text }).then(function (d) {
      busy = false; $('send-btn').disabled = false; untyping();
      if (d.__status === 401) { setPrep(false); loginNotice(); return; }
      if (d.limited) { setPrep(false); limitNotice(d.reset_ts); return; }
      if (d.chat) { setPrep(false); addMessage('ai', d.chat); return; }
      if (d.__status !== 200 || d.error) { setPrep(false); addMessage('ai', d.error || 'شروع صنف ممکن نشد. دوباره امتحان کنید.'); return; }
      addMessage('ai', 'خوب است. به تخته نگاه کنید؛ من می‌نویسم و می‌کشم و توضیح می‌دهم. هر وقت خواستید «دست بالا» را بزنید یا همین‌جا بنویسید.');
      openClass(d, { fresh: true }); loadSaved();
    }).catch(function () { busy = false; $('send-btn').disabled = false; untyping(); setPrep(false); addMessage('ai', 'ارتباط برقرار نشد. اینترنت را بررسی کنید.'); });
  }
  function openClass(d, o) {
    o = o || {};
    cls = { id: d.id, title: d.title, outline: d.outline || [], step: d.step || 0, status: d.status, done: d.status === 'done', limited: false,
            lobby: liveWanted && !(d.outline || []).length && !(d.count > 0) };
    sampleOn = false; spoken = {}; stopAt = -1; closing = false; failures = 0; inflight = false;
    placeholder('سؤال یا جوابتان را بنویسید…');
    $('board-empty').hidden = true; $('board-controls').hidden = false; $('lesson-title').textContent = d.title;
    $('btn-stop').hidden = false;
    renderPath(); renderSaved();
    var lesson = { v: 1, digits: 'fa', title: d.title, scenes: d.beats || [], auto: liveWanted };
    liveAll = (d.beats || []).slice();
    ensureBoard().live = true; board.askDone = {}; board.atEnd = false; board.waitingAsk = -1; board.stack = []; board.holdAtEnd = false;
    if (!o.keepPlace) { $('transcript').textContent = ''; $('caption').textContent = ''; }
    return board.load(lesson).then(function () {
      board.live = true; buildScrub();
      if (o.fresh) { board.gotoScene(0, true); }
      else { board.gotoScene(Math.min(d.pos || 0, stepCount() - 1), false); setWaiting(false); }
      sidebarOpen = false; applySidebar();
      showPane('board');
    });
  }
  function openSaved(id) {
    if (busy) return;
    if (board) board.pause();
    api('/api/class/' + id).then(function (d) {
      if (d.__status !== 200) { toast('باز کردن صنف ممکن نشد.'); return; }
      $('messages').textContent = '';
      addMessage('ai', 'خوش آمدید. ' + (d.summary ? d.summary : 'از همان‌جا که مانده بودیم ادامه می‌دهیم.'),
        linkRow('ادامهٔ صنف', function (a) { a.parentNode.remove(); resumeClass(d); }));
      openClass(d, {}).then(function () { showPane('chat'); if (!isMobile()) showPane('board'); });
    });
  }
  function resumeClass(d) {
    if (busy) return;
    if (liveWanted) { resumeLive(d); return; }
    busy = true; typing(); setWaiting(true, 'استاد مرور را آماده می‌کند…'); showPane('board');
    var tries = 0;
    (function go() {
      api('/api/class/' + d.id + '/resume', {}).then(function (r) {
        if (r.busy && tries++ < 6) { setTimeout(go, 1800); return; }
        busy = false; untyping(); setWaiting(false);
        if (r.limited) { limitNotice(r.reset_ts); board.play(); return; }
        if (r.__status !== 200 || r.error) { addMessage('ai', r.error || 'ادامهٔ صنف ممکن نشد.'); return; }
        cls.status = 'active'; cls.done = false; board.holdAtEnd = false;
        if (r.resume_from != null && r.beats) {
          var from = r.resume_from;
          for (var k = from; k < from + 2; k++) spoken[k] = true;
          board.setTimeline(from, r.beats.slice(from));
          board.gotoScene(from, true);
        } else { board.play(); }
        loadSaved();
      }).catch(function () { busy = false; untyping(); setWaiting(false); toast('ارتباط برقرار نشد.'); });
    })();
  }
  window.stopClass = function () {
    if (!cls) { toast('صنفی در جریان نیست.'); return; }
    saveAndClose(false);
  };
  function saveAndClose(auto) {
    if (!cls || !board) return;
    if (live) { finishLive(); return; }
    closing = true; stopAt = -1;
    board.pause(); setHand(false);
    var id = cls.id;
    typing();
    api('/api/class/' + id + '/pause', { pos: board.sceneIdx }).then(function (d) {
      untyping(); closing = false;
      if (d.__status !== 200) { closing = false; toast('ذخیرهٔ صنف ممکن نشد.'); return; }
      cls.status = 'paused';
      var txt = 'صنف ذخیره شد. هر وقت خواستید از همین‌جا ادامه می‌دهیم.' + (d.summary ? '\n\n' + d.summary : '');
      addMessage('ai', txt, linkRow('صنف تازه', function () { newClass(); }));
      loadSaved();
    }).catch(function () { untyping(); closing = false; toast('ذخیرهٔ صنف ممکن نشد.'); });
  }

  /* ───────── the student speaks ───────── */
  window.sendMessage = function () {
    var input = $('user-input'); var text = input.value.trim();
    if (!text || busy) return;
    if (text.length > 600) { toast('پیام خیلی طولانی است (حداکثر ۶۰۰ حرف).'); return; }
    if (!cls) { startClass(text); return; }
    if (live) {
      addMessage('user', text); input.value = ''; input.style.height = 'auto';
      if (!live.sendText(text)) toast('هنوز به استاد وصل نیستید.');
      return;
    }
    document.querySelectorAll('.skiprow').forEach(function (e) { e.remove(); });
    board.holdAtEnd = false; closing = false;
    addMessage('user', text);
    input.value = ''; input.style.height = 'auto';
    board.pause(); setHand(false); setWaiting(true, 'استاد جواب را آماده می‌کند…');
    placeholder('سؤال یا جوابتان را بنویسید…');
    busy = true; $('send-btn').disabled = true; typing();
    if (isMobile()) showPane('board');
    ask(text, 0);
  };
  function ask(text, tries) {
    var id = cls.id, pos = board.sceneIdx;
    api('/api/class/' + id + '/ask', { text: text, pos: pos }).then(function (d) {
      if (!cls || cls.id !== id) return;
      if (d.busy && tries < 8) { setTimeout(function () { ask(text, tries + 1); }, 1800); return; }
      busy = false; $('send-btn').disabled = false; untyping(); setWaiting(false);
      if (d.limited) { limitNotice(d.reset_ts); return; }
      if (d.__status !== 200 || d.error) {
        addMessage('ai', d.error || 'جواب آماده نشد. دوباره امتحان کنید.', linkRow('ادامهٔ درس', function (a) { a.parentNode.remove(); board.play(); }));
        return;
      }
      var from = d.from;
      Object.keys(spoken).forEach(function (k) { if (+k >= from) delete spoken[k]; });
      for (var k = from; k < from + d.beats.length; k++) spoken[k] = true;
      if (Array.isArray(d.outline) && d.outline.length > 1) cls.outline = d.outline;
      if (typeof d.step === 'number') { cls.step = d.step; renderPath(); }
      if (d.action === 'done') cls.done = true;
      if (d.action === 'stop') stopAt = from + d.beats.length - 1;
      board.setTimeline(from, d.beats);
      board.gotoScene(from, true);
    }).catch(function () {
      busy = false; $('send-btn').disabled = false; untyping(); setWaiting(false);
      addMessage('ai', 'ارتباط برقرار نشد.', linkRow('ادامهٔ درس', function (a) { a.parentNode.remove(); board.play(); }));
    });
  }

  /* ───────── the built-in sample (no server) ───────── */
  window.loadSample = function () {
    fetch('/static/lessons/sample.json').then(function (r) { return r.json(); }).then(function (lesson) {
      cls = null; sampleOn = true; spoken = {};
      $('messages').textContent = '';
      addMessage('user', 'می‌خواهم کسرها را از اول یاد بگیرم.');
      addMessage('ai', 'این یک درس نمونه است. برای یک صنف واقعی، بنویسید چه می‌خواهید یاد بگیرید.');
      renderPath(); $('board-empty').hidden = true; $('board-controls').hidden = false; $('btn-stop').hidden = true;
      $('lesson-title').textContent = lesson.title || 'درس نمونه';
      $('transcript').textContent = ''; $('caption').textContent = '';
      showPane('board'); sidebarOpen = false; applySidebar();
      ensureBoard().live = false;
      board.load(lesson).then(function () { board.live = false; buildScrub(); board.gotoScene(0, true); });
    }).catch(function () { toast('بارگیری نمونه ممکن نشد.'); });
  };
  window.newClass = function () {
    if (live) { try { live.stop(); } catch (e) {} live = null; $('voice-record-btn').classList.remove('live', 'muted'); }
    delete $('classroom').dataset.live;
    if (board) { board.pause(); board.destroy(); board = null; window.__board = null; }
    if (cls && posPending >= 0) { api('/api/class/' + cls.id + '/save', { pos: posPending }); posPending = -1; }
    cls = null; sampleOn = false; stopAt = -1; closing = false; inflight = false; busy = false; handUp = false; spoken = {};
    clearTimeout(retryTimer); clearTimeout(posTimer);
    $('send-btn').disabled = false; setHand(false); setPrep(false);
    renderPath(); renderSaved();
    $('board-empty').hidden = false; $('board-controls').hidden = true; $('caption').textContent = ''; $('caption').classList.remove('waiting');
    $('lesson-title').textContent = 'صنف درس'; placeholder('بگویید چه می‌خواهید بیاموزید…');
    $('scrub').textContent = '';
    showStart(); showPane('chat'); if (!isMobile()) showPane('board');
    if (isMobile()) { sidebarOpen = false; applySidebar(); }
  };

  /* ───────── the live voice class ───────── */
  function liveHandlers() {
    return {
      preview: function (from, beat) {
        liveAll.length = Math.min(liveAll.length, from); liveAll[from] = beat;
        var m = KB.buildModel({ v: 1, digits: 'fa', auto: true, scenes: liveAll.slice(0, from + 1) }, board && board.profile);
        var S = m.scenes[from] || {}, st = m.state;
        return { board: st.board, text_lines_left: st.text_lines_left, picture_slots_free: st.picture_slots_free,
                 wiped: !!S.wiped && !beat.clear, wiped_pictures: !!S.wipedPictures, dropped: S.dropped };
      },
      onBeat: function (from, beat) {
        if (!board || !board.model) return;
        leaveLobby(true);
        board.setTimeline(from, [beat]);
        if (board.sceneIdx >= from || board.model.scenes.length === 1) board.gotoScene(from, true);
      },
      onProgress: function (d) {
        if (!cls) return;
        if (Array.isArray(d.outline) && d.outline.length) cls.outline = d.outline;
        if (typeof d.step === 'number') cls.step = d.step;
        if (d.title && !(cls.lobby && !d.started)) { cls.title = d.title; $('lesson-title').textContent = d.title; }
        if (d.started || (cls.outline && cls.outline.length > 1)) leaveLobby();
        renderPath();
      },
      onUserText: function (t) { addMessage('user', t); },
      onAiText: function (t) { addMessage('ai', t); },          // the conversation is the only text; the board has no captions
      onState: function (st) {
        var b = $('voice-record-btn');
        b.classList.toggle('live', st !== 'idle');
        b.classList.toggle('talking', st === 'listening');
      },
      onEnd: function () { if (live && !closing) finishLive(); },
      onQuota: function (kind, minutes) {
        if (kind === 'warn') toast('از سهمیهٔ گفتگوی صوتی حدود ' + faNum(minutes) + ' دقیقه باقی مانده است.');
        else { toast('سهمیهٔ صوتی تمام شد؛ صنف ذخیره می‌شود.'); if (live && !closing) finishLive(); }
      },
      onError: function (msg) { toast(msg); }
    };
  }
  function connectLive(d, resumed) {
    if (cls) cls.voiceClass = true;
    var h = liveHandlers(); h.classId = d.id; h.resumed = !!resumed; h.firstText = resumed ? '' : (d.request || '');
    live = new TutorLive.Session(h);
    setPrep(true, 'در حال اتصال به استاد…', 'اگر مرورگر اجازهٔ میکروفون را پرسید، «اجازه» را بزنید.');
    return live.start().then(function () {
      setPrep(false);
      $('board-empty').hidden = !cls.lobby; $('board-controls').hidden = !!cls.lobby; $('lesson-title').textContent = cls.lobby ? 'گفتگو با استاد' : cls.title;
      $('classroom').dataset.live = '1';
      $('voice-record-btn').classList.add('live');
      placeholder('هر وقت خواستید صحبت کنید، یا اینجا بنویسید…');
      setWaiting(true, 'استاد می‌آید…');
      showPane('board');
    }).catch(function (err) {
      if (global_console()) console.warn('live voice failed', err);
      var sess = live; live = null; try { sess.stop(); } catch (e) {}
      $('voice-record-btn').classList.remove('live', 'muted');
      setPrep(false); setWaiting(false);
      var denied = err && /Permission|NotAllowed|denied/i.test(String(err.name || err.message || err));
      addMessage('ai', denied ? 'برای صنف صوتی به میکروفون نیاز است. اجازهٔ میکروفون را بدهید و دوباره امتحان کنید.' : 'اتصال صوتی برقرار نشد.',
        linkRow('ادامه با نوشتن', function (a) {
          a.parentNode.remove();
          var req = d.request || d.title || '';
          api('/api/class/' + d.id, null, 'DELETE').then(function () { if (cls && cls.id === d.id) { cls = null; } loadSaved(); if (req) startClass(req, true); });
        }));
      return false;
    });
  }
  // the conversation became a class (the teacher called start_class, or began drawing): show the board
  function leaveLobby(drawn) {
    if (!cls || !cls.lobby) return;
    cls.lobby = false;
    $('board-empty').hidden = true; $('board-controls').hidden = false;
    if (drawn && cls.title === 'گفتگو با استاد') $('lesson-title').textContent = 'گفتگو با استاد';
    renderPath();
  }
  function global_console() { return typeof console !== 'undefined'; }
  function startLiveClass(text) {
    var start = document.querySelector('.class-start'); if (start) start.remove();
    if (text) addMessage('user', text);
    $('user-input').value = ''; $('user-input').style.height = 'auto';
    busy = true; $('send-btn').disabled = true;
    setPrep(true, 'در حال اتصال به استاد…', 'اگر مرورگر اجازهٔ میکروفون را پرسید، «اجازه» را بزنید.');
    showPane('board');
    api('/api/class/voice/new', { topic: text }).then(function (d) {
      busy = false; $('send-btn').disabled = false;
      if (d.__status === 401) { setPrep(false); loginNotice(); return; }
      if (d.__status !== 200 || d.error) { setPrep(false); addMessage('ai', d.error || 'شروع صنف ممکن نشد.'); return; }
      return openClass(d, {}).then(function () { board.live = true; return connectLive(d, false); }).then(function () { loadSaved(); });
    }).catch(function () { busy = false; $('send-btn').disabled = false; setPrep(false); addMessage('ai', 'ارتباط برقرار نشد. اینترنت را بررسی کنید.'); });
  }
  function resumeLive(d) {
    if (busy) return;
    var n = stepCount();
    if (n) { board.setTimeline(n, [{ say: '', do: [] }]); board.gotoScene(n, false); }   // the board as it was left
    cls.status = 'active'; cls.done = false;
    connectLive(d, true).then(function () { loadSaved(); });
  }
  function finishLive() {
    if (!live || !cls || closing) return;
    closing = true;
    var id = cls.id, tr = live.stop(); live = null;
    $('voice-record-btn').classList.remove('live', 'muted', 'talking'); delete $('classroom').dataset.live;
    typing();
    api('/api/class/' + id + '/pause', { pos: Math.max(0, stepCount() - 1), transcript: tr }).then(function (d) {
      untyping(); closing = false; setWaiting(false);
      if (d.__status !== 200) { toast('ذخیرهٔ صنف ممکن نشد.'); return; }
      if (d.deleted) { addMessage('ai', 'هر وقت خواستید دوباره بیایید و بگویید چه می‌خواهید بیاموزید.', linkRow('شروع تازه', function () { newClass(); })); cls = null; loadSaved(); return; }
      if (cls) cls.status = 'paused';
      addMessage('ai', 'صنف ذخیره شد. هر وقت خواستید از همین‌جا ادامه می‌دهیم.' + (d.summary ? '\n\n' + d.summary : ''), linkRow('صنف تازه', function () { newClass(); }));
      loadSaved();
    }).catch(function () { untyping(); closing = false; setWaiting(false); toast('ذخیرهٔ صنف ممکن نشد.'); });
  }

  /* ───────── init ───────── */
  var input = $('user-input');
  input.addEventListener('input', function () { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 140) + 'px'; });
  input.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); } });

  applySidebar(); showStart(); renderPath(); renderSaved();
  if (isMobile()) showPane('chat');

  fetch('/api/me').then(function (r) { return r.json(); }).then(function (d) {
    if (!d || !d.logged_in) return;
    $('auth-buttons').style.display = 'none';
    var menu = $('user-menu'); menu.style.display = 'flex';
    $('nav-username').textContent = d.username;
    $('nav-avatar').textContent = (d.username || '').trim().charAt(0).toUpperCase();
    if (d.is_admin) { var a = document.createElement('a'); a.href = '/admin'; a.className = 'side-btn gold'; a.textContent = 'مدیریت'; menu.appendChild(a); }
    loadSaved();
  }).catch(function () {});
})();
