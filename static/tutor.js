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
  /* Status popups: one small pill per thing that is happening. A "busy" pill (spinner) stays until that thing is finished, then turns into
     a check mark ("ok") and leaves by itself; "warn" / "fail" stay a few seconds; "info" is a short note. delay = show a busy pill only if it
     lasts longer than that (so quick things do not flash); max = give up showing it after this long (never stuck forever). */
  var Stat = (function () {
    var stack = $('status-stack'), items = {}, seq = 0;
    function hide(key) {
      var it = items[key]; if (!it) return;
      clearTimeout(it.t); clearTimeout(it.d); delete items[key];
      if (!it.el) return;
      it.el.classList.remove('in'); it.el.classList.add('out');
      setTimeout(function () { if (it.el && it.el.parentNode) it.el.parentNode.removeChild(it.el); }, 380);
    }
    function paint(key, text, kind, ms) {
      var it = items[key]; if (!it) return;
      if (!it.el) {
        var el = document.createElement('div'); el.className = 'status';
        el.innerHTML = '<i class="st-ic" aria-hidden="true"></i><span class="st-tx"></span>';
        stack.appendChild(el); it.el = el;
        var kids = stack.children; while (kids.length > 4) { var first = kids[0]; for (var k in items) if (items[k].el === first) { delete items[k]; } first.parentNode.removeChild(first); }
        requestAnimationFrame(function () { requestAnimationFrame(function () { el.classList.add('in'); }); });
      }
      it.el.className = 'status in ' + kind;
      it.el.querySelector('.st-tx').textContent = text;
      clearTimeout(it.t); if (ms) it.t = setTimeout(function () { hide(key); }, ms);
    }
    function set(key, text, kind, ms, delay) {
      var it = items[key];
      if (!it) it = items[key] = { el: null, t: 0, d: 0 };
      clearTimeout(it.d);
      if (delay && !it.el) it.d = setTimeout(function () { paint(key, text, kind, ms); }, delay); else paint(key, text, kind, ms);
    }
    return {
      busy: function (key, text, delay, max) { set(key, text, 'busy', max || 120000, delay || 0); },
      ok: function (key, text, ms) { var it = items[key]; if (it && !it.el && it.d) { hide(key); return; } set(key, text, 'ok', ms || 2400, 0); },     // a quick thing that never showed leaves no trace
      warn: function (key, text, ms) { set(key, text, 'warn', ms || 6000, 0); },
      fail: function (key, text, ms) { set(key, text, 'fail', ms || 6000, 0); },
      info: function (text, ms) { set('i' + (++seq), text, 'info', ms || 3600, 0); },
      clear: hide,
      has: function (key) { return !!(items[key] && items[key].el); }
    };
  })();
  function toast(text) { Stat.info(text, 3600); }
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
    if (name === 'board' && board) setTimeout(function () { if (board) board.onResize(); }, 60);
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
    wireStudent(board);
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
    cap.classList.toggle('waiting', !!on); cap.textContent = '';
    if (on) Stat.busy('wait', text || 'استاد در حال نوشتن است…', 350, 60000); else Stat.clear('wait');
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
  function setPrep(on, title, text, quiet) {
    if (!quiet) { if (on) Stat.busy('prep', title, 0, 90000); else Stat.clear('prep'); }
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
    api('/api/class/' + d.id + '/materials').then(function (r) { if (r && r.ok) feedRegions(r.items); }).catch(function () {});
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
    Stat.busy('open', 'در حال باز کردن صنف…', 400, 30000);
    api('/api/class/' + id).then(function (d) {
      Stat.clear('open');
      if (d.__status !== 200) { Stat.fail('open', 'باز کردن صنف ممکن نشد.'); return; }
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
    typing(); Stat.busy('save', 'صنف ذخیره می‌شود…', 0, 90000);
    api('/api/class/' + id + '/pause', { pos: board.sceneIdx }).then(function (d) {
      untyping(); closing = false;
      if (d.__status !== 200) { closing = false; Stat.fail('save', 'ذخیرهٔ صنف ممکن نشد.'); return; }
      Stat.ok('save', 'صنف ذخیره شد ✓', 2600);
      cls.status = 'paused';
      var txt = 'صنف ذخیره شد. هر وقت خواستید از همین‌جا ادامه می‌دهیم.' + (d.summary ? '\n\n' + d.summary : '');
      addMessage('ai', txt, linkRow('صنف تازه', function () { newClass(); }));
      loadSaved();
    }).catch(function () { untyping(); closing = false; Stat.fail('save', 'ذخیرهٔ صنف ممکن نشد.'); });
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
    showStuTools(false); delete $('classroom').dataset.live;
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
                 wiped: !!S.wiped && !beat.clear && !(st.page && st.page.length), wiped_pictures: !!S.wipedPictures, dropped: S.dropped, page: st.page };
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
      onUserText: function (t) { stuSpoke(); addMessage('user', t); },
      onAiText: function (t) { addMessage('ai', t); },          // the conversation is the only text; the board has no captions
      onState: function (st) {
        var b = $('voice-record-btn');
        b.classList.toggle('live', st !== 'idle');
        b.classList.toggle('talking', st === 'listening');
        if (st === 'listening' || st === 'speaking') stuSpoke(true);
        if (st === 'thinking') Stat.busy('think', 'استاد فکر می‌کند…', 900, 40000);
        else if (st !== 'connected') { Stat.clear('think'); if (st === 'speaking') Stat.clear('wait'); }
      },
      onStage: function (s) {
        if (s === 'mic') Stat.busy('voice', 'اجازهٔ میکروفون را بدهید…', 0, 90000);
        else if (s === 'session') Stat.busy('voice', 'در حال اتصال به استاد…', 0, 60000);
        else if (s === 'connect') Stat.busy('voice', 'در حال برقراری ارتباط صوتی…', 0, 60000);
      },
      onWork: function (kind, on) {
        var txt = { look: 'استاد تصویر را دقیق نگاه می‌کند…', poem: 'استاد شعر را در مجموعه می‌جوید…', start: 'استاد درس را آماده می‌کند…' }[kind];
        if (!txt) return;
        if (on) Stat.busy('work-' + kind, txt, 500, 40000); else Stat.clear('work-' + kind);
      },
      onNet: function (up) {
        if (!up) Stat.busy('net', 'اتصال ضعیف است؛ دوباره وصل می‌شویم…', 1500, 60000);
        else if (Stat.has('net')) Stat.ok('net', 'اتصال برگشت ✓', 2000); else Stat.clear('net');
      },
      onEnd: function () { if (live && !closing) finishLive(); },
      onQuota: function (kind, minutes) {
        if (kind === 'warn') Stat.warn('quota', 'از سهمیهٔ گفتگوی صوتی حدود ' + faNum(minutes) + ' دقیقه باقی مانده است.', 9000);
        else { Stat.fail('quota', 'سهمیهٔ صوتی تمام شد؛ صنف ذخیره می‌شود.', 8000); if (live && !closing) finishLive(); }
      },
      onError: function (msg) { Stat.fail('err', msg, 6000); }
    };
  }
  function connectLive(d, resumed, pic) {
    if (cls) cls.voiceClass = true;
    var h = liveHandlers(); h.classId = d.id; h.resumed = !!resumed; h.firstText = resumed ? '' : (d.request || ''); if (pic) h.firstPicture = pic;
    live = new TutorLive.Session(h);
    setPrep(true, 'در حال اتصال به استاد…', 'اگر مرورگر اجازهٔ میکروفون را پرسید، «اجازه» را بزنید.', true);
    Stat.busy('voice', 'در حال اتصال به استاد…', 0, 60000);
    return live.start().then(function () {
      setPrep(false, '', '', true);
      Stat.ok('voice', 'به استاد وصل شدید ✓', 2600);
      showStuTools(true);
      Stat.busy('wait', 'استاد می‌آید…', 0, 30000);
      $('board-empty').hidden = !cls.lobby; $('board-controls').hidden = !!cls.lobby; $('lesson-title').textContent = cls.lobby ? 'گفتگو با استاد' : cls.title;
      $('classroom').dataset.live = '1';
      $('voice-record-btn').classList.add('live');
      placeholder('هر وقت خواستید صحبت کنید، یا اینجا بنویسید…');
      showPane('board');
    }).catch(function (err) {
      if (global_console()) console.warn('live voice failed', err);
      var sess = live; live = null; try { sess.stop(); } catch (e) {}
      $('voice-record-btn').classList.remove('live', 'muted');
      setPrep(false, '', '', true); setWaiting(false); showStuTools(false);
      var denied = err && /Permission|NotAllowed|denied/i.test(String(err.name || err.message || err));
      Stat.fail('voice', denied ? 'میکروفون اجازه نگرفت. اجازه بدهید و دوباره امتحان کنید.' : 'اتصال صوتی برقرار نشد.', 7000);
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
    setPrep(true, 'در حال اتصال به استاد…', 'اگر مرورگر اجازهٔ میکروفون را پرسید، «اجازه» را بزنید.', true);
    Stat.busy('voice', 'در حال آماده‌کردن صنف…', 0, 60000);
    showPane('board');
    api('/api/class/voice/new', { topic: text }).then(function (d) {
      busy = false; $('send-btn').disabled = false;
      if (d.__status === 401) { setPrep(false, '', '', true); Stat.fail('voice', 'برای شروع باید وارد حساب‌تان شوید.'); loginNotice(); return; }
      if (d.__status !== 200 || d.error) { setPrep(false, '', '', true); Stat.fail('voice', d.error || 'شروع صنف ممکن نشد.'); addMessage('ai', d.error || 'شروع صنف ممکن نشد.'); return; }
      return openClass(d, {}).then(function () { board.live = true; return connectLive(d, false); }).then(function () { loadSaved(); });
    }).catch(function () { busy = false; $('send-btn').disabled = false; setPrep(false, '', '', true); Stat.fail('voice', 'ارتباط برقرار نشد. اینترنت را بررسی کنید.'); addMessage('ai', 'ارتباط برقرار نشد. اینترنت را بررسی کنید.'); });
  }
  function resumeLive(d) {
    if (busy) return;
    var n = stepCount();
    if (n) { board.setTimeline(n, [{ say: '', do: [] }]); board.gotoScene(n, false); }   // the board as it was left
    cls.status = 'active'; cls.done = false;
    Stat.busy('voice', 'در حال برگرداندن صنف…', 0, 60000);
    connectLive(d, true).then(function () { loadSaved(); });
  }
  function finishLive() {
    if (!live || !cls || closing) return;
    closing = true;
    var id = cls.id, tr = live.stop(); live = null; showStuTools(false);
    Stat.clear('think'); Stat.clear('wait'); Stat.busy('save', 'صنف ذخیره می‌شود…', 0, 90000);
    $('voice-record-btn').classList.remove('live', 'muted', 'talking'); delete $('classroom').dataset.live;
    typing();
    api('/api/class/' + id + '/pause', { pos: Math.max(0, stepCount() - 1), transcript: tr }).then(function (d) {
      untyping(); closing = false; setWaiting(false);
      if (d.__status !== 200) { Stat.fail('save', 'ذخیرهٔ صنف ممکن نشد.'); return; }
      Stat.ok('save', 'صنف ذخیره شد ✓', 2600);
      if (d.deleted) { addMessage('ai', 'هر وقت خواستید دوباره بیایید و بگویید چه می‌خواهید بیاموزید.', linkRow('شروع تازه', function () { newClass(); })); cls = null; loadSaved(); return; }
      if (cls) cls.status = 'paused';
      addMessage('ai', 'صنف ذخیره شد. هر وقت خواستید از همین‌جا ادامه می‌دهیم.' + (d.summary ? '\n\n' + d.summary : ''), linkRow('صنف تازه', function () { newClass(); }));
      loadSaved();
    }).catch(function () { untyping(); closing = false; setWaiting(false); Stat.fail('save', 'ذخیرهٔ صنف ممکن نشد.'); });
  }

  /* ───────── pictures and document pages ───────── */
  function toJpeg(file, maxSide, q) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(file), im = new Image();
      im.onload = function () {
        var k = Math.min(1, maxSide / Math.max(im.naturalWidth, im.naturalHeight, 1)), c = document.createElement('canvas');
        c.width = Math.max(1, Math.round(im.naturalWidth * k)); c.height = Math.max(1, Math.round(im.naturalHeight * k));
        var x = c.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0, 0, c.width, c.height); x.drawImage(im, 0, 0, c.width, c.height);
        URL.revokeObjectURL(url);
        c.toBlob(function (b) { if (b) resolve(b); else reject(new Error('jpeg')); }, 'image/jpeg', q);
      };
      im.onerror = function () { URL.revokeObjectURL(url); reject(new Error('decode')); };
      im.src = url;
    });
  }
  function blobUrl(blob) {
    return new Promise(function (resolve, reject) { var r = new FileReader(); r.onload = function () { resolve(r.result); }; r.onerror = function () { reject(r.error); }; r.readAsDataURL(blob); });
  }
  function regionsNote(info) {
    var L = [];
    if (info.summary) L.push('What it shows: ' + info.summary);
    if (info.regions && info.regions.length) {
      L.push('Parts you can point at (use these ids as "region"):');
      info.regions.slice(0, 18).forEach(function (r) { L.push('  ' + r.id + ': ' + r.label + (r.what ? ' - ' + r.what : '') + (r.text ? ' ; "' + String(r.text).slice(0, 90) + '"' : '')); });
    }
    if (info.text) L.push('Text in it: ' + String(info.text).slice(0, 700));
    return L.join('\n');
  }
  function pictureNote(items, pdf, info, words) {
    var first = items[0], last = items[items.length - 1], L = [];
    L.push('[تصویر از شاگرد] The student has just sent ' + (pdf ? 'a document' : 'a picture') + ' ("' + first.title + '").');
    if (pdf) L.push('It has ' + (first.pages || items.length) + ' pages' + (items.length < (first.pages || 0) ? ' (only the first ' + items.length + ' can be used)' : '') + '. The pages are the keys ' + first.key + (items.length > 1 ? ' to ' + last.key : '') + ' in order (page 1 = ' + first.key + '). The image attached is page 1 (' + first.key + ').');
    else L.push('Its key is ' + first.key + '. The image is attached.');
    L.push(info ? regionsNote(info) : 'You have not looked at it closely yet: call the tool "look" with its key before you point at anything.');
    if (words) L.push('The student wrote with it: «' + words + '»');
    L.push('Put the picture on the board ONCE (board op "image", image "' + first.key + '"); it then stays there for the whole talk, so never show it again.');
    L.push(words
      ? 'Do what the student asked, using the picture (see PICTURES: the tasks).'
      : 'The student has not said what they want. Do not start explaining yet. Say in one short sentence what you see, then ask ONE short question in Dari offering the choices that fit this picture (for example: solve it with you, learn the lesson in it, translate it, check their answer, or something else), and wait for the answer.');
    return L.join('\n');
  }
  function addPictureMessage(items, pdf) {
    var first = items[0], img = document.createElement('img');
    img.className = 'chat-thumb'; img.src = first.url; img.alt = first.title;
    addMessage('user', pdf ? 'فایل «' + first.title + '»' + (first.pages ? ' (' + faNum(first.pages) + ' صفحه)' : '') : 'یک تصویر فرستادم.', img);
  }
  window.pickFile = function () {
    if (!liveWanted) { Stat.warn('pic', 'فرستادن عکس در صنف صوتی فعال است.', 4500); return; }
    if (busy || closing) { Stat.warn('pic', 'یک لحظه صبر کنید.', 4500); return; }
    if (cls && cls.id && !live) { Stat.warn('pic', 'برای فرستادن عکس، اول صنف را با صدا ادامه بدهید.', 4500); return; }
    $('file-input').click();
  };
  function sendFile(file) {
    if (!file) return Promise.resolve();
    if (busy || closing) { Stat.warn('pic', 'یک لحظه صبر کنید.', 4500); return Promise.resolve(); }
    if (!liveWanted) { Stat.warn('pic', 'فرستادن عکس در صنف صوتی فعال است.', 4500); return Promise.resolve(); }
    if (cls && cls.id && !live) { Stat.warn('pic', 'برای فرستادن عکس، اول صنف را با صدا ادامه بدهید.', 4500); return Promise.resolve(); }
    var pdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name || '');
    if (!pdf && !/^image\//.test(file.type || '')) { Stat.warn('pic', 'فقط عکس یا فایل PDF بفرستید.', 4500); return Promise.resolve(); }
    if (file.size > 40 * 1024 * 1024) { Stat.warn('pic', 'فایل خیلی بزرگ است (حداکثر ۱۲ مگابایت, 4500).', 4500); return Promise.resolve(); }
    busy = true; $('send-btn').disabled = true;
    var fresh = !(cls && cls.id), d = null, items = null, info = null, small = null;
    var words = ($('user-input').value || '').trim().slice(0, 400);
    if (words) { $('user-input').value = ''; $('user-input').style.height = 'auto'; }
    Stat.busy('pic', pdf ? 'در حال فرستادن فایل…' : 'در حال فرستادن تصویر…', 0, 90000);
    if (fresh) { var st = document.querySelector('.class-start'); if (st) st.remove(); setPrep(true, 'استاد تصویر را می‌بیند…', 'چند لحظه صبر کنید.', true); showPane('board'); }
    var p = fresh ? api('/api/class/voice/new', { topic: '' }).then(function (r) {
      if (r.__status === 401) { loginNotice(); throw { quiet: true }; }
      if (r.__status !== 200 || r.error) throw new Error(r.error || 'شروع صنف ممکن نشد.');
      d = r; return openClass(r, {}).then(function () { board.live = true; });
    }) : Promise.resolve().then(function () { d = { id: cls.id }; });
    return p.then(function () {
      return (pdf ? Promise.resolve(file) : toJpeg(file, 1600, .88).catch(function () { return file; }));
    }).then(function (blob) {
      var fd = new FormData(); fd.append('file', blob, pdf ? (file.name || 'file.pdf') : String(file.name || 'photo').replace(/\.\w+$/, '') + '.jpg');
      return fetch('/api/class/' + d.id + '/upload', { method: 'POST', body: fd, credentials: 'same-origin' });
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (up) {
        if (!r.ok || !up.ok || !(up.items || []).length) throw new Error(up.error || 'بارگذاری ممکن نشد.');
        items = up.items; addPictureMessage(items, pdf); feedRegions(items);
      });
    }).then(function () {
      Stat.busy('pic', pdf ? 'استاد صفحه‌ها را می‌خواند…' : 'استاد تصویر را بررسی می‌کند…', 0, 90000);
      var tm = new Promise(function (res) { setTimeout(function () { res(null); }, 30000); });
      return Promise.race([api('/api/class/' + d.id + '/look', { image: items[0].key }), tm]).then(function (lk) { if (lk && lk.ok) { info = lk; feedRegions(null, lk); } }).catch(function () {});
    }).then(function () {
      return (pdf ? fetch(items[0].url, { credentials: 'same-origin' }).then(function (r) { return r.blob(); }) : Promise.resolve(file))
        .then(function (b) { return toJpeg(b, 1280, .82); }).then(blobUrl).then(function (u) { small = u; }).catch(function () { small = null; });
    }).then(function () {
      var pic = { note: pictureNote(items, pdf, info, words), dataUrl: small, title: items[0].title };
      busy = false; $('send-btn').disabled = false;
      if (live) {
        setWaiting(false);
        if (!live.sendPicture(pic)) { Stat.fail('pic', 'هنوز به استاد وصل نیستید.'); return; }
        Stat.ok('pic', (pdf ? 'فایل' : 'تصویر') + ' به استاد رسید ✓', 2400); Stat.busy('think', 'استاد ' + (pdf ? 'فایل' : 'تصویر') + ' را می‌بیند…', 0, 40000);
        return;
      }
      Stat.busy('pic', 'در حال اتصال به استاد…', 0, 90000);
      return connectLive(d, false, pic).then(function (r) {
        if (r === false) Stat.clear('pic');
        else { Stat.ok('pic', (pdf ? 'فایل' : 'تصویر') + ' به استاد رسید ✓', 2400); Stat.busy('think', 'استاد ' + (pdf ? 'فایل' : 'تصویر') + ' را می‌بیند…', 0, 40000); }
        loadSaved();
      });
    }).catch(function (err) {
      busy = false; $('send-btn').disabled = false; setWaiting(false);
      if (words && !$('user-input').value) $('user-input').value = words;
      if (fresh) {
        setPrep(false, '', '', true);
        if (d && d.id) api('/api/class/' + d.id, null, 'DELETE').then(function () { if (cls && cls.id === d.id) newClass(); loadSaved(); });   // nothing came of it: no empty class is left behind
      }
      if (err && err.quiet) { Stat.clear('pic'); return; }
      Stat.fail('pic', (err && err.message) || 'فرستادن تصویر ممکن نشد.', 7000);
    });
  }
  (function pictureInit() {
    var inp = $('file-input'), room = $('classroom'), ta = $('user-input');
    if (liveWanted) { $('attach-btn').hidden = false; document.body.classList.add('live-on'); }
    inp.addEventListener('change', function () { var f = inp.files && inp.files[0]; inp.value = ''; if (f) sendFile(f); });
    var hasFiles = function (e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0; };
    ['dragenter', 'dragover'].forEach(function (n) { room.addEventListener(n, function (e) { if (hasFiles(e)) { e.preventDefault(); room.classList.add('dropping'); } }); });
    ['dragleave', 'drop'].forEach(function (n) { room.addEventListener(n, function () { room.classList.remove('dropping'); }); });
    room.addEventListener('drop', function (e) { if (!hasFiles(e)) return; e.preventDefault(); var f = e.dataTransfer.files && e.dataTransfer.files[0]; if (f) sendFile(f); });
    ta.addEventListener('paste', function (e) { var fs = e.clipboardData && e.clipboardData.files; if (fs && fs.length) { e.preventDefault(); sendFile(fs[0]); } });
  })();


  /* ───────── the student on the board: pointing, writing, typing ───────── */
  var stuTimer = 0;
  function showStuTools(on) {
    var bar = $('stu-tools'); if (!bar) return;
    bar.hidden = !on;
    if (!on) { clearTimeout(stuTimer); $('stu-send').hidden = true; Stat.clear('ask'); Stat.clear('turn'); if (board && board.setTool) { board.setTool(null); board.studentClear(); } }
  }
  function stuSpoke(quiet) { clearTimeout(stuTimer); Stat.clear('ask'); }       // the student (or the teacher) is talking: the "say what you want" hint is over
  function descText(d) {
    var L = [];
    if (d.lines && d.lines.length) { L.push('Text lines under it (element id: text):'); d.lines.slice(0, 8).forEach(function (l) { L.push('  ' + l.id + ': «' + l.s + '»'); }); }
    if (d.formulas && d.formulas.length) { L.push('Formulas under it:'); d.formulas.slice(0, 4).forEach(function (f) { L.push('  ' + f.id + ': ' + f.m); }); }
    (d.pictures || []).forEach(function (p) {
      L.push('Picture ' + p.key + ' (its element id ' + p.id + '): it covers this area of the picture [' + p.area.join(', ') + '] (thousandths: left, top, width, height)' +
        (p.regions.length ? '; named parts under it: ' + p.regions.map(function (r) { return r.id + ' (' + r.label + (r.text ? ': «' + String(r.text).slice(0, 80) + '»' : '') + ')'; }).join(', ')
          : '; no named part is there, so point with "area" or call look with find'));
    });
    if (d.things && d.things.length) L.push('Other things there: ' + d.things.slice(0, 5).map(function (x) { return x.id + ' (' + x.kind + (x.label ? ' ' + x.label : '') + ')'; }).join(', '));
    if (!L.length) L.push('Nothing you drew is under it (empty board).');
    return L.join('\n');
  }
  function padRect(r, pad) { return [Math.max(0, r[0] - pad), Math.max(0, r[1] - pad), r[2] + 2 * pad, r[3] + 2 * pad]; }
  function thumbMessage(text, dataUrl) {
    var img = document.createElement('img'); img.className = 'chat-thumb'; img.src = dataUrl; img.alt = text;
    addMessage('user', text, img);
  }
  function onStuSelect(info) {
    if (!live || !live.active) { toast('اول به استاد وصل شوید.'); return; }
    var whole = board.snapshot({ targetW: 1000 }), crop = board.snapshot({ rect: padRect(info.rect, 36), targetW: 640 });
    var note = '[انتخاب شاگرد] The student pointed at a part of the board with their gold pointer (outlined in yellow in the first picture; the second picture is a close-up of it).\n' + descText(info.desc) +
      '\nThis is exactly what they mean. Answer about THIS part only: first point at it with a cue (point with "target" = the element id, or the picture "region"/"area"), say in a sentence what it is, then explain it more simply. If they have not asked anything yet, say you see it and ask which part troubles them, then wait.';
    live.sendBoardNote(note, [whole, crop], false, '[انتخاب شاگرد روی تخته]');
    thumbMessage('این قسمت را نشان دادم.', crop);
    Stat.ok('sel', 'این قسمت را به استاد نشان دادید ✓', 1800);
    Stat.busy('ask', 'حالا بگویید چه می‌خواهید دربارهٔ آن بدانید…', 0, 14000);
    clearTimeout(stuTimer);
    stuTimer = setTimeout(function () { Stat.clear('ask'); if (live) live.nudge(); }, 8500);          // marked it and said nothing: the teacher asks which part
  }
  // the named parts (with their boxes) of the student's pictures, so the board can tell which part a marker covers
  function feedRegions(items, info) {
    if (!board || !board.setPicInfo) return;
    (items || []).forEach(function (m) { if (m && m.key && m.regions && m.regions.length) board.setPicInfo(m.key, m.regions); });
    if (info && info.image && info.regions) board.setPicInfo(info.image, info.regions);
  }
  function onStuInk() { var b = $('stu-send'); if (live && live.active) b.hidden = false; }
  function sendInk() {
    if (!live || !live.active || !board) return;
    var t = board.takeInk(); if (!t) { $('stu-send').hidden = true; return; }
    $('stu-send').hidden = true; Stat.clear('turn'); stuSpoke();
    thumbMessage('روی تخته نوشتم.', t.crop);
    Stat.busy('ink', 'استاد نوشتهٔ شما را می‌خواند…', 0, 40000);
    var id = cls && cls.id;
    var tm = new Promise(function (res) { setTimeout(function () { res(null); }, 25000); });
    Promise.race([api('/api/class/' + id + '/read_ink', { image: t.crop }), tm]).catch(function () { return null; }).then(function (d) {
      var rd = d && d.ok ? d : null, L = [];
      L.push('[نوشتهٔ شاگرد روی تخته] The student wrote this BY HAND on the board (light-blue chalk).');
      if (rd && rd.text) L.push('My reading of it: «' + rd.text + '» (kind: ' + rd.kind + ', confidence ' + rd.confidence.toFixed(2) + (rd.note ? '; unclear: ' + rd.note : '') + ').');
      else L.push('The reading tool could not read it; read it yourself from the first picture and say what you read.');
      L.push('Where it is: ' + descText(t.desc));
      L.push('The first picture is their writing alone (dark on white), the second is the whole board. React like their teacher: say back what you read if it is uncertain or matters, then judge it kindly (a tick or a pink circle on it, one sentence why, a hint before the full answer).');
      if (!live || !live.sendBoardNote(L.join('\n'), [t.crop, t.board], true, '[نوشتهٔ شاگرد روی تخته] ' + (rd && rd.text ? rd.text : ''))) { Stat.fail('ink', 'هنوز به استاد وصل نیستید.'); return; }
      Stat.ok('ink', 'نوشته به استاد رسید ✓', 2000); Stat.busy('think', 'استاد نوشتهٔ شما را می‌بیند…', 0, 40000);
    });
  }
  function onStuTyped(info) {
    if (!live || !live.active) { toast('اول به استاد وصل شوید.'); return; }
    stuSpoke(); Stat.clear('turn');
    var note = '[نوشتهٔ تایپی شاگرد] The student typed this on the board (light-blue text): «' + info.text + '».\nWhere it is: ' + descText(info.desc) +
      '\nReact like their teacher: judge it kindly (a tick or a pink circle on it, one sentence why, a hint before the full answer), or answer it if it is a question.';
    live.sendBoardNote(note, [board.snapshot({ rect: padRect(info.rect, 60), targetW: 700 }), board.snapshot({ targetW: 1000 })], true, '[نوشتهٔ تایپی روی تخته] ' + info.text);
    addMessage('user', info.text);
    Stat.ok('typed', 'نوشته به استاد رسید ✓', 1800); Stat.busy('think', 'استاد نوشتهٔ شما را می‌بیند…', 0, 40000);
  }
  function wireStudent(b) {
    var bar = $('stu-tools');
    b.on('select', onStuSelect).on('ink', onStuInk).on('typed', onStuTyped).on('tool', function (name) {
      [].forEach.call(bar.querySelectorAll('.stu-btn[data-tool]'), function (x) { x.classList.toggle('on', x.dataset.tool === name); });
      if (name === 'point') Stat.info('با موس یا انگشت روی تخته نشان بدهید یا دور چیزی خط بکشید؛ بعد بگویید چه می‌خواهید.', 5200);
      else if (name === 'pen') Stat.info('با انگشت یا قلم بنویسید؛ وقتی تمام شد «فرستادن نوشته» را بزنید.', 5200);
      else if (name === 'type') Stat.info('روی تخته بزنید، بنویسید و Enter بزنید.', 4500);
    }).on('turn', function (turn) {
      if (!live || !live.active) return;
      showStuTools(true);
      if (!b.stu || b.stu.tool !== 'pen') b.setTool('pen');
      Stat.busy('turn', 'نوبت شماست: ' + (turn.label || 'روی تخته بنویسید') + ' — بعد «فرستادن نوشته» را بزنید.', 0, 240000);
    });
  }
  (function stuInit() {
    var bar = $('stu-tools'); if (!bar) return;
    [].forEach.call(bar.querySelectorAll('.stu-btn[data-tool]'), function (x) { x.addEventListener('click', function () { if (board) board.setTool(x.dataset.tool); }); });
    $('stu-undo').addEventListener('click', function () { if (board && board.studentUndo()) { if (!board.unsent().length) $('stu-send').hidden = true; } });
    $('stu-send').addEventListener('click', sendInk);
  })();

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
