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

      select.addEventListener('change', function () {
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
