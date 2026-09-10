// Attaches the CSRF token (rendered server-side into a <meta> tag on every
// page load) to every same-origin, state-changing fetch() call site-wide.
// This wraps window.fetch once - no existing fetch() call anywhere in the
// app needs to change to get CSRF protection.
(function () {
  const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

  function getCsrfToken() {
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : null;
  }

  const originalFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    const method = (init.method || 'GET').toUpperCase();

    if (MUTATING_METHODS.has(method)) {
      const token = getCsrfToken();
      if (token) {
        const headers = new Headers(init.headers || {});
        headers.set('X-CSRFToken', token);
        init = Object.assign({}, init, { headers });
      }
    }
    return originalFetch(input, init);
  };
})();

// Shared HTML-escaping helper, available on every page that loads this
// script. Use this whenever untrusted text (a username, a saved
// conversation title, AI-generated quiz content, anything that didn't
// come from your own static markup) gets interpolated into an innerHTML
// template literal - without it, that text can carry a script/img-onerror
// payload that executes in the viewer's own browser.
function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str ?? '';
  return div.innerHTML;
}