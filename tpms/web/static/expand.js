/* Collapse the sightings behind each pass, on every table that shows passes.
 *
 * The rows are in the DOM already -- this only hides them, so with JS off the
 * evidence behind a pass is simply visible rather than unreachable. Shared by
 * the log and a vehicle's own pass history: one behaviour, so a reader who
 * learns to open a row in one place has learnt it in both.
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
