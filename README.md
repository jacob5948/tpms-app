# TPMS watch

Passively capture tire-pressure sensor (TPMS) transmissions with `rtl_433`, log
them to SQLite, correlate individual sensor IDs into the vehicles they belong
to, and track when each vehicle appears and goes quiet.

## What this can and cannot tell you

**Most TPMS sensors sleep when the wheel stops rolling.** They transmit roughly
every 30–60 s while moving, and many send nothing at all while parked. So:

- It is a good **drive-by detector**. Vehicles passing within range are logged
  reliably, with pressures, temperatures and timestamps.
- It is a poor **parked-car presence sensor**. A sensor going quiet means it
  stopped transmitting — the vehicle may have left, or may simply have stopped.

The UI reflects this everywhere: sightings end at **"last heard"**, never at an
inferred departure time.

**Frequency matters.** `rtl_433` defaults to 433.92 MHz (EU factory TPMS and most
aftermarket sensors), but North American factory TPMS — Toyota, Ford, GM, Honda,
Nissan — is on 315 MHz. This project defaults to **315 MHz**. You can switch
bands or hop between them in `config.yaml`, but hopping halves the dwell time per
band and will drop a real share of passes.

Finally: these are unencrypted broadcasts and receiving them is passive, but the
database this builds is a log of vehicle movements. Keep it on your LAN.

## Requirements

- An RTL-SDR dongle
- `rtl_433` — `brew install rtl_433` (macOS) or `apt install rtl-433` (Debian/Pi)
- Python 3.11+

The web UI has no build step. Its one front-end dependency,
[uPlot](https://github.com/leeoniya/uPlot) (MIT, ~15 KB gzipped), is vendored
into `tpms/web/static/vendor/` and served from there, so the Pi never needs to
reach a CDN.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e .

cp config.example.yaml config.yaml     # edit frequencies, gain, etc.
.venv/bin/tpms --config config.yaml serve
```

Then open <http://localhost:8080>.

### Try it without a dongle

The replay path runs recorded or synthetic data through the exact same ingest
pipeline as the radio, so you can see the whole UI working before any hardware
is involved:

```bash
.venv/bin/tpms replay --synthetic          # generate, ingest and cluster
.venv/bin/tpms serve --no-radio            # browse the result
```

## Which way was it going

Which wheels were heard says which side of the vehicle faced the receiver: the
near side has clear air to the antenna, the far side has the car in the way.

Label some wheels — `FL`, `FR`, `RL`, `RR`, or plain `L` / `R` when you know
the side but not whether it is the front or the rear — and the traffic log
grows a **Direction** column. Turning a side into a direction needs something
only you know, so name the two sides in `config.yaml`:

```yaml
direction:
  left: northbound
  right: southbound
```

Unnamed, the log reports "left side" / "right side" instead, which is as far as
the radio can honestly go.

The guess declines more often than it answers. A pass with no labelled wheels,
or one where both sides were heard at similar strength, shows nothing at all
rather than a coin flip. A call that rests on a level comparison, or on wheels
whose side is unknown, is marked with a `?` and carries its reasoning in the
tooltip.

### Confirm a pass, and let it label the wheels for you

That leaves the hard part: knowing which wheel is on which side in the first
place. The radio cannot tell you — a sensor announces an id and nothing else.
If you can see what went past, whether from a camera or a window, you can tell
it, and it will work the rest out.

Every pass row, in the log and on a vehicle's own page, has a **Seen** picker
carrying your two direction names. Confirm what actually went by. Nothing else
is asked of you.

A confirmation is worth more than any inference in this program, so it is
treated that way. It is anchored on a real sighting rather than on a derived
pass, which means changing `sessions.gap_seconds` — a setting that re-slices
history into different passes — cannot lose it. It can be corrected or cleared.
And it scores the guesswork: the **Direction** pill on a confirmed pass turns
green when the radio agreed with you and amber when it did not, so you can see
at a glance how far to trust that column on the passes nobody has confirmed.

Confirm a few each way and the **Wheel sides from confirmed passes** panel on
the vehicle page places the wheels, from two independent signals:

- **Which wheels went missing.** The far side is the one the car blocks, so a
  sensor heard on most of your entrances and few of your exits is on the side
  that faces the receiver as cars come in.
- **Which were louder.** Levels are taken relative to the loudest wheel *of the
  same pass*, so one close pass cannot outvote a dozen distant ones.

It shows its working — the counts and the dB gap are right there beside the
proposal, and the passes being counted are in the table above. It refuses to
answer below three confirmed passes each way, or when a wheel is heard equally
either way; "no call" is a real answer, and a wheel that this cannot place is
better left unlabelled than guessed at. **Apply these sides** writes the labels,
keeping the front-or-rear half of any corner label already set — nobody learned
that half from the radio, and this has no business overwriting it.

One thing it cannot know: which physical side is "left". That comes from your
`direction:` names. If you called the entering side `left` and the cars are in
fact showing you their right, every label comes out mirrored — consistently, and
the directions still read correctly, but `L` will mean the right-hand wheels.

## Settings

Everything in `config.yaml` is editable at <http://localhost:8080/settings>.
The page is generated from the config itself, so it is never out of step with
what the file accepts.

Saves take effect immediately — the log, the charts and the clock all re-read
their settings on every request. Two kinds of setting are read only at startup,
and for both the page says so and offers the restart:

- **Radio settings.** The receiver reads them when it launches `rtl_433`, so
  **Restart receiver** is enough; capture pauses for a second.
- **Web settings** (`web.host`, `web.port`). The server binds its address once,
  while the program is starting, so these need **Restart the service** — the
  whole process re-execs itself, and the reminder banner stays on every page
  until it does. The browser reconnects on its own unless you moved the port.

The file is rewritten in place, with its explanatory comments regenerated from
the same descriptions shown on the page, and the previous version kept beside
it as `config.yaml.bak`. Notes of your own are worth keeping in a separate
file, since a save rewrites the comments.

`database` is shown but not editable: the running program holds a connection
to the file it started with, so changing it needs an edit of the file and a
restart from the button above.

## Commands

| Command | What it does |
| --- | --- |
| `tpms serve` | Run capture and the web UI. `--no-radio` serves stored data only. |
| `tpms replay FILE.jsonl` | Ingest a recorded `rtl_433 -F json` capture. `--synthetic` generates one. |
| `tpms recluster` | Rebuild vehicle clusters. `--dry-run` reports without writing; `-v` shows edge weights. |
| `tpms aliases` | List sensors that are one transmitter decoded by several protocols. |
| `tpms diagnose` | Explain why sensors are or are not grouping, with the nearest misses. |
| `tpms ids` | Measure whether sensor IDs indicate which wheels pair up. `--explain` shows every pair. |
| `tpms purge Jansite` | Delete every sensor from one decoder. `--dry-run` first; `-y` skips the prompt. |
| `tpms export --since 7d -o out.csv` | Export the sighting log as CSV. |
| `tpms status` | Summarise the database from the terminal. |
| `tpms simulate -o capture.jsonl` | Write synthetic capture data to a file. |

### Which protocols get decoded

The TPMS decoder list is read from your installed rtl_433 at startup
(`rtl_433 -R help`), selecting decoders by name. Protocol numbers are **not**
stable across rtl_433 versions and older builds simply lack the higher ones, so
hardcoding them breaks against anything but the exact version they came from.
If the binary cannot be queried, the app decodes all protocols rather than risk
passing a number your build rejects. The Status page shows the exact command.

## How vehicle correlation works

Four sensors bolted to the same car are heard within seconds of each other, over
and over, across independent passes. That is the whole signal.

1. **Sightings.** Per sensor, readings arriving at least every `gap_seconds`
   (default 120 s) belong to one sighting. A longer silence starts a new one.
2. **Co-occurrence.** Two sensors heard within `window_seconds` (default 10 s)
   score a point — but **at most once per shared sighting**. A car idling in
   range for ten minutes therefore casts one vote, not sixty.
3. **Edges.** A pair links only if it co-occurred at least `min_cooccurrences`
   times (default 3) *and* `count / min(sightings_a, sightings_b)` clears
   `min_support` (default 0.6). The support term is what rejects two unrelated
   cars that happened to drive past together a couple of times.
4. **Components.** Connected components become vehicles. Anything larger than
   `max_cluster_size` (default 6) is assigned but flagged `needs review` in the
   UI, since an oversized component is nearly always a bad merge.

### Duplicate decodes come first

Several rtl_433 TPMS decoders will match the same RF burst with different
framing, so one physical sensor shows up two or three times under different
protocol names — `Jansite/6cd2eb3` and `Ford/6cd2eb33`, `Jansite/c20f14d` and
`Citroen/0f14dbd2`. Left alone these phantoms inflate the sensor list and, since
they co-occur perfectly by construction, cluster into vehicles that do not exist.

Two things identify them. First, only *different* decoders are ever merged:
wheels on one car share an OEM sensor type, so a same-decoder pair is two real
sensors, never one seen twice. Second, signal level — rtl_433 reports RSSI and
SNR per burst, and two decoders parsing the same burst report *exactly* the
same values at the same instant. Measured against real capture, every genuine
duplicate agreed to 0.0 dB on RSSI and SNR, while the nearest false candidate
was 2 seconds and 1.1 dB away. The tolerances in `config.yaml` are safety
margin, not expected spread — widening them starts merging real sensors that
happened to transmit at a similar moment and level.

Run `tpms aliases --explain` to see the actual deltas in your own data and what
accepted or rejected each pair.

Where rtl_433 supports it, capture also requests microsecond timestamps
(`-M time:utc:usec`), which pins two decodes of one burst together far more
tightly than whole seconds can.

Duplicates are folded into one canonical sensor before clustering.
`tpms aliases` lists what was merged.

### Vehicles seen only once

Some vehicles pass once and never return, so they can never reach
`min_cooccurrences`. Which ones depends entirely on where the receiver sits: on
a through road that is nearly all of them, while in a residential community it
is the deliveries and the visitors, and the cars that live there are heard
several times a day. When `clustering.single_pass` is on (the default), sensors
heard together in one pass that share a decoder and a comparable signal level
are grouped anyway, and the vehicle is marked **provisional**.

The rule only applies to a pair that has never had the chance to confirm --
where the rarer of the two has been heard fewer than `min_cooccurrences` times.
Past that, the pair has had its run, and a failure to coincide over twenty
sightings each is evidence of absence; falling back to a weaker test there
reads it as the opposite. It is the rarer sensor's count that decides, so a
weak wheel caught once still reaches the car it belongs to.

That distinction matters most where vehicles return. Signal level is the whole
of the fallback test once IDs are far apart, so on a saturating receiver --
where every strong sensor reports the same level -- one shared ten-second
window was enough to weld two unrelated cars together, permanently: nothing
ever deletes a co-occurrence, and the single-pass branch has no threshold that
time can push a pair back across.

"Share a decoder" means their decoder *sets* overlap, counting duplicate
decodes. That matters: three wheels all decoded as Jansite can end up with
canonical models of Jansite, Citroen and Renault after duplicates are
collapsed, and comparing only the canonical model would split one car
into three. It is
promoted to confirmed if the same sensors are heard together again.

Turn `single_pass` off if you only care about vehicles that visit repeatedly
— in a community where almost everything returns, the confirmed path carries
nearly all of it and this only ever groups the visitors.

### The known limitation

Two vehicles that **always** travel together are mathematically indistinguishable
from one vehicle by co-occurrence alone. Nothing in the data can separate them.
That is what the manual split control on the vehicle page is for.

### Manual overrides always win

### Sensor IDs as a second signal

Wheel sets appear to be programmed with near-consecutive IDs: in a 13-hour
capture every pair co-occurrence had grouped confidently also had neighbouring
IDs within its decoder — `Renault/f7b207` and `f7b209` differ by 2 — while
unrelated sensors were millions apart.

Two sensors from one decoder with IDs within `clustering.id_max_distance` are
therefore treated as one vehicle even when their signal levels differ more than
`single_pass_rssi_spread` allows, which is common for wheels on opposite sides
of a car. **It never groups anything on its own** — the pair must still have
been heard together, or every wheel set from one production run would merge
regardless of where it was heard.

The same signal flags clusters spanning several unrelated ID blocks, which
usually means two cars that drove past at the same moment rather than one
vehicle with six wheels.

Run `tpms ids` to check the idea against your own capture before trusting it.
It scores recall against pairs co-occurrence is already sure of, and a
false-positive rate against same-decoder pairs never heard together — and
declines to give a verdict at all until the sample is big enough to mean
something. Set `clustering.id_adjacency: false` if it does not hold up for you.

`id_max_distance` was measured on 32-bit IDs, and decoders do not all print
32-bit IDs: Renault writes six hex digits where Toyota writes eight, a space
256 times smaller. The same 65536 that is a thousandth of a percent of the
Toyota space covers 0.8% of the Renault one, so with fifty Renault sensors
heard, any one of them is better than a one-in-three shot to find an unrelated
"neighbour" — coincidence wearing the shape of evidence.

Each decoder's distance is therefore capped at the point where it would expect
`clustering.id_coincidence_limit` unrelated ID-near neighbours per sensor,
given how many of that decoder have been heard. A wide, sparse space keeps the
measured number; a narrow crowded one tightens to match, and tightens further
as the capture grows — which is right, because more sensors is more chances to
collide. The cap is on coincidence rather than on width directly, because
manufacturers allocate a wheel set a block of roughly the same absolute size
whatever their ID width: scaling by width would divide Renault's 65536 by 256
and cut through sets observed to span 1457. Consecutive IDs are never scaled
apart, whatever the density.

`tpms ids --sweep` prints the caps in force, scores several distances, and
scores the capture again with the cap off, so the default can be checked
rather than believed.

### Resident sensors

A sensor parked within range behaves nothing like passing traffic: on a real
capture two had been continuously audible for over ten hours while everything
else was heard for a minute or two. Because a resident is audible while *every*
car drives past, being heard together says nothing about them belonging
together — so residents are marked with a `resident` badge and every edge one
of them is part of must also *look* like one vehicle: a shared decoder, and a
neighbouring ID or a comparable signal level.

Repetition does not rescue such a pair, which is what this rule was got wrong
about at first. A pair is counted once per shared sighting, so a resident
scores one vote per passing car while support divides by that car's own two or
three sightings: three cars in an afternoon is `n=3` at support `1.00`, a
confirmed edge. With the guard covering only one-pass groupings, twelve parked
transmitters held 223 sensors from six decoders in a single cluster.

Shape rather than a ban, because a parked car's own wheels are residents too
and co-occurrence is the only evidence they will ever offer. Naming one pins it
out of the clusterer's way entirely.

Detection is a duty cycle: the share of its observed window the sensor was
audible, over at least `clustering.resident_min_span_seconds`. Passing traffic
scores near zero, a resident near one.

Clustering never touches a sensor that is **pinned**, or that belongs to a
vehicle a human **named** or created. Moving a sensor by hand pins it
automatically, so your correction survives the next clustering run.

## Web UI

Two words carry the whole UI: a **pass** is one vehicle going by, a **sighting**
is one transmitter being heard. A pass is its wheels' sightings merged.

- **Live** — what is audible right now, grouped by vehicle, above an SSE feed
  of incoming readings, both updated in place without reloading. Because the
  usual reason to watch this page is a car you can actually see, each group
  carries an inline name field and each ungrouped sensor an assign control:
  the moment to say what something is, is while it is still in front of you.
- **Vehicles** — cards per vehicle with per-wheel pressure/temperature. The
  tiles are filter toggles, so **Needs review** and **Provisional** are the
  worklist of what clustering is unsure about. Detail pages carry **comings and
  goings**, a pressure chart, pass history, and rename/merge/split/pin/unpin.
- **Sensors** — every transmitter heard, with manual assignment. Sort by
  **First heard**, or hit the **New today** tile, to see what turned up while
  you were out: four transmitters first heard the minute you got home is
  most of an identification.
- **Sensor detail** — one page per transmitter: every reading field, its bands,
  its sightings, its duplicate decodes, the last raw packet, and **what it was
  heard alongside**. Every mention of a sensor anywhere links here, so no table
  has to carry every column.
- **Log** — the traffic history, in two peer views over one filter:
  **vehicle passes** (the default: one row per car going by, expandable to the
  sightings behind it) and **sensor sightings** (the raw decoded rows). Both
  filter by time, vehicle or sensor, and export to CSV in the shape on screen.

### Correcting a grouping

Clustering guesses; you correct it. Everything it flags is reachable from the
tiles on Vehicles, and every correction is reversible:

- **Split** — tick several sensors on a vehicle's page and move them out into a
  vehicle of their own. This is the fix for an oversized or mixed-family
  cluster, which is almost always two cars that travel together.
- **Merge** — fold another vehicle into this one. Asks first: it reparents
  every sensor involved and the other vehicle's name does not come back.
- **Pin / unpin** — a manual placement is pinned so clustering cannot undo it.
  Unpinning hands the sensor back.
- **Hide** — for a transmitter that is real but not interesting, such as a
  neighbour's car parked in range. It leaves the sensor list, the live view,
  the activity charts and clustering, but keeps recording and keeps its own
  page. Hidden sensors are still listed behind the **Hidden** tile, and asking
  for one by name still answers. This is *not* deletion — that is `tpms purge`.

A vehicle's page opens with **comings and goings**: a strip showing when it was
audible — each block one pass, from the first wheel heard to the last —
above a count of passes per bucket. Two shapes of the same fact, because
neither works alone: a 90-second drive-by is sub-pixel across a month, and the
bucket counts that stay readable at that zoom have lost the individual visits.
The Passes table below lists the same intervals exactly.

The Live page opens with an **activity chart**: readings per bucket as bars,
and below it, on its own plot sharing the same window, how many distinct
transmitters were heard and how many sightings began — so one chatty resident
sensor cannot be mistaken for traffic. Two plots rather than two y axes on one:
where two scales line up is arbitrary, and a reader should not be invited to
read a relationship off it. Quiet buckets are drawn as zero rather than
skipped, because a gap in the capture is the thing worth seeing.

Every chart takes a cursor: hover to read values off it, drag to zoom into a
window, double-click to go back. Stacked plots share one cursor and one zoom.
The range buttons sit in a single row above the charts they scope and fetch a
wider window from the server rather than re-slicing what the page happened to
ship with, and a range only appears when there is enough history to fill it.
Every chart has a **Show as table** twin underneath, so no value is reachable
only by reading a colour off a line.

Every table sorts on a header click, and remembers your choice per page. Times
sort by their real timestamp rather than the words "8m ago", and blanks always
sink to the bottom. The live feed is deliberately excluded: new readings arrive
at the top, which no sort order could survive.
- **Status** — receiver health, tuned frequency, packet rate, decoder breakdown,
  and how many readings arrived on each band.

### Reading the clustering evidence

Each sensor page lists the sensors it was audible alongside, with the shared
sighting count and the **support** — the share of the rarer sensor's sightings
the two appeared in together. That is the exact number `clustering.min_support`
is tested against, so a grouping that looks wrong can be traced to the figure
that caused it rather than guessed at.

### Which band a sensor was heard on

Every reading carries the frequency `rtl_433` decoded it at, and every sighting
records the band it was heard on, so the **Band** column on Sensors, Vehicles
and the Log (and the `band` column in CSV exports) answers "315 or 433.92?" per
sensor. Measured frequencies scatter either side of the tuned band — 314.98 and
315.03 are the same 315 MHz sensor — so they are snapped to the nearest known
band before being counted; anything else is shown as measured.

This matters mainly when hopping: a sensor heard on both bands gets a `+1` pill
listing the split, and the Status page shows whether a second band is earning
its share of the hop cycle at all. Duplicate decodes are counted with their
canonical sensor, since an alias is the same RF burst on the same band.

## Configuration

See `config.example.yaml`; every key is commented. The settings you are most
likely to touch:

| Key | Default | Notes |
| --- | --- | --- |
| `radio.frequencies` | `[315M]` | Add `433.92M` for a second band (enables hopping). |
| `radio.gain` | `null` (AGC) | A fixed gain often beats AGC for weak TPMS bursts. |
| `radio.all_protocols` | `false` | `true` decodes everything, not just TPMS. Costs CPU. |
| `radio.exclude_protocols` | `["Jansite"]` | Decoder names to leave out. See below. |
| `sessions.gap_seconds` | `120` | Must exceed the sensor transmit interval. |
| `clustering.min_support` | `0.6` | Raise it if unrelated vehicles are merging. |
| `clustering.single_pass` | `true` | Group vehicles seen only once, marked provisional. |
| `clustering.id_adjacency` | `true` | Use near-consecutive sensor IDs as a second signal. |
| `clustering.id_coincidence_limit` | `0.02` | Per decoder, tighten the ID distance until coincidences are this rare. |
| `clustering.resident_duty_cycle` | `0.5` | Above this, a sensor is parked in range, not passing. |
| `aliases.min_share_ratio` | `0.5` | Lower it if duplicate decodes are not being merged. |

### Removing sensors a decoder already created

Excluding a protocol only stops new sensors appearing. To clear the ones
already recorded:

```bash
tpms purge Jansite --dry-run    # list what would go
tpms purge Jansite              # asks before deleting
```

The pattern is a case-insensitive substring of the decoder name, so `Jansite`
also matches `Jansite-Solar`. It deletes the sensors with their readings,
sightings and co-occurrence history, removes any vehicle left empty, and
re-runs clustering. **This is not reversible** — the `raw/` archive is the only
copy afterwards, and `tpms replay` can rebuild from it. Hence the dry run and
the confirmation prompt; `--yes` skips the prompt for scripts.

### Why Jansite is excluded by default

The `Jansite` decoder matches bursts that belong to other makers, so a single
passing car is decoded twice: once correctly, and once again as a Jansite
sensor with a different ID. Each of those is a phantom transmitter that the
duplicate detector then has to recognise and fold away. Excluding the decoder
stops them being created at all.

The trade-off: a shared decoder name is one of the signals single-pass grouping
uses, and Jansite was supplying it for some vehicles. Expect slightly fewer
provisional groups on first sighting, and cleaner sensor counts in exchange.
Set `exclude_protocols: []` to decode it again. Sensors already recorded stay
in the database; excluding the protocol only stops new ones appearing.

Every raw `rtl_433` line is also archived to `raw/rtl433-YYYY-MM-DD.jsonl`, so a
normalization bug can never lose data — re-import with `tpms replay` (which
reads the compressed `.jsonl.gz` form too).

## Housekeeping

Measured on a year of ordinary traffic — 6,000 readings a day, 2.19M rows — the
database reaches **786 MB**, and about two thirds of that is the raw JSON text
stored beside each reading. Nothing breaks at that size, but nothing gets
smaller on its own either, so the service tidies up once a day:

| What | Default | Why |
|---|---|---|
| Raw packet text | dropped after **7 days** | every line is also in `raw/`, so `tpms replay` can put it back |
| Readings | **kept forever** | the only setting here that really forgets something, so you opt in |
| `raw/` archives | **gzipped after 7 days** | JSON compresses about 10:1; today's file is never touched |
| `VACUUM` | **on** | SQLite never shrinks the file on delete |

Sightings, sensors, vehicles and the per-band totals are never pruned. They are
the summary of the readings at a fraction of the size, so a database trimmed to
a fortnight of packets still knows every vehicle it has ever heard.

On that same year of data: dropping raw text past a week takes 786 MB to
552 MB, and adding `readings_days: 90` takes it to **154 MB** — with every
sighting and every band total still there.

```bash
tpms prune --dry-run                # what the next run would do
tpms prune --readings-days 90       # trim harder than the config says, once
```

The Status page shows what the capture costs, what it is costing per day, and
when housekeeping last ran. Set `retention.run_daily: false` to do it only by
hand.

## Deploying to a Pi

```bash
git clone https://github.com/jacob5948/tpms-app.git
cd tpms-app
sudo ./scripts/install-pi.sh
```

The script is idempotent — re-run it to upgrade, and it leaves your
`config.yaml`, database and raw archive alone. It installs `rtl-433` and
`rtl-sdr`, blacklists the DVB-T kernel driver, installs the udev rules,
creates a `tpms` service user, builds the venv under `/opt/tpms`, and enables
the systemd unit. When it finishes it prints the URL and the log command.

**Reboot afterwards if the DVB driver was already loaded** — it cannot always
be unloaded from under a device that is in use.

Then edit `/opt/tpms/config.yaml` (frequency, gain, port) and
`sudo systemctl restart tpms`.

### Updating

From the checkout on the Pi, as the user who owns it (not with `sudo` — the
script asks for it where it needs it):

```bash
./scripts/update-pi.sh
```

It stops the service, fast-forwards the branch, copies the new code into the
prefix the service runs from, reinstalls dependencies only if `pyproject.toml`
moved, starts it again, and then checks the UI actually answers. If the
service does not stay up, both the checkout and the prefix are rolled back to
the commit that was running before it, so a bad pull cannot leave the Pi deaf
until you next look at it. `--stash` sets aside local edits, `--no-rollback`
leaves a failed update in place to debug, `--service NAME` points it at a
differently-named unit, `--prefix DIR` overrides where it deploys.

**The service does not run from your checkout.** The unit sets
`WorkingDirectory=/opt/tpms` and `ProtectHome=true`, so `/home` is not visible
to it at all; installing and updating both end by copying the source into the
prefix (`scripts/deploy.sh`, shared by both). Pulling on its own changes
nothing the service can see. The prefix records the commit it holds in
`/opt/tpms/.deployed`, and the updater redeploys whenever that disagrees with
your checkout — so a pull that happened without a deploy gets noticed rather
than mistaken for "already up to date".

### A word on the packaged rtl_433 version

`apt install rtl-433` gives you whatever your Debian release pinned:

| Release | Version |
| --- | --- |
| Pi OS 12 / bookworm | **22.11** (from 2022) |
| Pi OS 13 / trixie | 25.02 |
| upstream current | 26.07 |

This matters because **TPMS decoders are added continuously** — an old build
simply cannot decode some sensors, and its protocol numbers stop lower. The app
handles that automatically (it asks your binary which protocols it supports
rather than assuming), so an old build works fine; it just sees fewer vehicles.
Builds up to 25.02 also carry CVE-2025-34450, a stack overflow in packet
parsing — relevant here, since the input is radio traffic from strangers.

For the widest decoder coverage, build from source:

```bash
sudo apt install -y libtool libusb-1.0-0-dev librtlsdr-dev rtl-sdr \
                    build-essential cmake pkg-config
git clone https://github.com/merbanan/rtl_433.git
cd rtl_433 && mkdir build && cd build && cmake .. && make -j"$(nproc)"
sudo make install
```

Then set `radio.binary` in `config.yaml` if it did not land on `PATH`.

### Manual install

If you would rather not run the script, it is short and readable — the steps
are: install `rtl-433`, blacklist `dvb_usb_rtl28xxu`, copy
`systemd/99-rtl-sdr.rules` to `/etc/udev/rules.d/`, create a service user in
the `plugdev` group, build a venv, copy `systemd/tpms.service` to
`/etc/systemd/system/`, then `systemctl enable --now tpms`.

Set `web.host: 0.0.0.0` to reach the UI from elsewhere on the LAN.

## Stopping it

`Ctrl+C` in the terminal, or `sudo systemctl stop tpms` under systemd. Note the
unit file sets `Restart=always`, so killing the process on the Pi just brings it
back — use `systemctl stop`.

rtl_433 runs in its own process group, so Ctrl+C reaches only this program,
which then shuts the receiver down deliberately. Nothing is lost on exit:
readings are committed as they arrive, and open sightings close at their last
reading on the next start.

## Troubleshooting

**"rtl_433 exited; restarting in Ns".** The log prints rtl_433's own output
and, for known failures, the fix. The **Status** page shows the same under
"Why the receiver stopped". Common causes:

- *A long protocol table in the output.* rtl_433 was passed a `-R` protocol
  number it does not know, so it printed its table and quit. This should no
  longer happen — the TPMS protocol list is discovered from your binary at
  startup rather than hardcoded — but `radio.all_protocols: true` disables
  `-R` entirely if you hit it.

- `No supported devices found.` — the dongle is not plugged in, or not passed
  through to the container/VM.
- `usb_claim_interface error -6` / `Failed to open rtlsdr device` — something
  else has the dongle. On a Pi this is almost always the DVB-T kernel driver:

  ```bash
  echo blacklist dvb_usb_rtl28xxu | sudo tee /etc/modprobe.d/blacklist-dvb.conf
  sudo reboot
  ```

  If it is not the kernel driver, check for another `rtl_433`/SDR process
  holding the device, and that the service user is in the `plugdev` group with
  `systemd/99-rtl-sdr.rules` installed.

**No packets at all.** Confirm the dongle and decoding by hand first:

```bash
rtl_433 -f 315M -F json          # drive a car past, or squeeze a tire valve
```

If that is silent, the problem is hardware, gain, antenna or frequency — not
this program. A quarter-wave whip for 315 MHz is about 23 cm.

**Packets, but no vehicles.** Run `tpms diagnose`. It reports how many sensors
are really duplicate decodes, how many have only ever been heard once, and for
each co-occurring pair exactly what is keeping it from grouping.

**Sensors clustering that shouldn't.** Raise `clustering.min_support` toward
0.8, or `min_cooccurrences`, then `tpms recluster`.

**"rtl_433 not found on PATH".** The Status page shows the exact command being
run; install `rtl_433` or set `radio.binary` to its full path.

## Tests

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

No hardware required — the suite drives the real pipeline with synthetic data,
including a scenario with a decoy sensor that must *not* get absorbed into a
neighbouring vehicle's cluster.

### The UI is tested in a browser

Half of what these pages do only exists once script has run: the charts are
drawn from fetched JSON, pass rows expand, the bulk bar appears on the first
tick, and saves are posted without leaving the page. A string in the HTML says
nothing about any of it, so `tests/test_browser.py` drives the real thing with
Playwright. Each of those tests also fails on a console error, which is how a
page that renders and quietly does nothing gets caught.

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/playwright install chromium   # once
.venv/bin/pytest tests/test_browser.py
```

They skip themselves, with the command to fix it, when playwright or its
browser is missing — the rest of the suite still runs on a machine without
either. **Any change to a template, to `app.css` or to a script under
`static/` belongs in that file**, alongside whatever HTML-level assertion the
change deserves.

For looking rather than asserting — layout, spacing, whether a control reads as
a control — there is a screenshot tool over the same synthetic data:

```bash
python scripts/uishot.py /vehicles/1 -o /tmp/vehicle.png
python scripts/uishot.py /events --click "button.expander" --clip table
python scripts/uishot.py / --dark --full          # the dark theme, whole page
python scripts/uishot.py /status --db tpms.db     # your real database instead
```
