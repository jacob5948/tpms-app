/* Keep the sides panel honest while passes are being confirmed.
 *
 * Confirming a pass saves in place -- reviewing a week of traffic against
 * camera footage is dozens of these, and a full reload each time would put the
 * page back at the top every time. But every number in the sides panel is
 * computed from exactly those confirmations, so leaving it as it was would be
 * the stale-page bug in a new place. Instead the panel is re-fetched from the
 * server, which keeps one description of what the confirmations add up to.
 */
(function () {
  if (!document.getElementById('sides-panel')) return;   // the log has none

  async function refresh() {
    // Looked up per refresh, never held: this replaces the panel, so a
    // reference kept from page load points at a detached node from the second
    // confirmation onwards -- and every refresh after the first is silently
    // written to a copy of the panel that nobody can see.
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
      /* The panel is a summary of rows still on screen; a failed refresh is
       * not worth an error over, and the next confirmation tries again. */
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
