/* Tutor classroom interface. The lesson engine is not connected yet, so this page
   only shows the classroom itself: a lessons list, the green board and the conversation. */
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var isMobile = function () { return window.matchMedia('(max-width: 860px)').matches; };
  var sidebarOpen = !isMobile();
  var course = null;     // { name, lessons: [{title, blocks}] }
  var current = 0;
  var busy = false;

  /* ───────── helpers ───────── */
  function toast(text) {
    var el = $('toast');
    el.textContent = text; el.classList.add('show');
    clearTimeout(toast._t); toast._t = setTimeout(function () { el.classList.remove('show'); }, 3200);
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
  };
  window.voiceSoon = function () { toast('گفتگوی صوتی با استاد به‌زودی در کلاس فعال می‌شود.'); };

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
    busy = true; $('send-btn').disabled = true; typing();
    setTimeout(function () {
      var t = $('typing'); if (t) t.remove();
      var link = document.createElement('div'); link.style.marginTop = '10px';
      var a = document.createElement('a'); a.href = '#'; a.textContent = 'دیدن کلاس نمونه';
      a.addEventListener('click', function (e) { e.preventDefault(); loadSample(); });
      var sep = document.createTextNode('  ·  ');
      var b = document.createElement('a'); b.href = '/chat'; b.textContent = 'پرسیدن در بخش گفتگو';
      link.appendChild(a); link.appendChild(sep); link.appendChild(b);
      addMessage('ai', 'پیام شما را دیدم. چیدن درس و آموزش روی تخته هنوز در حال آماده‌سازی است و به‌زودی فعال می‌شود. تا آن زمان می‌توانید یک کلاس نمونه ببینید، یا پرسش‌تان را در بخش گفتگو بپرسید.', link);
      busy = false; $('send-btn').disabled = false;
    }, 900);
  };

  /* ───────── lessons ───────── */
  function renderLessons() {
    var list = $('lesson-list'); list.textContent = '';
    if (!course) {
      var e = document.createElement('div'); e.className = 'sidebar-empty'; e.id = 'lesson-empty';
      e.innerHTML = 'هنوز کلاسی شروع نشده است.<br>به استاد بگویید چه چیزی می‌خواهید بیاموزید؛ درس‌ها همین‌جا ردیف می‌شوند.';
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
      b.addEventListener('click', function () { openLesson(i); if (isMobile()) { sidebarOpen = false; applySidebar(); } });
      list.appendChild(b);
    });
    $('course-count').textContent = 'درس ' + faNum(current + 1) + ' از ' + faNum(course.lessons.length);
    $('course-progress-fill').style.width = (current / course.lessons.length * 100) + '%';
  }
  function faNum(n) { return String(n).replace(/\d/g, function (d) { return '۰۱۲۳۴۵۶۷۸۹'[d]; }); }

  window.stepLesson = function (d) { openLesson(current + d); };
  function openLesson(i) {
    if (!course || i < 0 || i >= course.lessons.length) return;
    current = i; renderLessons();
    $('lesson-title').textContent = course.lessons[i].title;
    $('prev-lesson').disabled = i === 0;
    $('next-lesson').disabled = i === course.lessons.length - 1;
    drawBoard(course.lessons[i].blocks);
    $('board').scrollTop = 0;
  }

  /* ───────── board ───────── */
  var NS = 'http://www.w3.org/2000/svg';
  function el(tag, attrs, cls) {
    var n = document.createElementNS(NS, tag);
    for (var k in attrs) n.setAttribute(k, attrs[k]);
    if (cls) n.setAttribute('class', cls); return n;
  }
  function drawBoard(blocks) {
    var box = $('board-content'); box.textContent = '';
    $('board-empty').hidden = true; $('board-controls').hidden = false;
    var t = 150;
    blocks.forEach(function (b) {
      var node;
      if (b.type === 'title') { node = document.createElement('h2'); node.className = 'bd-title bd-in'; node.textContent = b.text; t += 100; }
      else if (b.type === 'line') { node = document.createElement('p'); node.className = 'bd-line bd-in' + (b.key ? ' key' : ''); node.textContent = b.text; }
      else if (b.type === 'figure') { node = figure(b, t); }
      if (!node) return;
      if (b.type !== 'figure') node.style.setProperty('--d', t + 'ms');
      box.appendChild(node); t += b.type === 'figure' ? 1700 : 650;
    });
  }
  function figure(b, t) {
    var f = document.createElement('figure'); f.className = 'bd-figure';
    var s = el('svg', { viewBox: '0 0 200 200', role: 'img', 'aria-label': b.label });
    var delay = t;
    if (b.shape === 'pie') {
      var q = el('path', { d: 'M100 100 L100 20 A80 80 0 0 1 180 100 Z' }, 'fill bd-fade'); q.style.setProperty('--d', (delay + 1500) + 'ms');
      s.appendChild(q);
      [['circle', { cx: 100, cy: 100, r: 80 }, 520], ['path', { d: 'M100 20 V180' }, 170], ['path', { d: 'M20 100 H180' }, 170]].forEach(function (p, i) {
        var n = el(p[0], p[1], 'stroke bd-draw'); n.style.setProperty('--len', p[2]); n.style.setProperty('--d', (delay + i * 350) + 'ms'); s.appendChild(n);
      });
    } else if (b.shape === 'bars') {
      [[20, 'whole'], [60, 'half'], [100, 'quarter']].forEach(function (r, i) {
        var y = r[0] + 10; var parts = i === 0 ? 1 : i === 1 ? 2 : 4; var w = 180 / parts;
        for (var k = 0; k < parts; k++) {
          var rect = el('rect', { x: 10 + k * w, y: y, width: w, height: 30, rx: 3 }, 'stroke bd-draw' + (k === 0 ? ' gold' : ''));
          rect.style.setProperty('--len', 2 * (w + 30)); rect.style.setProperty('--d', (delay + i * 450 + k * 120) + 'ms'); s.appendChild(rect);
        }
      });
    }
    var cap = document.createElement('figcaption'); cap.className = 'bd-fade'; cap.style.setProperty('--d', (delay + 1700) + 'ms'); cap.textContent = b.caption;
    f.appendChild(s); f.appendChild(cap); return f;
  }

  /* ───────── sample class ───────── */
  var SAMPLE = {
    name: 'کسرها، از پایه',
    lessons: [
      { title: 'کسر چیست؟', blocks: [
        { type: 'title', text: 'کسر چیست؟' },
        { type: 'line', text: 'کسر یعنی بخشی از یک کل.' },
        { type: 'line', text: 'کل را به بخش‌های برابر تقسیم می‌کنیم و چند بخش را برمی‌داریم.' },
        { type: 'figure', shape: 'pie', label: 'دایره‌ای که به چهار بخش برابر تقسیم شده و یک بخش آن رنگ شده است', caption: 'یک چهارم، یعنی یک بخش از چهار بخش برابر' },
        { type: 'line', key: true, text: 'مثال: نان را به چهار قسمت برابر ببُرید؛ هر قسمت یک چهارم است.' }
      ] },
      { title: 'نصف، یک‌سوم و یک‌چهارم', blocks: [
        { type: 'title', text: 'نصف، یک‌سوم و یک‌چهارم' },
        { type: 'line', text: 'هر چه کل را به بخش‌های بیشتری ببُریم، هر بخش کوچک‌تر می‌شود.' },
        { type: 'figure', shape: 'bars', label: 'سه نوار: یک کل، دو نیمه و چهار چهارم', caption: 'یک کل، دو نیمه، چهار چهارم' },
        { type: 'line', key: true, text: 'پس یک چهارم از نصف کوچک‌تر است.' }
      ] },
      { title: 'مقایسهٔ کسرها', blocks: [ { type: 'title', text: 'مقایسهٔ کسرها' }, { type: 'line', text: 'وقتی کل‌ها برابرند، کسری بزرگ‌تر است که بخش بزرگ‌تری از کل را برمی‌دارد.' } ] },
      { title: 'جمع کسرهای هم‌مخرج', blocks: [ { type: 'title', text: 'جمع کسرهای هم‌مخرج' }, { type: 'line', text: 'اگر بخش‌ها هم‌اندازه باشند، فقط شمار بخش‌ها را جمع می‌کنیم.' } ] },
      { title: 'مسئله‌هایی از زندگی روزمره', blocks: [ { type: 'title', text: 'مسئله‌هایی از زندگی روزمره' }, { type: 'line', text: 'کسرها را در بازار، آشپزخانه و ساعت می‌بینیم.' } ] }
    ]
  };
  window.loadSample = function () {
    course = SAMPLE; current = 0;
    $('messages').textContent = '';
    addMessage('user', 'می‌خواهم کسرها را از اول یاد بگیرم.');
    addMessage('ai', 'خوب است. برای شما یک مسیر پنج‌درسی چیدم؛ در ستون کناری می‌بینید. با درس اول شروع می‌کنیم، به تخته نگاه کنید.');
    openLesson(0); showPane('board');
    if (isMobile()) { sidebarOpen = false; applySidebar(); }
  };
  window.newClass = function () {
    course = null; current = 0; renderLessons();
    $('board-content').textContent = ''; $('board-empty').hidden = false; $('board-controls').hidden = true;
    $('lesson-title').textContent = 'کلاس درس';
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
