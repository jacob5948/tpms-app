# Working in this repo

TPMS capture, logging and vehicle correlation. `tpms/` is the program,
`tpms/web/` the UI, `tests/` the suite. `INFO_DESIGN.md` holds the UI rules and
the reasoning behind them; `README.md` is the user-facing documentation and is
expected to stay true.

## Running things

```bash
.venv/bin/pytest                      # the whole suite, no hardware needed
.venv/bin/pytest tests/test_browser.py # the pages, in a real browser
```

## UI changes are checked in a browser, by default

Anything touching `tpms/web/templates/`, `tpms/web/static/app.css` or a script
under `tpms/web/static/` gets driven in a real browser before it is called
done. Reading the served HTML proves nothing about the half of these pages that
only exists once script has run — charts drawn from fetched JSON, rows that
expand, the bulk bar that appears on the first tick, saves posted without a
reload. That is where the last few UI bugs actually lived.

- **Assert it** in `tests/test_browser.py`. The `serve` and `page` fixtures in
  `tests/conftest.py` run the real app over synthetic data; `page` fails the
  test on any console error, so a page that renders and does nothing cannot
  pass. Add the HTML-level assertion too where one is worth having.
- **Look at it** with `scripts/uishot.py` when the question is design rather
  than behaviour — spacing, whether a control reads as a control, how it holds
  up in the dark theme:

  ```bash
  python scripts/uishot.py /vehicles/1 -o /tmp/shot.png --click "button.expander"
  python scripts/uishot.py /events --dark --clip table
  ```

Both need `playwright install chromium` once. The browser tests skip themselves
when it is missing, so a green suite is not proof they ran — check that they
did before claiming a UI change is verified.

## House style

**Commit messages are plain.** Subject: what changed, stated directly — "Show
travel direction in the vehicle pass history", not an epigram about it. Body:
what was wrong, what the change does, anything a reader needs to know. No
aphorisms, no wordplay, no rhetorical build-up. If a sentence exists to sound
good rather than to inform, cut it.

Do not take the tone of existing commits as the model — the log drifted into
copywriting over several sessions and is being corrected, not continued.

Tests are named as sentences about the rule they defend.
