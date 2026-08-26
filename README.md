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

### The known limitation

Two vehicles that **always** travel together are mathematically indistinguishable
from one vehicle by co-occurrence alone. Nothing in the data can separate them.
That is what the manual split control on the vehicle page is for.

### Manual overrides always win

Clustering never touches a sensor that is **pinned**, or that belongs to a
vehicle a human **named** or created. Moving a sensor by hand pins it
automatically, so your correction survives the next clustering run.

## Web UI

- **Live** — SSE feed of incoming readings and what is audible right now.
- **Vehicles** — cards per vehicle with per-wheel pressure/temperature; detail
  pages with a pressure chart, appearance history, and rename/merge/split/pin.
- **Sensors** — every transmitter heard, with manual assignment.
- **Events** — the appear / last-heard log, filterable, with CSV export.
- **Status** — receiver health, tuned frequency, packet rate, decoder breakdown.

## Configuration

See `config.example.yaml`; every key is commented. The settings you are most
likely to touch:

| Key | Default | Notes |
| --- | --- | --- |
| `radio.frequencies` | `[315M]` | Add `433.92M` for a second band (enables hopping). |
| `radio.gain` | `null` (AGC) | A fixed gain often beats AGC for weak TPMS bursts. |
| `radio.all_protocols` | `false` | `true` decodes everything, not just TPMS. Costs CPU. |
| `sessions.gap_seconds` | `120` | Must exceed the sensor transmit interval. |
| `clustering.min_support` | `0.6` | Raise it if unrelated vehicles are merging. |

Every raw `rtl_433` line is also archived to `raw/rtl433-YYYY-MM-DD.jsonl`, so a
normalization bug can never lose data — re-import with `tpms replay`.

## Deploying to a Pi

```bash
sudo useradd -r -G plugdev -d /opt/tpms tpms
sudo mkdir -p /opt/tpms && sudo chown tpms:plugdev /opt/tpms
# copy the project to /opt/tpms, create the venv, write config.yaml

sudo cp systemd/99-rtl-sdr.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger

sudo cp systemd/tpms.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now tpms
journalctl -u tpms -f
```

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

**Packets, but no vehicles.** Clustering needs several *separate* passes.
Check progress with `tpms recluster --dry-run -v`, which prints the edge weights
and how far each pair is from the thresholds.

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
