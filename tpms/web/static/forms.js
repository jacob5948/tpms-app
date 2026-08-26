/* Feedback for every mutation in the UI.
 *
 * Three things, in order of how much they matter:
 *
 *  1. The clicked button goes busy immediately, so a submit that takes a
 *     second on a Pi does not look like a click that never landed.
 *  2. A toast reports the outcome. Full-page submits get theirs from the
 *     flash the server sets on the redirect; forms marked data-async get it
 *     from the JSON reply.
 *  3. Forms marked data-async never navigate at all. Only forms whose effect
 *     is confined to one row qualify -- labelling a wheel. Anything that moves
 *     a sensor between vehicles reshapes the page and still reloads it.
 */
(function () {
  const TOAST_MS = 4000;

  function tray() {
    let el = document.getElementById('toasts');
    if (!el) {
      el = document.createElement('div');
      el.id = 'toasts';
      el.setAttribute('aria-live', 'polite');
      document.body.appendChild(el);
    }
    return el;
  }

  function toast(text, kind) {
    const el = document.createElement('div');
    el.className = 'toast' + (kind ? ' ' + kind : '');
    el.textContent = text;
    tray().appendChild(el);
    setTimeout(() => {
      el.classList.add('leaving');
      setTimeout(() => el.remove(), 400);
    }, TOAST_MS);
  }
  window.tpmsToast = toast;

  function markBusy(button, form) {
    if (!button || button.disabled) return null;
    const label = button.textContent;
    // The browser has already serialised the form by the time submit fires,
    // so disabling here cannot drop a field.
    button.disabled = true;
    button.classList.add('busy');
    button.textContent = form.dataset.busy || 'Saving…';
    return () => {
      button.disabled = false;
      button.classList.remove('busy');
      button.textContent = label;
    };
  }

  /* Keep the wheel pill beside the sensor name in step with the field that
   * just changed it -- without this the async save is invisible on the row. */
  function updateWheelPill(form, label) {
    const scope = form.closest('tr') || form.closest('.panel');
    const slot = scope && scope.querySelector('[data-wheel-slot]');
    if (!slot) return;
    slot.querySelectorAll('.pill.wheel').forEach(p => p.remove());
    if (!label) return;
    const pill = document.createElement('span');
    pill.className = 'pill wheel';
    pill.textContent = label;
    slot.querySelector('a')?.after(document.createTextNode(' '), pill);
  }

  document.addEventListener('submit', function (event) {
    const form = event.target;
    if (!form.matches('form') || form.method.toLowerCase() !== 'post') return;
    const button = form.querySelector('button[type="submit"], button:not([type])');

    if (form.dataset.async === undefined) {
      markBusy(button, form);   // then let the browser navigate as usual
      return;
    }

    event.preventDefault();
    const done = markBusy(button, form);
    fetch(form.action, {
      method: 'POST',
      body: new FormData(form),
      headers: { 'X-Tpms-Async': '1' },
    })
      .then(response => response.ok ? response.json() : Promise.reject(response))
      .then(data => {
        toast(data.message || 'Saved.', 'ok');
        if ('wheel_label' in data) updateWheelPill(form, data.wheel_label);
        form.dispatchEvent(new CustomEvent('tpms:saved', { detail: data, bubbles: true }));
      })
      .catch(() => toast('Could not save. Is the receiver still running?', 'err'))
      .finally(() => done && done());
  });

  document.addEventListener('DOMContentLoaded', () => {
    // Whatever the last mutation did, reported on the page it landed on.
    const flash = document.getElementById('flash');
    if (flash && flash.textContent.trim()) toast(flash.textContent.trim(), 'ok');

    // A link that starts a slow page should also look like it registered.
    document.querySelectorAll('a[data-busy]').forEach(link => {
      link.addEventListener('click', () => link.classList.add('busy'));
    });
  });
})();
