/* Khayyam shared front-end helpers. Loaded in <head> of every page.
 *  - adds the CSRF token to every same-origin write request (fetch)
 *  - escapeHtml(): use for ANY user/AI text placed inside innerHTML
 *  - renderMarkdown(): markdown -> sanitized HTML for AI replies
 */
(function () {
  if (window.__khayyamMain) return;
  window.__khayyamMain = true;

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute('content') : '';
  }

  var nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    var method = String(init.method || (input && input.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
      var url = typeof input === 'string' ? input : ((input && input.url) || '');
      var sameOrigin = (url.charAt(0) === '/' && url.charAt(1) !== '/') || url.indexOf(location.origin) === 0;
      if (sameOrigin) {
        var headers = new Headers(init.headers || (input && input.headers) || {});
        if (!headers.has('X-CSRFToken')) headers.set('X-CSRFToken', csrfToken());
        init.headers = headers;
      }
    }
    return nativeFetch(input, init);
  };

  window.escapeHtml = function (value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };

  var hookInstalled = false;
  window.renderMarkdown = function (text) {
    text = String(text == null ? '' : text);
    if (!window.marked || !window.DOMPurify) return window.escapeHtml(text).replace(/\n/g, '<br>');
    if (!hookInstalled) {
      hookInstalled = true;
      DOMPurify.addHook('afterSanitizeAttributes', function (node) {
        if (node.tagName === 'A') {
          node.setAttribute('target', '_blank');
          node.setAttribute('rel', 'noopener noreferrer nofollow');
        }
      });
    }
    return DOMPurify.sanitize(marked.parse(text), { USE_PROFILES: { html: true } });
  };
})();
