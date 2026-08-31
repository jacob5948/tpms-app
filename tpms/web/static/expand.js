/* Collapse the sightings behind each pass, on every table that shows passes.
 *
 * The rows are already in the DOM; this only hides them, so with JS off they
 * are visible instead of unreachable. Shared by the log and the vehicle page.
 */
(function () {
  document.querySelectorAll('.sub-rows').forEach(row => { row.hidden = true; });
  document.querySelectorAll('[data-expand]').forEach(button => {
    button.addEventListener('click', () => {
      const row = document.getElementById(button.dataset.expand);
      if (!row) return;
      row.hidden = !row.hidden;
      button.setAttribute('aria-expanded', String(!row.hidden));
    });
  });
})();
