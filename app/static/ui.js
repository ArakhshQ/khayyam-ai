/* Khayyam shared UI helpers: scroll reveal, nav state, mobile menu. No dependencies. */
(function () {
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) document.documentElement.classList.add('reduce-motion');
  document.documentElement.classList.remove('no-js');

  // Reveal elements marked data-reveal when they enter the viewport.
  var items = document.querySelectorAll('[data-reveal]');
  if (!('IntersectionObserver' in window) || reduce) {
    items.forEach(function (el) { el.classList.add('is-in'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) { e.target.classList.add('is-in'); io.unobserve(e.target); }
      });
    }, { threshold: 0, rootMargin: '0px 0px -8% 0px' });
    items.forEach(function (el) { io.observe(el); });
  }

  // Navigation: solid background after scrolling, mobile menu toggle.
  var nav = document.querySelector('.nav');
  if (nav) {
    var onScroll = function () { nav.classList.toggle('scrolled', window.scrollY > 24); };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    var toggle = nav.querySelector('.nav-toggle');
    if (toggle) {
      toggle.addEventListener('click', function () {
        var open = nav.classList.toggle('open');
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      });
      nav.querySelectorAll('.nav-link').forEach(function (a) {
        a.addEventListener('click', function () { nav.classList.remove('open'); });
      });
    }
  }

  // Show the user's name in the navigation when signed in.
  var guest = document.getElementById('auth-nav-guest');
  var user = document.getElementById('auth-nav-user');
  if (guest && user && window.fetch) {
    fetch('/api/me').then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.logged_in) {
        guest.hidden = true; user.hidden = false;
        var name = document.getElementById('nav-username-text');
        if (name) name.textContent = d.username;
      }
    }).catch(function () {});
  }
})();
