/* Text and toggle filtering for a table or a grid of cards.
 *
 * Works on the rendered rows rather than round-tripping to the server: it
 * keeps the click-to-sort order intact, responds instantly, and at the scale
 * this runs at (hundreds of sensors, not millions) there is nothing to gain
 * from paging. Hidden rows stay in the DOM, so sort.js still reorders them
 * and they reappear in the right place when the filter clears.
 *
 * "Rows" are table rows by default, or any [data-filter-row] element, so the
 * vehicle cards get the same search and toggles the sensor table has.
 */
(function () {
  function rowsOf(container) {
    const explicit = container.querySelectorAll('[data-filter-row]');
    if (explicit.length) return Array.from(explicit);
    return Array.from(container.tBodies?.[0]?.rows || []);
  }

  function rowText(row) {
    if (row.dataset.text === undefined) row.dataset.text = row.innerText.toLowerCase();
    return row.dataset.text;
  }

  function apply(container, state, readout) {
    const needle = state.q.trim().toLowerCase();
    const rows = rowsOf(container);
    let shown = 0;

    for (const row of rows) {
      const matchesText = !needle || rowText(row).includes(needle);
      const matchesFlags = state.flags.every(flag => row.dataset[flag] === '1');
      /* Rows that only belong on screen when their flag is asked for --
       * hidden sensors, which are hidden precisely so they stay out of the
       * way, but must still be findable to be un-hidden. */
      const opt = row.dataset.hiddenUnless;
      const optedIn = !opt || state.flags.includes(opt);
      row.hidden = !(matchesText && matchesFlags && optedIn);
      if (!row.hidden) shown += 1;
    }

    if (readout) {
      const total = rows.filter(r => !r.dataset.hiddenUnless).length;
      const filtered = shown !== total;
      readout.textContent = filtered ? `showing ${shown} of ${total}` : '';
      readout.hidden = !filtered;
    }
    // An empty result is otherwise a blank panel with no explanation.
    const empty = container.parentElement.querySelector('.filter-empty');
    if (empty) empty.hidden = shown !== 0 || rows.length === 0;
  }

  window.initFilter = function (root) {
    const scope = root || document;
    scope.querySelectorAll('[data-filter-for]').forEach(bar => {
      if (bar.dataset.ready === 'on') return;
      const container = document.getElementById(bar.dataset.filterFor);
      if (!container) return;
      bar.dataset.ready = 'on';

      const input = bar.querySelector('input[type="search"]');
      // Scoped to this bar's target, so two filterable lists on one page
      // cannot drive each other's toggles.
      const toggles = Array.from(document.querySelectorAll(
        '[data-filter-flag][data-filter-target="' + bar.dataset.filterFor + '"]'
      ));
      const readout = bar.querySelector('.filter-count');
      const key = 'tpms.filter:' + location.pathname + ':' + bar.dataset.filterFor;
      const state = { q: '', flags: [] };

      function refresh(persist) {
        state.flags = toggles.filter(t => t.classList.contains('active'))
                             .map(t => t.dataset.filterFlag);
        apply(container, state, readout);
        if (persist) {
          try { localStorage.setItem(key, JSON.stringify(state)); } catch (e) {}
        }
      }

      input?.addEventListener('input', () => { state.q = input.value; refresh(true); });
      toggles.forEach(toggle => {
        toggle.addEventListener('click', () => {
          toggle.classList.toggle('active');
          toggle.setAttribute('aria-pressed', toggle.classList.contains('active'));
          refresh(true);
        });
        toggle.setAttribute('role', 'button');
        toggle.tabIndex = 0;
        toggle.addEventListener('keydown', e => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle.click(); }
        });
      });

      bar.querySelector('.filter-clear')?.addEventListener('click', () => {
        if (input) input.value = '';
        state.q = '';
        toggles.forEach(t => {
          t.classList.remove('active');
          t.setAttribute('aria-pressed', 'false');
        });
        refresh(true);
      });

      try {
        const saved = JSON.parse(localStorage.getItem(key) || 'null');
        if (saved) {
          state.q = saved.q || '';
          if (input) input.value = state.q;
          (saved.flags || []).forEach(flag => {
            const toggle = toggles.find(t => t.dataset.filterFlag === flag);
            toggle?.classList.add('active');
            toggle?.setAttribute('aria-pressed', 'true');
          });
        }
      } catch (e) { /* private browsing */ }
      refresh(false);
    });
  };

  document.addEventListener('DOMContentLoaded', () => window.initFilter());
})();
