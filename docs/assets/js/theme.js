/* Theme, loaded synchronously in <head> so the stored choice applies before first paint.
   Dark by default; a stored choice wins. The choice lives in localStorage under 'theme',
   the same key the portfolio uses, so it carries across pages on this origin.
   The toggle is any element with [data-theme-toggle]; no inline script is needed, which
   keeps the page under script-src 'self'. */
(function () {
  var root = document.documentElement;
  var KEY = 'theme';
  var PAPER = { dark: '#070708', light: '#f4f3ef' };

  function stored() {
    try {
      var t = localStorage.getItem(KEY);
      return t === 'dark' || t === 'light' ? t : null;
    } catch (e) {
      return null;
    }
  }

  function current() {
    return root.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  }

  /* The browser tints its toolbar from the theme-color meta whose media query matches the
     OS scheme, so both metas follow the page once a theme is applied. */
  function apply(theme) {
    root.setAttribute('data-theme', theme);
    var metas = document.querySelectorAll('meta[name="theme-color"]');
    for (var i = 0; i < metas.length; i++) metas[i].setAttribute('content', PAPER[theme]);
  }

  function label() {
    var next = current() === 'dark' ? 'light' : 'dark';
    var buttons = document.querySelectorAll('[data-theme-toggle]');
    for (var i = 0; i < buttons.length; i++) buttons[i].setAttribute('aria-label', 'Switch to ' + next + ' mode');
  }

  apply(stored() || 'dark');

  document.addEventListener('DOMContentLoaded', label);

  document.addEventListener('click', function (e) {
    var t = e.target && e.target.closest ? e.target.closest('[data-theme-toggle]') : null;
    if (!t) return;
    var next = current() === 'dark' ? 'light' : 'dark';
    root.classList.add('theming');
    apply(next);
    void root.offsetWidth; /* flush styles while transitions are muted, so the swap lands in one paint */
    root.classList.remove('theming');
    try { localStorage.setItem(KEY, next); } catch (err) {}
    label();
  });

  /* Another tab, or the portfolio on the same origin, changed the theme. */
  window.addEventListener('storage', function (e) {
    if (e.key === KEY && (e.newValue === 'dark' || e.newValue === 'light')) {
      apply(e.newValue);
      label();
    }
  });
})();
