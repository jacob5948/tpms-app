# TPMS watch

Passively capture tyre-pressure sensor (TPMS) transmissions with `rtl_433`, log
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

## Commands

| Command | What it does |
| --- | --- |
| `tpms serve` | Run capture and the web UI. `--no-radio` serves stored data only. |
| `tpms replay FILE.jsonl` | Ingest a recorded `rtl_433 -F json` capture. `--synthetic` generates one. |
| `tpms recluster` | Rebuild vehicle clusters. `--dry-run` reports without writing; `-v` shows edge weights. |
| `tpms aliases` | List sensors that are one transmitter decoded by several protocols. |
| `tpms diagnose` | Explain why sensors are or are not grouping, with the nearest misses. |
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

Most vehicles on a public road pass once and never return, so they can never
reach `min_cooccurrences`. When `clustering.single_pass` is on (the default),
sensors heard together in one pass that share a decoder and a comparable signal
level are grouped anyway, and the vehicle is marked **provisional**.

"Share a decoder" means their decoder *sets* overlap, counting duplicate
decodes. That matters: three wheels all decoded as Jansite can end up with
canonical models of Jansite, Citroen and Renault after duplicates are
collapsed, and comparing only the canonical model would split one car
into three. It is
promoted to confirmed if the same sensors are heard together again.

Turn `single_pass` off if you only care about vehicles that visit repeatedly.

### The known limitation

Two vehicles that **always** travel together are mathematically indistinguishable
from one vehicle by co-occurrence alone. Nothing in the data can separate them.
That is what the manual split control on the vehicle page is for.

### Manual overrides always win

Clustering never touches a sensor that is **pinned**, or that belongs to a
vehicle a human **named** or created. Moving a sensor by hand pins it
automatically, so your correction survives the next clustering run.

## Web UI

- **Live** — SSE feed of incoming readings and what is audible right now,
  updated in place without reloading the page.
- **Vehicles** — cards per vehicle with per-wheel pressure/temperature; detail
  pages with a pressure chart, appearance history, and rename/merge/split/pin.
- **Sensors** — every transmitter heard, with manual assignment.
- **Sensor detail** — one page per transmitter: every reading field, its bands,
  its sightings, its duplicate decodes, the last raw packet, and **what it was
  heard alongside**. Every mention of a sensor anywhere links here, so no table
  has to carry every column.
- **Events** — the appear / last-heard log, filterable, with CSV export.

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
and Events (and the `band` column in CSV exports) answers "315 or 433.92?" per
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
normalization bug can never lose data — re-import with `tpms replay`.

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
rtl_433 -f 315M -F json          # drive a car past, or squeeze a tyre valve
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
