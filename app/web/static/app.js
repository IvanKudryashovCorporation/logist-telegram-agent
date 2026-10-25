    (function () {
      var field = document.getElementById('dateRangeField');
      if (!field) return; // фильтров нет на этой странице (не лента)

      var trigger = document.getElementById('dateRangeTrigger');
      var label = document.getElementById('dateRangeLabel');
      var calendar = document.getElementById('dateRangeCalendar');
      var grid = document.getElementById('drcGrid');
      var monthLabel = document.getElementById('drcMonthLabel');
      var hint = document.getElementById('drcHint');
      var fromInput = document.getElementById('dateFromInput');
      var toInput = document.getElementById('dateToInput');
      var clearBtn = document.getElementById('drcClear');
      var doneBtn = document.getElementById('drcDone');

      var MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
      var today = new Date();
      today.setHours(0, 0, 0, 0);

      function parseISO(s) {
        if (!s) return null;
        var parts = s.split('-');
        if (parts.length !== 3) return null;
        var d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
        d.setHours(0, 0, 0, 0);
        return d;
      }
      function toISO(d) {
        var m = String(d.getMonth() + 1).padStart(2, '0');
        var day = String(d.getDate()).padStart(2, '0');
        return d.getFullYear() + '-' + m + '-' + day;
      }
      function fmtShort(d) {
        return String(d.getDate()).padStart(2, '0') + '.' + String(d.getMonth() + 1).padStart(2, '0');
      }
      function fmtFull(d) {
        return fmtShort(d) + '.' + d.getFullYear();
      }

      var rangeStart = parseISO(fromInput.value);
      var rangeEnd = parseISO(toInput.value);
      var viewDate = new Date(rangeStart || today);
      viewDate.setDate(1);

      function updateLabel() {
        if (rangeStart && rangeEnd) {
          label.textContent = fmtFull(rangeStart) + ' – ' + fmtFull(rangeEnd);
        } else if (rangeStart) {
          label.textContent = 'с ' + fmtFull(rangeStart);
        } else {
          label.textContent = 'Любая дата';
        }
      }
      function updateHint() {
        hint.textContent = (rangeStart && !rangeEnd) ? 'Выберите дату окончания' : 'Выберите дату начала';
      }
      function updateInputs() {
        var before = fromInput.value + '|' + toInput.value;
        fromInput.value = rangeStart ? toISO(rangeStart) : '';
        toInput.value = rangeEnd ? toISO(rangeEnd) : '';
        // Скрытые поля из JS событий не порождают — сообщаем сами, чтобы
        // пересчитался счётчик на «Применить (N)».
        if (before !== fromInput.value + '|' + toInput.value) {
          fromInput.dispatchEvent(new Event('change', { bubbles: true }));
        }
      }

      function render() {
        monthLabel.textContent = MONTHS[viewDate.getMonth()] + ' ' + viewDate.getFullYear();
        grid.innerHTML = '';

        var firstOfMonth = new Date(viewDate.getFullYear(), viewDate.getMonth(), 1);
        // Понедельник = 0 ... воскресенье = 6
        var leadingBlanks = (firstOfMonth.getDay() + 6) % 7;
        var daysInMonth = new Date(viewDate.getFullYear(), viewDate.getMonth() + 1, 0).getDate();

        for (var i = 0; i < leadingBlanks; i++) {
          var blank = document.createElement('button');
          blank.className = 'drc-day';
          blank.disabled = true;
          blank.innerHTML = '<span class="drc-day-num"></span>';
          grid.appendChild(blank);
        }

        for (var day = 1; day <= daysInMonth; day++) {
          var d = new Date(viewDate.getFullYear(), viewDate.getMonth(), day);
          var btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'drc-day';

          if (d.getTime() === today.getTime()) btn.classList.add('today');
          if (rangeStart && rangeEnd && d >= rangeStart && d <= rangeEnd) btn.classList.add('in-range');
          if (rangeStart && d.getTime() === rangeStart.getTime()) btn.classList.add('range-start');
          if (rangeEnd && d.getTime() === rangeEnd.getTime()) btn.classList.add('range-end');

          btn.innerHTML = '<span class="drc-day-num">' + day + '</span>';
          btn.addEventListener('click', (function (clickedDate) {
            return function () {
              if (!rangeStart || (rangeStart && rangeEnd)) {
                rangeStart = clickedDate;
                rangeEnd = null;
              } else if (clickedDate < rangeStart) {
                rangeEnd = rangeStart;
                rangeStart = clickedDate;
              } else {
                rangeEnd = clickedDate;
              }
              updateInputs();
              updateLabel();
              updateHint();
              render();
              // Календарь остаётся открытым, пока водитель сам его не
              // закроет — ни автозакрытия по таймеру, ни по клику мимо
              // (это и раздражало: закрывался раньше, чем успевали
              // выбрать вторую дату).
            };
          })(d));

          grid.appendChild(btn);
        }
      }

      trigger.addEventListener('click', function () {
        calendar.hidden = !calendar.hidden;
        trigger.setAttribute('aria-expanded', calendar.hidden ? 'false' : 'true');
        if (!calendar.hidden) render();
      });

      document.querySelectorAll('.drc-nav').forEach(function (btn) {
        btn.addEventListener('click', function () {
          viewDate.setMonth(viewDate.getMonth() + Number(btn.dataset.dir));
          render();
        });
      });

      clearBtn.addEventListener('click', function () {
        rangeStart = null;
        rangeEnd = null;
        updateInputs();
        updateLabel();
        updateHint();
        render();
      });

      doneBtn.addEventListener('click', function () {
        calendar.hidden = true;
        trigger.setAttribute('aria-expanded', 'false');
      });

      updateLabel();
      updateHint();
    })();

    (function () {
      var citiesDataEl = document.getElementById('knownCitiesData');
      if (!citiesDataEl) return; // фильтров нет на этой странице (не лента)

      var KNOWN_CITIES;
      try { KNOWN_CITIES = JSON.parse(citiesDataEl.textContent || '[]'); } catch (e) { KNOWN_CITIES = []; }

      function norm(s) { return s.trim().toLowerCase(); }

      document.querySelectorAll('.city-picker').forEach(function (root) {
        var textInput = root.querySelector('.city-picker-input');
        var suggestionsBox = root.querySelector('.city-suggestions');
        var chipsBox = root.querySelector('.city-chips');
        var hiddenInput = root.querySelector('input[type="hidden"]');

        var selected = (hiddenInput.value || '')
          .split(',')
          .map(function (s) { return s.trim(); })
          .filter(Boolean);

        function syncHidden() {
          var before = hiddenInput.value;
          hiddenInput.value = selected.join(', ');
          if (before !== hiddenInput.value) {
            hiddenInput.dispatchEvent(new Event('change', { bubbles: true }));
          }
        }

        function renderChips() {
          chipsBox.innerHTML = '';
          selected.forEach(function (city) {
            var chip = document.createElement('span');
            chip.className = 'city-chip';

            var label = document.createElement('span');
            label.textContent = city;
            chip.appendChild(label);

            var removeBtn = document.createElement('button');
            removeBtn.type = 'button';
            removeBtn.className = 'city-chip-x';
            removeBtn.setAttribute('aria-label', 'Убрать ' + city + ' из фильтра');
            removeBtn.textContent = '×';
            removeBtn.addEventListener('click', function () {
              selected = selected.filter(function (c) { return c !== city; });
              syncHidden();
              renderChips();
            });
            chip.appendChild(removeBtn);

            chipsBox.appendChild(chip);
          });
        }

        function hideSuggestions() {
          suggestionsBox.hidden = true;
          suggestionsBox.innerHTML = '';
        }

        function addCity(city) {
          city = city.trim();
          if (!city) return;
          var exists = selected.some(function (c) { return norm(c) === norm(city); });
          if (!exists) {
            selected.push(city);
            syncHidden();
            renderChips();
          }
          textInput.value = '';
          hideSuggestions();
        }

        function showSuggestions(query) {
          var q = norm(query);
          if (!q) { hideSuggestions(); return; }

          // Порядок подсказок: точное совпадение, потом названия, начинающиеся с запроса,
          // потом слова с запросом в начале («ЖД Симферополь»), и только потом всё остальное.
          function rank(city) {
            var name = norm(city);
            if (name === q) return 0;
            if (name.indexOf(q) === 0) return 1;
            if (name.split(/[\s(,\/-]+/).some(function (w) { return w.indexOf(q) === 0; })) return 2;
            return 3;
          }
          var matches = KNOWN_CITIES.filter(function (city) {
            return norm(city).indexOf(q) !== -1 && !selected.some(function (c) { return norm(c) === norm(city); });
          }).sort(function (a, b) {
            return rank(a) - rank(b) || a.length - b.length || a.localeCompare(b, 'ru');
          }).slice(0, 8);

          if (!matches.length) { hideSuggestions(); return; }

          suggestionsBox.innerHTML = '';
          matches.forEach(function (city) {
            var btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'city-suggestion';
            btn.textContent = city;
            btn.addEventListener('mousedown', function (e) {
              // mousedown, не click — срабатывает раньше blur текстового поля.
              e.preventDefault();
              addCity(city);
            });
            suggestionsBox.appendChild(btn);
          });
          suggestionsBox.hidden = false;
        }

        textInput.addEventListener('input', function () {
          showSuggestions(textInput.value);
        });
        textInput.addEventListener('focus', function () {
          if (textInput.value.trim()) showSuggestions(textInput.value);
        });
        textInput.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') {
            e.preventDefault();
            if (textInput.value.trim()) addCity(textInput.value);
          } else if (e.key === 'Escape') {
            hideSuggestions();
          } else if (e.key === 'Backspace' && !textInput.value && selected.length) {
            selected.pop();
            syncHidden();
            renderChips();
          }
        });
        textInput.addEventListener('blur', function () {
          setTimeout(hideSuggestions, 150);
        });

        renderChips();
      });
    })();

    // «Написать диспетчеру» в строке заказа: текст сообщения кладём в буфер, а чат откроет сервер.
    (function () {
      document.addEventListener('submit', function (event) {
        var form = event.target;
        if (!form.classList || !form.classList.contains('contact-form')) return;
        var text = form.getAttribute('data-message') || '';
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).catch(function () {});
          }
        } catch (e) { /* буфер недоступен — ссылка с ?text= всё равно сработает */ }
      });
    })();

    // Сердечко: добавить заказ в «Избранное» или убрать, не перезагружая страницу.
    (function () {
      function bump(delta) {
        document.querySelectorAll('.tabs a[href="/favorites"] .tab-count').forEach(function (el) {
          el.textContent = Math.max(0, (parseInt(el.textContent, 10) || 0) + delta);
        });
      }

      document.addEventListener('click', function (event) {
        var button = event.target.closest ? event.target.closest('.fav-btn') : null;
        if (!button || button.disabled) return;
        event.preventDefault();
        button.disabled = true;
        fetch('/orders/' + button.getAttribute('data-order-id') + '/favorite', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Accept': 'application/json' }
        })
          .then(function (response) {
            if (response.status === 401) {
              location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
              return null;
            }
            return response.ok ? response.json() : null;
          })
          .then(function (data) {
            button.disabled = false;
            if (!data) return;
            var on = !!data.favorite;
            var was = button.classList.contains('on');
            button.classList.toggle('on', on);
            button.setAttribute('aria-pressed', on ? 'true' : 'false');
            if (on !== was) bump(on ? 1 : -1);
            // На странице «Избранное» снятое сердечко убирает строку из списка.
            var row = button.closest('#favoritesList .order-row');
            if (row && !on) row.remove();
          })
          .catch(function () { button.disabled = false; });
      });
    })();

    // Бегунок радиуса: подпись «+ N км» обновляется на лету.
    (function () {
      document.querySelectorAll('.radius-slider input[type="range"]').forEach(function (range) {
        var output = range.parentNode.querySelector('.radius-value');
        function paint() {
          var value = parseInt(range.value, 10) || 0;
          if (output) output.textContent = value ? '+ ' + value + ' км' : 'Только город';
          range.style.setProperty('--fill', (value / (parseInt(range.max, 10) || 1) * 100) + '%');
        }
        range.addEventListener('input', paint);
        paint();
      });
    })();


    // Панель фильтра: «Применить (N)» пересчитывается на лету, кнопка прилипает
    // к низу экрана, а после применения панель закрывается.
    (function () {
      var details = document.getElementById('filtersDetails');
      var form = document.getElementById('filtersForm');
      if (!details || !form) return;

      // Высота панели — до нижнего края экрана: поля прокручиваются внутри,
      // а кнопки «Применить»/«Сбросить» всегда на виду (и на телефоне).
      function fitPanel() {
        if (!details.open) return;
        var top = form.getBoundingClientRect().top;
        form.style.maxHeight = Math.max(240, window.innerHeight - top - 12) + 'px';
      }
      details.addEventListener('toggle', fitPanel);
      window.addEventListener('resize', fitPanel);
      fitPanel();

      form.addEventListener('submit', function () { details.open = false; });

      var countEl = document.getElementById('filterApplyCount');
      if (!countEl) return;
      var SKIP = { notify: 1, edit_id: 1 };
      var timer = null;
      var controller = null;
      var lastQuery = null;

      function query() {
        var params = new URLSearchParams();
        new FormData(form).forEach(function (value, key) {
          if (!SKIP[key] && value !== '' && value !== '0') params.append(key, value);
        });
        return params.toString();
      }
      lastQuery = query();

      function refresh() {
        var current = query();
        if (current === lastQuery) return; // поле без значения для фильтра (ввод города и т.п.)
        lastQuery = current;
        if (controller) controller.abort();
        controller = window.AbortController ? new AbortController() : null;
        countEl.classList.add('is-loading');
        fetch('/feed/count?' + current, { signal: controller ? controller.signal : undefined })
          .then(function (response) { return response.ok ? response.json() : null; })
          .then(function (data) {
            if (data && typeof data.total === 'number') countEl.textContent = '(' + data.total + ')';
            countEl.classList.remove('is-loading');
          })
          .catch(function (error) {
            if (error && error.name === 'AbortError') return; // уже идёт свежий запрос
            countEl.classList.remove('is-loading');
          });
      }

      function schedule() {
        clearTimeout(timer);
        timer = setTimeout(refresh, 350);
      }
      form.addEventListener('input', schedule);
      form.addEventListener('change', schedule);
    })();

    (function () {
      var toggle = document.getElementById('themeToggle');
      if (!toggle) return;
      toggle.addEventListener('click', function () {
        var next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
        document.documentElement.dataset.theme = next;
        try { localStorage.setItem('theme', next); } catch (e) { /* без хранилища тема живёт до перезагрузки */ }
      });
    })();

// Страница подписки: закрытие баннера и состояние «Создаём платёж…» на кнопках оплаты.
(function () {
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-dismiss]') : null;
    if (!btn) return;
    var target = document.querySelector(btn.getAttribute('data-dismiss'));
    if (target) target.remove();
  });

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.classList || !form.classList.contains('pay-form')) return;
    var button = form.querySelector('button[type="submit"]');
    if (!button || button.getAttribute('aria-busy') === 'true') return;
    var label = button.querySelector('.sb-btn-label');
    var icon = button.querySelector('svg');
    if (label) label.textContent = 'Создаём платёж…';
    if (icon) {
      icon.classList.add('sb-spin');
      icon.innerHTML = '<path d="M21 12a9 9 0 1 1-6.2-8.6"/>';
    }
    button.classList.add('sb-btn-load');
    button.setAttribute('aria-busy', 'true');
    // Блокируем уже после отправки формы, иначе браузер не пошлёт её данные.
    setTimeout(function () { button.disabled = true; }, 0);
  });

  // Возврат кнопкой «назад» из банка: страница из кэша осталась в состоянии загрузки.
  window.addEventListener('pageshow', function (e) {
    if (e.persisted && document.querySelector('.pay-form [aria-busy="true"]')) location.reload();
  });
})();

// Колокольчик уведомлений: открыть/закрыть панель; при открытии всё отмечается прочитанным.
(function () {
  var btn = document.getElementById('bellBtn');
  var panel = document.getElementById('bellPanel');
  if (!btn || !panel) return;
  function close() { panel.hidden = true; btn.setAttribute('aria-expanded', 'false'); }
  btn.addEventListener('click', function (e) {
    e.stopPropagation();
    var opening = panel.hidden;
    panel.hidden = !opening;
    btn.setAttribute('aria-expanded', opening ? 'true' : 'false');
    var dot = document.getElementById('bellDot');
    if (opening && dot) {
      fetch('/notifications/read', { method: 'POST', credentials: 'same-origin' })
        .then(function () { dot.remove(); })
        .catch(function () { /* сеть: бейдж останется до перезагрузки */ });
    }
  });
  document.addEventListener('click', function (e) { if (!panel.hidden && !panel.contains(e.target)) close(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') close(); });
})();
