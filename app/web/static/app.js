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
        fromInput.value = rangeStart ? toISO(rangeStart) : '';
        toInput.value = rangeEnd ? toISO(rangeEnd) : '';
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
          hiddenInput.value = selected.join(', ');
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

          var matches = KNOWN_CITIES.filter(function (city) {
            return norm(city).indexOf(q) !== -1 && !selected.some(function (c) { return norm(c) === norm(city); });
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

    (function () {
      var select = document.getElementById('sortSelect');
      if (!select) return;

      var latInput = document.getElementById('sortLat');
      var lonInput = document.getElementById('sortLon');
      var form = document.getElementById('searchForm');
      var lastValue = select.value; // на случай отказа геолокации — откатиться сюда
      var dirSelect = document.getElementById('dirSelect');
      var defaults = {};
      try { defaults = JSON.parse(dirSelect.getAttribute('data-defaults') || '{}'); } catch (e) {}

      // Направление меняется отдельно — сразу перезагружаем ленту.
      dirSelect.addEventListener('change', function () { form.submit(); });

      select.addEventListener('change', function () {
        // Новая сортировка — с её обычным направлением (свежие и дорогие сверху,
        // ближайшая подача первой); водитель может переключить его после.
        if (defaults[select.value]) dirSelect.value = defaults[select.value];
        if (select.value !== 'distance') {
          form.submit();
          return;
        }
        if (!('geolocation' in navigator)) {
          alert('Геолокация недоступна в этом браузере.');
          select.value = lastValue;
          return;
        }
        select.disabled = true;
        navigator.geolocation.getCurrentPosition(
          function (pos) {
            latInput.value = pos.coords.latitude;
            lonInput.value = pos.coords.longitude;
            select.disabled = false;
            form.submit();
          },
          function () {
            select.disabled = false;
            alert('Не удалось определить местоположение — проверьте разрешение геолокации в браузере (нужен HTTPS).');
            select.value = lastValue;
          },
          { enableHighAccuracy: false, timeout: 10000 }
        );
      });
    })();

    (function () {
      var form = document.getElementById('contactForm');
      if (!form) return;
      var text = form.getAttribute('data-message') || '';

      function copy() {
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).catch(function () {});
          }
        } catch (e) { /* буфер недоступен — ссылка с ?text= всё равно сработает */ }
      }

      // Копируем при отправке формы: если у диспетчера нет @username (или
      // клиент Telegram не подхватит ?text=), текст останется в буфере.
      form.addEventListener('submit', copy);
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

    // Возврат из карточки заказа в ленту на то же место прокрутки. Позицию
    // запоминаем при клике по карточке, а восстанавливаем, только если на ленту
    // пришли с карточки заказа или кнопкой «назад» (обычный заход — с начала).
    (function () {
      var KEY = 'scrollBeforeCard';

      function page() {
        var params = new URLSearchParams(location.search);
        params.delete('_cb');
        params.sort();
        return location.pathname + '?' + params.toString();
      }

      document.addEventListener('click', function (event) {
        var card = event.target.closest ? event.target.closest('a.card') : null;
        if (!card) return;
        try {
          sessionStorage.setItem(KEY, JSON.stringify({ page: page(), y: Math.round(window.scrollY) }));
        } catch (e) { /* хранилище недоступно — вернёмся в начало, как раньше */ }
      });

      var saved = null;
      try { saved = JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch (e) { saved = null; }
      if (!saved || saved.page !== page() || !saved.y) return;

      var entries = performance.getEntriesByType ? performance.getEntriesByType('navigation') : [];
      var cameBack = entries.length > 0 && entries[0].type === 'back_forward';
      var fromOrder = /\/orders\//.test(document.referrer || '');
      if (!cameBack && !fromOrder) return;

      function restore() { window.scrollTo(0, saved.y); }
      restore();
      // Страница могла дорисоваться после первой прокрутки — повторяем, но только
      // если пользователь сам ещё никуда не прокрутил.
      window.addEventListener('load', function () { if (window.scrollY < 10) restore(); });
    })();
