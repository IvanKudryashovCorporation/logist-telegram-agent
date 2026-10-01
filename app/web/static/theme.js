// Подключается в <head> до стилей и без defer: тема выставляется до первой
// отрисовки, иначе при тёмной теме страница на миг мигает белым.
(function () {
  var theme = null;
  try { theme = localStorage.getItem('theme'); } catch (e) { /* хранилище недоступно */ }
  if (theme !== 'dark' && theme !== 'light') {
    theme = window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  document.documentElement.dataset.theme = theme;
})();
