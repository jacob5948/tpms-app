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

    /* Carry the pressed button's own name/value in a field of its own before
     * disabling it.
     *
     * This used to be written the other way round, under a comment claiming
     * the browser had already serialised the form by the time submit fires.
     * It has not: the entry list is built *after* the submit event, and
     * building it skips disabled controls -- the submitter included. So
     * disabling the button here dropped the button's own pair.
     *
     * For most forms that cost nothing, because their instruction sits in
     * real inputs. But pin, unpin, hide and show carry the whole of what
     * they are asking for in the button: `pinned=1`, `ignored=0`. Those
     * posted an empty body, and the handler, seeing no field it recognised,
     * answered "Nothing to change." -- so the entire pinning system was
     * dead with JS on and worked only with JS off. */
    let carried = null;
    if (button.name) {
      carried = document.createElement('input');
      carried.type = 'hidden';
      carried.name = button.name;
      carried.value = button.value;
      form.appendChild(carried);
    }

    button.disabled = true;
    button.classList.add('busy');
    // The button's own wording wins: one form can carry two actions, and
    // "Splitting..." on the button that moves is a lie.
    button.textContent = button.dataset.busy || form.dataset.busy || 'Saving…';
    return () => {
      // Only the async path restores; a full-page submit never comes back
      // here. Removing it matters so a second submit cannot send it twice.
      if (carried) carried.remove();
      button.disabled = false;
      button.classList.remove('busy');
      button.textContent = label;
    };
  }

  /* Keep the wheel pill beside the sensor name in step with the field that
   * just changed it -- without this the async save is invisible on the row.
   *
   * The row forms on the vehicle page are declared outside the table, because
   * HTML forbids nesting them in the one that wraps it, and their fields reach
   * them by `form=`. So the row is found through a field the form owns, never
   * by walking up from the form: closest() from a form that lives at the foot
   * of the page finds no row and no panel, and the update silently did
   * nothing. */
  function updateWheelPill(form, label) {
    const owned = form.id && document.querySelector('[form="' + form.id + '"]');
    const from = owned || form;
    const scope = from.closest('tr') || from.closest('.panel');
    const slot = scope && scope.querySelector('[data-wheel-slot]');
    if (!slot) return;
    slot.querySelectorAll('.pill.wheel-label').forEach(p => p.remove());
    if (!label) return;
    const pill = document.createElement('span');
    pill.className = 'pill wheel-label';
    pill.textContent = label;
    slot.querySelector('a')?.after(document.createTextNode(' '), pill);
  }

  document.addEventListener('submit', function (event) {
    const form = event.target;
    if (!form.matches('form') || form.method.toLowerCase() !== 'post') return;

    /* The button that was pressed, where the browser tells us: one form can
     * carry several actions, and busying the first button while a different
     * one runs points at the wrong thing. */
    const button = event.submitter
      || form.querySelector('button[type="submit"], button:not([type])');

    /* Preconditions first. A bulk action with nothing ticked used to ask
     * "move the ticked sensors?", take yes for an answer, and only then be
     * turned down by the server -- a question about a set that was empty when
     * it was asked. The server still refuses; this just stops the asking. */
    const needs = button && button.dataset.needs;
    if (needs && !form.querySelector(needs)) {
      event.preventDefault();
      toast(button.dataset.needsMessage || 'Nothing selected.', 'err');
      return;
    }

    /* Merging and splitting reparent every sensor on a vehicle and cannot be
     * undone with one click, so they ask first. The question belongs to
     * whichever action was pressed, falling back to the form's. Without JS
     * the submit goes through as before -- the server still decides. */
    const confirm = (button && button.dataset.confirm) || form.dataset.confirm;
    if (confirm && !window.confirm(confirm)) {
      event.preventDefault();
      return;
    }

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
      .then(response => response.ok ? response.json()
                                    : response.json().then(body => Promise.reject(body)))
      .then(data => {
        toast(data.message || 'Saved.', 'ok');
        if ('wheel_label' in data) updateWheelPill(form, data.wheel_label);
        form.dispatchEvent(new CustomEvent('tpms:saved', { detail: data, bubbles: true }));
      })
      // A refusal answers in JSON and says why; a dead connection throws an
      // Error whose message is about fetch, not about the save.
      .catch(body => toast(
        (body && !(body instanceof Error) && body.message)
          || 'Could not save. Is the receiver still running?', 'err'))
      // Whatever the outcome, the form is no longer in flight. Row controls
      // that show their own busy state need the end of it as well as the
      // start, and only the success path fires tpms:saved.
      .finally(() => {
        if (done) done();
        form.dispatchEvent(new CustomEvent('tpms:done', { bubbles: true }));
      });
  });

  /* The tick and the bar under it.
   *
   * Nothing on screen used to connect the two: the checkboxes changed no
   * appearance, the bar said "with the ticked sensors" whether or not any
   * were, and pressing an action with an empty set was answered by the
   * server.
   *
   * The bar now stays out of the way until there is a selection to act on.
   * It carried a standing instruction -- "Tick the sensors to act on:" --
   * which is a sentence for something the checkbox column already says by
   * being there. Appearing on the first tick, in the same accent the ticked
   * rows wear, is the same information without the sentence.
   *
   * All of this is decoration over a form that still works without it: the
   * bar is in the HTML and only hidden from here, so with JS off every
   * action is reachable, and the server refuses the same cases either way. */
  function wireSelection(form) {
    const boxes = Array.from(form.querySelectorAll('input[type="checkbox"][name="sensor"]'));
    if (!boxes.length) return;
    const count = form.querySelector('[data-tick-count]');
    const bar = form.querySelector('[data-tick-bar]');
    const all = form.querySelector('[data-tick-all]');
    const gated = Array.from(form.querySelectorAll('[data-needs]'));
    const gates = Array.from(form.querySelectorAll('[data-gate]'));

    /* What each button says when it is simply ready. The gating below writes
     * a reason into `title` while an action is unavailable, and clearing that
     * to '' on the way out would erase whatever the template put there. */
    gated.forEach(button => {
      if (button.dataset.readyTitle === undefined) {
        button.dataset.readyTitle = button.title;
      }
    });

    function sync() {
      const ticked = boxes.filter(b => b.checked).length;
      if (count) {
        count.textContent = ticked + ' selected';
      }
      if (bar) bar.hidden = !ticked;
      if (all) {
        all.checked = ticked === boxes.length;
        all.indeterminate = ticked > 0 && ticked < boxes.length;
      }
      gated.forEach(button => {
        // Splitting every sensor is a rename, not a split, and the server has
        // always said so -- say it here instead of after the click.
        const whole = button.dataset.needsRemainder !== undefined
                      && ticked === boxes.length;
        const gate = button.dataset.gatedBy && form.querySelector(button.dataset.gatedBy);
        const open = !gate || gate.value;
        button.disabled = !ticked || whole || !open;
        button.title = !ticked ? 'Tick a sensor above first'
          : whole ? 'That is every sensor \u2014 leave at least one wheel here'
          : !open ? 'Choose where to move them first'
          : button.dataset.readyTitle;
      });
    }

    boxes.forEach(b => b.addEventListener('change', sync));
    gates.forEach(g => g.addEventListener('change', sync));
    all?.addEventListener('change', () => {
      boxes.forEach(b => { b.checked = all.checked; });
      sync();
    });
    sync();
  }

  /* A picker with one field saves itself.
   *
   * The wheel position was a text box beside a "Set" button, and pressing it
   * was a second action for a choice that was already made -- the value the
   * row will keep is the one now showing in the select. So changing it
   * submits, and the toast reports it; the button stays in the HTML and is
   * hidden from here, so with JS off the field is still submittable.
   *
   * The form is submitted *through* the button rather than on its own, so the
   * submit carries a submitter and markBusy has something to make busy. */
  function wireInstantSave(select) {
    const form = select.form;
    if (!form || select.dataset.wired) return;
    select.dataset.wired = '1';
    const fallback = select.id
      && document.querySelector('[data-fallback-for="' + CSS.escape(select.id) + '"]');
    if (fallback) fallback.hidden = true;
    select.addEventListener('change', () => {
      /* The busy state has to live on the field, because the button that
       * carried it is now hidden -- and it is a class, never `disabled`: the
       * entry list skips disabled controls, and this control *is* the value
       * being sent. */
      select.classList.add('busy');
      if (fallback) form.requestSubmit(fallback);
      else form.requestSubmit();
    });
    form.addEventListener('tpms:done', () => select.classList.remove('busy'));
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form[data-selection]').forEach(wireSelection);
    document.querySelectorAll('select[data-save-on-change]').forEach(wireInstantSave);

    // Whatever the last mutation did, reported on the page it landed on.
    const flash = document.getElementById('flash');
    if (flash && flash.textContent.trim()) {
      toast(flash.textContent.trim(), flash.dataset.kind || 'ok');
    }

    // A link that starts a slow page should also look like it registered.
    document.querySelectorAll('a[data-busy]').forEach(link => {
      link.addEventListener('click', () => link.classList.add('busy'));
    });
  });
})();
