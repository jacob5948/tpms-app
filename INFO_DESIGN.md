# Information design

The rules this UI is built to. Each one exists because it was got wrong once,
and most are guarded by `tests/test_design_rules.py` so a template edit cannot
quietly undo them.

The program's subject is **vehicles going past a fixed point**. Everything
below follows from that, and from the one hard physical constraint: TPMS
sensors sleep when the wheel stops, so a sensor going quiet means it stopped
transmitting — not that the vehicle left.

## The three nouns

There are exactly three, and they are named the same way everywhere:

| Noun | What it is | Where it lives |
|---|---|---|
| **Sensor** | One transmitter. Identity is `model` + `id` together, because raw IDs collide between protocols. | `/sensors`, `/sensors/{pk}` |
| **Vehicle** | Sensors repeatedly heard together. A guess the clusterer makes and a human corrects. | `/vehicles`, `/vehicles/{pk}` |
| **Pass** / **Sighting** | A **sighting** is one sensor being heard continuously. A **pass** is one vehicle going by: its wheels' sightings merged. | `/events` |

**Two words, used consistently.** A pass belongs to a vehicle; a sighting
belongs to a sensor. These were once called "events", "sightings" and
"appearances" across three pages with three column sets, which made one object
read as three unrelated things. Do not introduce a fourth word.

## Rules

**Vehicles lead, sensors stay reachable.** The log opens on passes, because a
car going by is one event and not four. But the raw sighting view is a peer —
a tab beside it, not a debug mode — because matching a car you watched pass to
the transmitters audible at that moment is done against unmerged rows. Never
demote the sighting view to a hidden detail.

**The merge rule is defined once.** `queries.merge_runs` decides where one pass
ends and the next begins; `merge_intervals` and `vehicle_passes` both build on
it. Two implementations would drift, and the vehicle page and the log would
disagree about the same afternoon.

**Nothing is inferred about departure.** Sightings and passes end at *last
heard*. There is no "left at" anywhere, in the schema or on screen.

**The one guess is labelled as one.** Direction is the only thing the program
infers rather than reports, and it earns that by never overstating itself. It
rests on an absence -- the wheels that were *not* heard -- so it declines more
often than it answers: nothing labelled, both sides heard at similar strength,
or no levels to compare all produce an empty cell rather than a coin flip. A
reading nothing contradicts is shown firm; one resting on a level comparison
or on wheels whose side is unknown carries a "?" and a quieter style, and
every one of them puts its basis in the tooltip. An inference the reader
cannot audit is worse than none, because it reads as a measurement.

**A side is not a direction.** The radio can only know which side of a vehicle
faced it. Which way that points is a fact about the road, which only the person
who owns the receiver knows, so `direction:` in the config names the two sides
and the program never guesses them. Unnamed, the UI says "left side" and stops
-- the honest half of the answer. Direction is computed at read time from the
wheel labels as they are now, never stored: a stored guess goes stale the
moment someone corrects a wheel.

**Every mention of a sensor looks the same and links to the same place.**
`_macros.html` owns that rendering — `sensor_link`, `sensor_cell`,
`sensor_table`, `when`. A sensor once appeared on five pages with five column
sets and no way to click through. If a page needs a sensor rendered, it calls
the macro.

**One zone, named where it can be read.** Readings are stored as epoch
seconds; `timezone:` in the config decides how every one of them is written
out — tables, chart axes, CSV, and the dates typed into filter boxes. The
charts label their own axes, so they are told the zone too: an axis in the
reader's zone above a table in the receiver's is two clocks on one page. The
Status page says which zone is in force.

**One timestamp idiom.** Relative text for reading, ISO in `title` for
precision, epoch in `data-sort` for ordering. Use `m.when()`. Three pages once
showed raw ISO instead, so reading across them meant switching formats.

**Hiding is not deleting.** `sensors.ignored` keeps a known-irrelevant
transmitter out of the lists, the live view, the activity charts and
clustering. It keeps recording, keeps its own page, and comes back on request.
Destroying data is `tpms purge`, and it stays on the CLI. Hidden rows are still
rendered into `/sensors` and held back by the filter, so they can be recovered
without knowing a URL. Asking for a hidden sensor *by name* — `?sensor=N` —
always answers: hidden means kept out of what you browse, not out of what you
ask for.

**A flag clustering sets, clustering must be able to clear.** `provisional`
is only rewritten for the vehicles a run still owns — and naming a vehicle,
or pinning every wheel, is exactly what takes it out of that set. The flag
therefore has to be dropped at the moment a vehicle passes into a person's
hands, or it becomes permanent on the vehicles the user has curated most.

**Corrections must survive the next clustering run.** Any manual placement
pins the sensor. Anything that pins must also be un-pinnable from the UI: a
one-way door is not a correction loop. And every action that pins says so in
the flash it returns -- "moved to X and pinned there", not just the move. A
flag the user did not ask for and is not told about is most of why pinning
reads as arbitrary: the sensor comes back wearing a pill nothing accounted
for.

**A control that carries its whole instruction in the button must not be
disabled before the browser reads it.** `pinned=1` and `ignored=0` live in the
pressed button's own name/value and nowhere else. The entry list is built
after the `submit` event and skips disabled controls, the submitter included,
so the busy-state disable in `markBusy` silently emptied the POST and every
one of these controls answered "Nothing to change." The server tests posted
the field directly and stayed green throughout. `markBusy` now copies the
pair into a field of its own first, and `test_a_busy_button_still_sends_its_own_name`
guards the order.

**One selection, one bar of actions.** Anything that acts on a set of rows
reads the ticks and lives in a bar under the table, sharing one form. The
vehicle page once put a select on every row carrying every vehicle in the
program, defaulting to "stay here" — a non-action — with "split" and
"unassign" mixed in among the destinations. A per-row control is for what is
true of that row alone: on that table, only pinning.

The bar arrives with the selection and wears its accent, rather than standing
there permanently under a label reading "Tick the sensors to act on:". A
checkbox column already says the rows can be ticked, so the label was a
sentence for something the controls had already said; what it could not say
was which tier of control it belonged to, sitting in the same panel as a
"Set" and a "Pin" on every row. Appearing on the first tick, on the same
tinted ground as the ticked rows, says both. It is hidden from script and
rendered either way, so with JS off every action stays reachable.

**A bar of actions sits inside the set it acts on.** The tick, the table and
the buttons that read the ticks are one object, so they share one panel and
the bar is its footer. Rendered as a sibling it had no ground of its own and
stood equidistant between the panel above and the panel below, which made it
read as the heading of the wrong one.

**A refusal returns the page, not an error.** The bulk handlers live under
`/api/`, and that path answers with JSON, so a browser form posted with
nothing ticked used to land on a bare `{"detail": ...}` — no shell, no nav,
and every tick just made gone. A rejected mutation is the page saying no: it
redirects back with the reason, and the flash carries whether it is an outcome
or a refusal so a turned-down action cannot arrive coloured like a save. Only
a request that asked for JSON gets a 400.

**Buttons weigh what they cost.** Three tiers, no more: `primary` for the
thing the page is for, the default for an ordinary action, `ghost` for a
control that repeats once per row. `danger` is for the one action that
destroys something. Eleven identical grey buttons in one region is not a
hierarchy, and the loudest control on the page should not be the one that
changes a name.

**Reshaping vehicles asks first.** Merge and split reparent every sensor
involved and cannot be undone in one click, so they carry `data-confirm`.
Labelling a wheel does not, because it changes nothing else — and must not:
the label form and the move form are separate forms for exactly this reason.

**A closed set is chosen, never typed.** There are seven wheel positions and
direction reads no others, so the control is a `select`, grouped, with every
position and its meaning visible before anything is clicked. It was a text box
with a `datalist`: it looked like free text, its options only appeared once the
box was clicked, and what you clicked -- "front left" -- was not what it left
behind -- "FL" -- so the control appeared to change the answer after it was
given. A picker with one field also saves on change; the "Set" button beside it
is the no-script fallback and is hidden from the script that replaces it. A
label already stored outside the set is kept as an option of its own, because
opening a page must never silently offer to erase data. And because a car has
one front left, the picker marks the positions the vehicle's other wheels
already wear.

**Flagged work gathers into a queue.** `needs_review` and `provisional` are
filter toggles on `/vehicles`, so the tiles *are* the worklist. A flag with no
way to gather it is a flag nobody acts on.

**Two scales never share a plot.** Where two y-axes line up is arbitrary, and
reading a correlation off that alignment reads something the data never said.
Use stacked facets with a synced cursor. Every chart also carries a table twin.

**Every facet carries its title.** Stacked plots in one panel are one figure,
but an untitled one runs into the next: the caption under the first reads as
the heading of the second. `tpmsChart` draws the `label` every caller already
passed. The vehicle page's plots were also filed under "comings and goings",
a fourth name for a pass — see the three nouns above.

**The page states what a thing is, never why the program does it that way.**
A sensor's identity is model + ID; *why* raw IDs collide between protocols is
in the README, and re-reading it on every page load is not what the Sensors
page is for. Prose on screen has three jobs — warn before something
irreversible, state a truncation, tell an empty page what to do next — and a
control that names its own action does not also get a paragraph telling you to
press it. Every `<h1>` and most `<h2>`s once carried a paragraph, and several
facts were written out in three places at once: on the page, in the tooltip of
the pill that carried the flag, and again in the confirm dialog. The rule below
is the case where this was caught first; it applies to the whole UI.

**A flag most rows share is explained once.** Nearly every vehicle is
provisional, and the review reason is nearly always the same one, so a
paragraph per card came out as one sentence repeated down the whole grid,
dwarfing the readings the card exists to show. The card carries the flag, the
reason as a tooltip, and the action the flag calls for; the tile hint and the
vehicle's own page carry the prose.

**Range controls sit above the charts they scope**, and one range row governs
all facets of one window — two views of one window must not be able to disagree
about which window that is.

**Nothing caches a setting.** Every read of the config goes to
`service.config` at the moment it is needed. `gap`, `direction_names` and
`rssi_margin` were once locals bound when the app was built, which was
harmless while only a restart could change a config -- and became a bug the
moment the Settings page could change one, because a saved value would reach
the file and the service and still not reach the log. A page that appears to
do nothing is the worst outcome of a save, so the two template globals are
callables (`{{ timezone() }}`) rather than values: a Jinja macro cannot see
the render context, so a called global is the one shape that works from inside
`_macros.html` as well as from a page.

**A config the program would not load must never reach the file.** The
Settings page parses and validates every box before assigning any, then writes
the file from what the process actually holds rather than from the form. Half
a save is a config that fails to load on next start, from a UI that reported
success. A refusal names the box it came from -- there are forty on that page
and "invalid literal for int()" identifies none of them -- and leaves the file
byte-identical.

**A rewritten config still explains itself.** The Settings page rewrites
`config.yaml` in place, and every knob in that file has a reason worth more
than its number. So the prose lives beside the defaults in `config.py` and is
re-emitted on every save, and the previous version is kept as `.bak` because
comments regenerated from code cannot recover a note someone typed by hand.

**A setting the running process cannot adopt is shown, not offered.**
`database` is the whole of that set: every component was handed a connection
to the old file at startup, so a box for it would change where the *next* run
looks and nothing about this one. It renders as text. A control that silently
does nothing until a restart is worse than no control.

**A restart is offered where the setting that needs one is saved.** Most
settings take effect on the next request, so the few that cannot must not look
the same. The section says it is read at startup, the save says which values
are waiting, and a banner carrying the button stays on every page until the
service restarts — a reminder that vanishes on the next click is a setting
that is quietly not in force. Restarting is an exec of this same program, not
an exit: exiting is a restart under systemd and a shutdown in a terminal, and
one button must not mean two things.

**Errors are pages.** A stale bookmark or a mistyped filter renders the normal
shell with nav, not raw JSON. A bad value in a filter box is a 400 that blames
the box, never a 500.

**Truncation is stated.** A capped list says how much it is showing of what,
and offers the export. The screen and the CSV apply the same filters and the
same view.

**Progressive enhancement.** The four scripts (`sort`, `filter`, `forms`,
`chart`) are attribute-driven, idempotent, and re-runnable over swapped-in
HTML. Without JS every page still works: the sightings behind a pass are
rendered and merely hidden, confirmations fall through to the server, and the
server remains the thing that decides.
