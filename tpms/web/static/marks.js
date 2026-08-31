/* Refresh the sides panel as passes are confirmed.
 *
 * Confirming saves in place, because reviewing a week of traffic is dozens of
 * these and a reload each time would scroll the page back to the top. Every
 * number in the sides panel comes from those confirmations, so the panel is
 * re-fetched from the server rather than left stale or rebuilt here.
 */
(function () {
  if (!document.getElementById('sides-panel')) return;   // the log has none

  async function refresh() {
    // Looked up per refresh, never held: this replaces the panel, so a
    // reference kept from page load would be detached after the first
    // refresh, and later updates would go to a node not in the document.
    const panel = document.getElementById('sides-panel');
    if (!panel || !panel.dataset.vehicle) return;
    try {
      const response = await fetch('/api/vehicles/' + panel.dataset.vehicle + '/sides');
      if (!response.ok) return;
      const html = await response.text();
      const parsed = new DOMParser().parseFromString(html, 'text/html');
      const fresh = parsed.getElementById('sides-panel');
      if (fresh) panel.replaceWith(fresh);
    } catch (e) {
      /* A failed refresh leaves the previous summary in place; the next
       * confirmation tries again. */
    }
  }

  // Delegated, and on the document: the panel replaces itself, so a listener
  // bound to the old node would stop hearing after the first confirmation.
  document.addEventListener('tpms:done', event => {
    if (event.target.querySelector && event.target.querySelector('select[data-mark]')) {
      refresh();
    }
  });
})();
