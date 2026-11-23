(function () {
  var theme = 'tokyo-night';
  try {
    var stored = localStorage.getItem('seccam.theme');
    if (stored && /^[a-z-]{2,32}$/.test(stored)) theme = stored;
  } catch (e) { /* storage blocked: keep default */ }
  document.documentElement.dataset.theme = theme;
})();
