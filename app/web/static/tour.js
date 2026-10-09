// Обучение для новых водителей: затемнение, подсветка элемента и подсказка.
// Показывается один раз (флаг в localStorage), повторно — по кнопке «?» в шапке.
(function () {
  var KEY = 'tourDone';
  var steps = [
    { title: 'Добро пожаловать!',
      text: 'Это лента заказов на перевозку из рабочих групп диспетчеров. Цена и условия — ровно как в заявке, без наценки. Покажем главное за минуту.' },
    { sel: '.live-badge', title: 'Заказы в реальном времени',
      text: 'Сайт сам следит за группами диспетчеров. Число рядом — сколько групп сейчас читается. Новые заказы появляются в ленте сразу.' },
    { sel: '.tabs', title: 'Лента и «Мои заказы»',
      text: '«Лента» — все свободные заказы. «Мои заказы» — те, о которых вы договорились с диспетчером: они пропадают из общей ленты и лежат у вас.' },
    { sel: '#searchForm', title: 'Быстрый поиск',
      text: 'Введите город, телефон диспетчера или цену — лента покажет только подходящее.' },
    { sel: '.sort-picker', title: 'Сортировка',
      text: 'Упорядочьте заказы по времени подачи, цене и другим признакам. Стрелка меняет направление.' },
    { sel: '#filtersDetails > summary', title: 'Фильтры',
      text: 'Откуда и куда (можно добавить радиус вокруг города), тип авто, дата и время подачи, цена. После входа через Telegram фильтр можно сохранить — и бот будет присылать новые подходящие заказы.' },
    { sel: '.card', title: 'Карточка заказа',
      text: 'Время подачи, цена, маршрут, расстояние и метки. Нажмите на карточку — откроются все детали.' },
    { sel: '.card-footer-more', title: 'Как взять заказ',
      text: 'В карточке нажмите «Написать диспетчеру» — откроется Telegram с готовым сообщением. Договорились — жмите «Договорился с диспетчером», и заказ попадёт в «Мои заказы».' },
    { sel: '.auth-buttons, .header-profile', title: 'Вход через Telegram',
      text: 'Войдите, чтобы сохранять фильтры, получать уведомления в Telegram и вести «Мои заказы».' },
    { sel: '#themeToggle', title: 'Тёмная тема',
      text: 'Переключает светлое и тёмное оформление.' },
    { sel: '#tourBtn', title: 'Это всё!',
      text: 'Если что-то забудете — нажмите «?», и подсказки покажутся снова. Удачных рейсов!' }
  ];

  function done() { try { localStorage.setItem(KEY, '1'); } catch (e) { /* ignore */ } }
  function seen() { try { return localStorage.getItem(KEY) === '1'; } catch (e) { return false; } }

  function visible(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }

  var root, hole, tip, active = [], idx = 0, prevFocus;

  function el(tag, cls, parent) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (parent) parent.appendChild(e);
    return e;
  }

  function start() {
    if (root) return;
    active = steps.filter(function (s) { return !s.sel || visible(document.querySelector(s.sel)); });
    if (!active.length) return;
    prevFocus = document.activeElement;
    root = el('div', 'tour-root', document.body);
    el('div', 'tour-blocker', root); // перехватывает все клики и касания
    hole = el('div', 'tour-hole', root);
    tip = el('div', 'tour-tip', root);
    tip.setAttribute('role', 'dialog');
    tip.setAttribute('aria-modal', 'true');
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    idx = 0;
    show();
  }

  function finish() {
    done();
    if (!root) return;
    root.remove();
    root = null;
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', place);
    window.removeEventListener('scroll', place, true);
    if (prevFocus && prevFocus.focus) { try { prevFocus.focus(); } catch (e) { /* ignore */ } }
  }

  function go(delta) {
    var n = idx + delta;
    if (n < 0) return;
    if (n >= active.length) { finish(); return; }
    idx = n;
    show();
  }

  function show() {
    var s = active[idx], last = idx === active.length - 1;
    tip.innerHTML = '';
    el('div', 'tour-step', tip).textContent = (idx + 1) + ' из ' + active.length;
    el('div', 'tour-title', tip).textContent = s.title;
    el('div', 'tour-text', tip).textContent = s.text;
    var row = el('div', 'tour-actions', tip);
    if (!last) {
      var skip = el('button', 'tour-skip', row);
      skip.type = 'button';
      skip.textContent = 'Пропустить';
      skip.onclick = finish;
    }
    var nav = el('div', 'tour-nav', row);
    if (idx > 0) {
      var back = el('button', 'tour-back', nav);
      back.type = 'button';
      back.textContent = 'Назад';
      back.onclick = function () { go(-1); };
    }
    var next = el('button', 'tour-next', nav);
    next.type = 'button';
    next.textContent = last ? 'Готово' : (idx === 0 ? 'Начать' : 'Далее');
    next.onclick = function () { go(1); };

    var target = s.sel ? document.querySelector(s.sel) : null;
    if (target) {
      var r = target.getBoundingClientRect();
      if (r.top < 80 || r.bottom > window.innerHeight - 20) {
        target.scrollIntoView({ block: 'center' });
      }
    }
    place();
    next.focus({ preventScroll: true });
  }

  function place() {
    if (!root) return;
    var s = active[idx];
    var target = s.sel ? document.querySelector(s.sel) : null;
    var vw = window.innerWidth, vh = window.innerHeight, pad = 6, gap = 12;
    var tw = Math.min(340, vw - 24);
    tip.style.width = tw + 'px';
    var th = tip.offsetHeight, left, top;
    if (target && visible(target)) {
      var r = target.getBoundingClientRect();
      hole.style.display = 'block';
      hole.style.left = (r.left - pad) + 'px';
      hole.style.top = (r.top - pad) + 'px';
      hole.style.width = (r.width + pad * 2) + 'px';
      hole.style.height = (r.height + pad * 2) + 'px';
      var below = vh - r.bottom - pad - gap;
      var above = r.top - pad - gap;
      top = (below >= th || below >= above) ? r.bottom + pad + gap : r.top - pad - gap - th;
      top = Math.max(12, Math.min(top, vh - th - 12));
      left = Math.max(12, Math.min(r.left + r.width / 2 - tw / 2, vw - tw - 12));
    } else {
      hole.style.display = 'none'; // без цели: затемнение и карточка по центру
      left = (vw - tw) / 2;
      top = Math.max(12, (vh - th) / 2);
    }
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  }

  function onKey(e) {
    if (!root) return;
    if (e.key === 'Escape') { e.preventDefault(); finish(); return; }
    if (e.key === 'ArrowRight') { e.preventDefault(); go(1); return; }
    if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1); return; }
    if (e.key === 'Tab') { // фокус не уходит за пределы подсказки
      var btns = tip.querySelectorAll('button');
      if (!btns.length) return;
      var i = Array.prototype.indexOf.call(btns, document.activeElement);
      e.preventDefault();
      i = e.shiftKey ? (i <= 0 ? btns.length - 1 : i - 1) : (i + 1) % btns.length;
      btns[i].focus();
    }
  }

  var btn = document.getElementById('tourBtn');
  if (btn) btn.addEventListener('click', start);
  // Автозапуск — только на ленте (там есть что показывать) и только впервые.
  if (location.pathname === '/' && !seen()) {
    window.addEventListener('load', function () { setTimeout(start, 400); });
  }
})();
