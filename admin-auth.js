// Adds the admin password (kept in this browser tab's sessionStorage only) to every
// same-origin /api request. On a 401 it asks for the password once and retries.
(function () {
  const KEY = 'cabaiAdminPw';
  const origFetch = window.fetch.bind(window);

  function isApi(input) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    return url.startsWith('/api/') || url.startsWith(window.location.origin + '/api/');
  }

  function withPw(init, pw) {
    init = Object.assign({}, init || {});
    const h = new Headers(init.headers || {});
    if (pw) h.set('X-Admin-Password', pw);
    init.headers = h;
    return init;
  }

  window.fetch = async function (input, init) {
    if (!isApi(input)) return origFetch(input, init);
    let res = await origFetch(input, withPw(init, sessionStorage.getItem(KEY)));
    if (res.status === 401 && !(init && init.body instanceof ReadableStream)) {
      const pw = prompt('Password admin diperlukan:');
      if (pw) {
        sessionStorage.setItem(KEY, pw);
        res = await origFetch(input, withPw(init, pw));
        if (res.status === 401) sessionStorage.removeItem(KEY);
      }
    }
    return res;
  };

  window.cabaiAdminLogin = async function (pw) {
    const r = await origFetch('/api/auth/check', { method: 'POST', headers: { 'X-Admin-Password': pw } });
    if (r.ok) sessionStorage.setItem(KEY, pw);
    return r.ok;
  };
  window.cabaiAdminLogout = function () { sessionStorage.removeItem(KEY); };
})();
