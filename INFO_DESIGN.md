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
one-way door is not a correction loop.

**One selection, one bar of actions.** Anything that acts on a set of rows
reads the ticks and lives in a bar under the table, sharing one form. The
vehicle page once put a select on every row carrying every vehicle in the
program, defaulting to "stay here" — a non-action — with "split" and
"unassign" mixed in among the destinations. A per-row control is for what is
true of that row alone: on that table, only pinning.

**Reshaping vehicles asks first.** Merge and split reparent every sensor
involved and cannot be undone in one click, so they carry `data-confirm`.
Labelling a wheel does not, because it changes nothing else — and must not:
the label form and the move form are separate forms for exactly this reason.

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

**A flag most rows share is explained once.** Nearly every vehicle is
provisional, and the review reason is nearly always the same one, so a
paragraph per card came out as one sentence repeated down the whole grid,
dwarfing the readings the card exists to show. The card carries the flag, the
reason as a tooltip, and the action the flag calls for; the tile hint and the
vehicle's own page carry the prose.

**Range controls sit above the charts they scope**, and one range row governs
all facets of one window — two views of one window must not be able to disagree
about which window that is.

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
