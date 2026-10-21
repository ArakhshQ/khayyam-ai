/* Tutor classroom: lessons list, the chalkboard (board.js) and the conversation with the teacher.
   The lesson engine (the model writing scenes) is connected in the next step; today the board plays a built-in sample class. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var isMobile = function () { return window.matchMedia('(max-width: 860px)').matches; };
  var sidebarOpen = !isMobile();
  var course = null;     // { name, lessons: [{title}] }
  var current = 0;
  var busy = false;
  var board = null;
  var handUp = false;
  var inDetour = false;

  /* ───────── helpers ───────── */
  function toast(text) {
    var el = $('toast');
    el.textContent = text; el.classList.add('show');
    clearTimeout(toast._t); toast._t = setTimeout(function () { el.classList.remove('show'); }, 3200);
  }
  function faNum(n) { return String(n).replace(/\d/g, function (d) { return '۰۱۲۳۴۵۶۷۸۹'[d]; }); }
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
  window.voiceSoon = function () { toast('گفتگوی صوتی با استاد به‌زودی در صنف فعال می‌شود.'); };

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
  function typing() {
    var wrap = document.createElement('div'); wrap.className = 'message ai'; wrap.id = 'typing';
    var av = document.createElement('div'); av.className = 'avatar ai'; av.appendChild(svgIcon('i-cap'));
    var b = document.createElement('div'); b.className = 'bubble';
    var d = document.createElement('div'); d.className = 'typing-dots';
    d.innerHTML = '<span></span><span></span><span></span>';
    b.appendChild(d); wrap.appendChild(av); wrap.appendChild(b);
    $('messages').appendChild(wrap); scrollDown();
  }
  function scrollDown() { var w = $('chat-wrapper'); w.scrollTop = w.scrollHeight; }

  function showStart() {
    var m = $('messages'); m.textContent = '';
    m.appendChild($('start-tpl').content.cloneNode(true));
    m.querySelectorAll('[data-say]').forEach(function (b) {
      b.addEventListener('click', function () { $('user-input').value = b.dataset.say; sendMessage(); });
    });
  }

  window.sendMessage = function () {
    var input = $('user-input'); var text = input.value.trim();
    if (!text || busy) return;
    if (text.length > 4000) { toast('پیام خیلی طولانی است.'); return; }
    var start = document.querySelector('.class-start'); if (start) start.remove();
    addMessage('user', text);
    input.value = ''; input.style.height = 'auto';
    var lessonOn = board && board.model;
    if (lessonOn) { board.pause(); setHand(false); }
    busy = true; $('send-btn').disabled = true; typing();
    setTimeout(function () {
      var t = $('typing'); if (t) t.remove();
      if (lessonOn && /مثال/.test(text)) {
        addMessage('ai', 'حتماً. یک لحظه، همین‌جا روی تخته برایتان می‌کشم.');
        showPane('board');
        fetch('/static/lessons/detour_demo.json').then(function (r) { return r.json(); }).then(function (l) { inDetour = true; board.detour(l); });
      } else if (lessonOn) {
        addMessage('ai', 'سؤالتان را دیدم. در نسخهٔ کامل، استاد همین‌جا با توجه به چیزی که روی تخته است جواب می‌دهد. برای دیدن نمونه، بنویسید «یک مثال بزن».');
        var go = document.createElement('div'); go.style.marginTop = '10px';
        var a = document.createElement('a'); a.href = '#'; a.textContent = 'ادامهٔ درس';
        a.addEventListener('click', function (e) { e.preventDefault(); showPane('board'); board.play(); });
        go.appendChild(a); $('messages').lastChild.querySelector('.bubble-content').appendChild(go);
      } else {
        var link = document.createElement('div'); link.style.marginTop = '10px';
        var a2 = document.createElement('a'); a2.href = '#'; a2.textContent = 'دیدن صنف نمونه';
        a2.addEventListener('click', function (e) { e.preventDefault(); loadSample(); });
        link.appendChild(a2);
        addMessage('ai', 'پیام شما را دیدم. چیدن درس توسط استاد به‌زودی فعال می‌شود. تا آن وقت می‌توانید صنف نمونه را ببینید.', link);
      }
      busy = false; $('send-btn').disabled = false;
    }, 700);
  };

  /* ───────── lessons list ───────── */
  function renderLessons() {
    var list = $('lesson-list'); list.textContent = '';
    if (!course) {
      var e = document.createElement('div'); e.className = 'sidebar-empty'; e.id = 'lesson-empty';
      e.innerHTML = 'هنوز صنفی شروع نشده است.<br>به استاد بگویید چه چیزی می‌خواهید بیاموزید؛ درس‌ها همین‌جا ردیف می‌شوند.';
      list.appendChild(e); $('course-head').hidden = true; return;
    }
    $('course-head').hidden = false;
    $('course-name').textContent = course.name;
    course.lessons.forEach(function (l, i) {
      var b = document.createElement('button'); b.type = 'button';
      b.className = 'lesson' + (i < current ? ' done' : '') + (i === current ? ' current' : '');
      b.style.setProperty('--i', i);
      var dot = document.createElement('span'); dot.className = 'lesson-dot';
      if (i < current) dot.appendChild(svgIcon('i-check')); else dot.textContent = faNum(i + 1);
      var t = document.createElement('span'); t.textContent = l.title;
      b.appendChild(dot); b.appendChild(t);
      list.appendChild(b);
    });
    $('course-count').textContent = 'درس ' + faNum(current + 1) + ' از ' + faNum(course.lessons.length);
    $('course-progress-fill').style.width = (current / course.lessons.length * 100) + '%';
  }

  /* ───────── board ───────── */
  function ensureBoard() {
    if (board) return board;
    board = new KB.Board($('board-stage')); window.__board = board;
    board.on('say', onSay).on('scene', onScene).on('time', onTime).on('state', onState).on('end', onEnd);
    return board;
  }
  function buildScrub() {
    var box = $('scrub'); box.textContent = '';
    board.model.scenes.forEach(function (s, i) {
      var b = document.createElement('button'); b.type = 'button'; b.setAttribute('aria-label', 'بخش ' + faNum(i + 1));
      b.appendChild(document.createElement('i'));
      b.addEventListener('click', function () { board.gotoScene(i, board.playing); setHand(false); });
      box.appendChild(b);
    });
  }
  function onScene(i) {
    var kids = $('scrub').children;
    for (var k = 0; k < kids.length; k++) {
      kids[k].classList.toggle('now', k === i); kids[k].classList.toggle('done', k < i);
      kids[k].firstChild.style.setProperty('--p', k < i ? 1 : 0);
    }
    $('btn-prev').disabled = i === 0 && board.st < 2;
    $('btn-next').disabled = i >= board.model.scenes.length - 1;
  }
  function onSay(text) {
    var cap = $('caption'); cap.textContent = '';
    var s = document.createElement('span'); s.textContent = text; cap.appendChild(s);
    var tr = $('transcript'); var p = document.createElement('p'); p.textContent = text; tr.appendChild(p);
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
  function onEnd() {
    if (inDetour) {
      inDetour = false;
      var row = document.createElement('div'); row.style.marginTop = '10px';
      var a = document.createElement('a'); a.href = '#'; a.textContent = 'برگشت به درس';
      a.addEventListener('click', function (e) { e.preventDefault(); board.resume(); buildScrub(); onScene(board.sceneIdx); showPane('board'); board.play(); });
      row.appendChild(a);
      addMessage('ai', 'مثال روشن شد؟ اگر خواستید به درس برمی‌گردیم.', row);
      return;
    }
    addMessage('ai', 'درس تمام شد. اگر سؤالی دارید همین‌جا بنویسید، یا بگویید «یک مثال بزن».');
    onScene(board.model.scenes.length - 1);
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
    if (!board || !board.model) { toast('اول یک صنف شروع کنید.'); return; }
    if (handUp) { setHand(false); board.play(); return; }
    board.pause(); setHand(true);
    toast('درس ایستاد. سؤالتان را بنویسید.');
    if (isMobile()) showPane('chat');
    $('user-input').focus();
  };

  /* ───────── sample class ───────── */
  window.loadSample = function () {
    fetch('/static/lessons/sample.json').then(function (r) { return r.json(); }).then(function (lesson) { startLesson(lesson, 'کسرها، از اول'); })
      .catch(function () { toast('بارگیری نمونه ممکن نشد.'); });
  };
  function startLesson(lesson, name) {
    course = { name: name, lessons: [{ title: lesson.title || name }] }; current = 0;
    $('messages').textContent = '';
    addMessage('user', 'می‌خواهم کسرها را از اول یاد بگیرم.');
    addMessage('ai', 'خوب است. به تخته نگاه کنید؛ من می‌نویسم و می‌کشم و توضیح می‌دهم. هر وقت خواستید «دست بالا» را بزنید یا همین‌جا بنویسید.');
    renderLessons(); $('board-empty').hidden = true; $('board-controls').hidden = false; $('lesson-title').textContent = lesson.title || name;
    $('transcript').textContent = ''; $('caption').textContent = '';
    showPane('board');
    sidebarOpen = false; applySidebar();
    ensureBoard().load(lesson).then(function () { buildScrub(); board.gotoScene(0, true); });
  }
  window.newClass = function () {
    if (board) { board.pause(); board.destroy(); board = null; }
    course = null; current = 0; renderLessons(); inDetour = false; setHand(false);
    $('board-empty').hidden = false; $('board-controls').hidden = true; $('caption').textContent = '';
    $('lesson-title').textContent = 'صنف درس';
    showStart(); showPane('chat'); if (!isMobile()) showPane('board');
    if (isMobile()) { sidebarOpen = false; applySidebar(); }
  };

  /* ───────── init ───────── */
  var input = $('user-input');
  input.addEventListener('input', function () { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 140) + 'px'; });
  input.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendMessage(); } });

  applySidebar(); showStart(); renderLessons();
  if (isMobile()) showPane('chat');

  fetch('/api/me').then(function (r) { return r.json(); }).then(function (d) {
    if (!d || !d.logged_in) return;
    $('auth-buttons').style.display = 'none';
    var menu = $('user-menu'); menu.style.display = 'flex';
    $('nav-username').textContent = d.username;
    $('nav-avatar').textContent = (d.username || '').trim().charAt(0).toUpperCase();
    if (d.is_admin) { var a = document.createElement('a'); a.href = '/admin'; a.className = 'side-btn gold'; a.textContent = 'مدیریت'; menu.appendChild(a); }
  }).catch(function () {});
})();