// Установка сайта как приложения: регистрация сервис-воркера и кнопка «Добавить на экран».
(function () {
  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
    });
  }

  var card = document.getElementById('installApp');
  if (!card) { return; }
  var button = document.getElementById('installAppBtn');
  var hint = document.getElementById('installAppHint');
  var deferred = null;

  var standalone = (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
    window.navigator.standalone === true;
  if (standalone) {
    hint.textContent = 'Приложение уже установлено и открыто с экрана телефона.';
    button.hidden = true;
    return;
  }

  var ua = navigator.userAgent || '';
  var isIos = /iPad|iPhone|iPod/.test(ua) || (ua.indexOf('Mac') > -1 && navigator.maxTouchPoints > 1);

  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();
    deferred = event;
    button.hidden = false;
  });
  window.addEventListener('appinstalled', function () {
    deferred = null;
    button.hidden = true;
    hint.textContent = 'Готово: значок «Лента заказов» появился на экране телефона.';
  });

  button.addEventListener('click', function () {
    if (!deferred) { return; }
    deferred.prompt();
    deferred.userChoice.finally(function () { deferred = null; button.hidden = true; });
  });

  if (isIos) {
    hint.textContent = 'На iPhone: откройте сайт в Safari, нажмите «Поделиться» (квадрат со стрелкой) и выберите «На экран Домой».';
  } else {
    hint.textContent = 'Нажмите кнопку ниже. Если её нет: в меню браузера (⋮) выберите «Установить приложение» или «Добавить на главный экран».';
  }
})();
