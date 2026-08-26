/* Text and toggle filtering for a table.
 *
 * Works on the rendered rows rather than round-tripping to the server: it
 * keeps the click-to-sort order intact, responds instantly, and at the scale
 * this runs at (hundreds of sensors, not millions) there is nothing to gain
 * from paging. Hidden rows stay in the DOM, so sort.js reorders them happily
 * and they reappear in the right place when the filter clears.
 */
(function () {
  function rowText(row) {
    if (row.dataset.text === undefined) row.dataset.text = row.innerText.toLowerCase();
    return row.dataset.text;
  }

  function apply(table, state, readout) {
    const needle = state.q.trim().toLowerCase();
    const rows = Array.from(table.tBodies[0]?.rows || []);
    let shown = 0;

    for (const row of rows) {
      const matchesText = !needle || rowText(row).includes(needle);
      const matchesFlags = state.flags.every(flag => row.dataset[flag] === '1');
      row.hidden = !(matchesText && matchesFlags);
      if (!row.hidden) shown += 1;
    }

    if (readout) {
      const filtered = shown !== rows.length;
      readout.textContent = filtered ? `showing ${shown} of ${rows.length}` : '';
      readout.hidden = !filtered;
    }
    // An empty result is otherwise a blank panel with no explanation.
    const empty = table.parentElement.querySelector('.filter-empty');
    if (empty) empty.hidden = shown !== 0 || rows.length === 0;
  }

  window.initFilter = function (root) {
    const scope = root || document;
    const bar = scope.querySelector('[data-filter-for]');
    if (!bar || bar.dataset.ready === 'on') return;
    const table = document.getElementById(bar.dataset.filterFor);
    if (!table) return;
    bar.dataset.ready = 'on';

    const input = bar.querySelector('input[type="search"]');
    const toggles = Array.from(document.querySelectorAll('[data-filter-flag]'));
    const readout = bar.querySelector('.filter-count');
    const key = 'tpms.filter:' + location.pathname;
    const state = { q: '', flags: [] };

    function refresh(persist) {
      state.flags = toggles.filter(t => t.classList.contains('active'))
                           .map(t => t.dataset.filterFlag);
      apply(table, state, readout);
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
  };

  document.addEventListener('DOMContentLoaded', () => window.initFilter());
})();
