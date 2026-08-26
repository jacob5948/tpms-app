/* Click-to-sort for every table in the UI.
 *
 * Cells carry a data-sort attribute wherever the displayed text is not the
 * value you would sort by -- "8m ago" has to sort by its timestamp, "1m 25s"
 * by seconds, "still audible" by when it started. Everything else falls back
 * to the cell text, parsed as a number when it looks like one.
 */
(function () {
  const MISSING = ['—', '-', ''];

  function value(row, index) {
    const cell = row.cells[index];
    if (!cell) return '';
    const explicit = cell.dataset.sort !== undefined
      ? cell.dataset.sort
      : cell.querySelector('[data-sort]')?.dataset.sort;
    return explicit !== undefined ? explicit : cell.innerText.trim();
  }

  function compare(a, b) {
    const aMissing = MISSING.includes(a), bMissing = MISSING.includes(b);
    // Blanks sort last in either direction; a column of dashes on top is noise.
    if (aMissing || bMissing) return aMissing && bMissing ? 0 : aMissing ? 1 : -1;
    const na = parseFloat(a), nb = parseFloat(b);
    const numeric = !isNaN(na) && !isNaN(nb) &&
                    /^-?[\d.]+$/.test(a.replace(/,/g, '')) &&
                    /^-?[\d.]+$/.test(b.replace(/,/g, ''));
    if (numeric) return na - nb;
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
  }

  function sortBy(table, index, direction) {
    const body = table.tBodies[0];
    if (!body) return;
    const rows = Array.from(body.rows);
    rows.sort((x, y) => compare(value(x, index), value(y, index)) * direction);
    rows.forEach(row => body.appendChild(row));

    table.querySelectorAll('th').forEach((th, i) => {
      th.classList.toggle('sorted', i === index);
      th.classList.toggle('desc', i === index && direction < 0);
      th.setAttribute('aria-sort', i !== index ? 'none'
        : direction > 0 ? 'ascending' : 'descending');
    });
    table.dataset.sortIndex = index;
    table.dataset.sortDir = direction;
  }

  function enable(table, key) {
    const head = table.tHead;
    if (!head || table.dataset.sortable === 'on') return;
    if (table.dataset.nosort !== undefined) return;   // e.g. the live feed
    table.dataset.sortable = 'on';

    Array.from(head.rows[0].cells).forEach((th, index) => {
      // Action columns have no heading and nothing meaningful to order by.
      if (!th.textContent.trim()) return;
      th.classList.add('sortable');
      th.tabIndex = 0;
      const activate = () => {
        const same = Number(table.dataset.sortIndex) === index;
        const direction = same && Number(table.dataset.sortDir) > 0 ? -1 : 1;
        sortBy(table, index, direction);
        try { localStorage.setItem(key, index + ':' + direction); } catch (e) {}
      };
      th.addEventListener('click', activate);
      th.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); }
      });
    });

    // Restore the last choice for this table, so a refresh keeps your order.
    try {
      const saved = localStorage.getItem(key);
      if (saved) {
        const [index, direction] = saved.split(':').map(Number);
        if (head.rows[0].cells[index]) sortBy(table, index, direction);
      }
    } catch (e) { /* private browsing */ }
  }

  window.initSortable = function (root) {
    const scope = root || document;
    scope.querySelectorAll('table').forEach((table, i) => {
      enable(table, 'tpms.sort:' + location.pathname + ':' + (table.id || i));
    });
  };

  document.addEventListener('DOMContentLoaded', () => window.initSortable());
})();
