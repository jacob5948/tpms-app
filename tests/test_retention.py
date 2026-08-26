"""Housekeeping: what it drops, what it must never drop, and the rollup that
lets the Sensors page survive a large capture."""

import gzip
import time

import pytest

from tpms import queries as q
from tpms.config import Config, RetentionConfig
from tpms.db import Database
from tpms.models import Reading
from tpms.retention import archive_date, human_bytes, run as run_retention
from tpms.service import Service

DAY = 86400.0


def _reading(sensor_id: str, ts: float, raw: str = '{"id":"x"}') -> Reading:
    return Reading(
        model="Toyota-TPMS", sensor_id=sensor_id, ts=ts, rssi=-20.0,
        pressure_kpa=240.0, freq_mhz=315.01, raw=raw,
    )


@pytest.fixture
def aged(ingestor, db):
    """Readings a fortnight old and readings from today."""
    now = time.time()
    for age_days in (14, 13, 10, 2, 0):
        ingestor.ingest(_reading("aaa", now - age_days * DAY))
    return db, now


# -- raw text -------------------------------------------------------------

def test_old_raw_text_is_dropped_and_recent_kept(aged):
    db, now = aged
    report = run_retention(db, RetentionConfig(raw_days=7), now=now)
    assert report.raw_dropped == 3
    kept = db.query("SELECT ts FROM readings WHERE raw IS NOT NULL")
    assert len(kept) == 2
    # The readings themselves stay; only their archived text goes.
    assert db.query_one("SELECT COUNT(*) n FROM readings")["n"] == 5


def test_a_dry_run_changes_nothing(aged):
    db, now = aged
    report = run_retention(db, RetentionConfig(raw_days=7), dry_run=True, now=now)
    assert report.raw_dropped == 3
    assert db.query_one("SELECT COUNT(*) n FROM readings WHERE raw IS NOT NULL")["n"] == 5


def test_running_twice_is_a_no_op_the_second_time(aged):
    db, now = aged
    run_retention(db, RetentionConfig(raw_days=7), now=now)
    assert run_retention(db, RetentionConfig(raw_days=7), now=now).raw_dropped == 0


# -- readings -------------------------------------------------------------

def test_readings_are_kept_unless_asked_for(aged):
    db, now = aged
    assert run_retention(db, RetentionConfig(), now=now).readings_deleted == 0
    assert db.query_one("SELECT COUNT(*) n FROM readings")["n"] == 5


def test_deleting_readings_keeps_the_summary(aged):
    """Sightings, sensor totals and band counts are the history; they survive."""
    db, now = aged
    before_sightings = db.query_one("SELECT COUNT(*) n FROM sightings")["n"]
    before_bands = q.sensor_bands(db, db.list_sensors()[0].pk)

    report = run_retention(db, RetentionConfig(readings_days=7), now=now)

    assert report.readings_deleted == 3
    assert db.query_one("SELECT COUNT(*) n FROM readings")["n"] == 2
    assert db.query_one("SELECT COUNT(*) n FROM sightings")["n"] == before_sightings
    assert q.sensor_bands(db, db.list_sensors()[0].pk) == before_bands
    assert db.list_sensors()[0].reading_count == 5


def test_a_policy_that_would_empty_the_database_is_refused(aged):
    db, now = aged
    report = run_retention(db, RetentionConfig(readings_days=0.5), now=now)
    assert report.readings_deleted == 0
    assert report.skipped


# -- the raw archive ------------------------------------------------------

def test_old_archives_compress_and_ancient_ones_go(db, tmp_path):
    now = time.time()

    def name(age_days):
        stamp = time.strftime("%Y-%m-%d", time.gmtime(now - age_days * DAY))
        return f"rtl433-{stamp}.jsonl"

    for age in (0, 3, 30, 400):
        (tmp_path / name(age)).write_text('{"model":"Toyota-TPMS"}\n' * 100)

    report = run_retention(
        db,
        RetentionConfig(archive_gzip_days=7, archive_delete_days=180),
        archive_dir=tmp_path,
        now=now,
    )
    assert (report.archives_compressed, report.archives_deleted) == (1, 1)
    # Today's file is still being appended to; it must not be touched.
    assert (tmp_path / name(0)).exists()
    assert (tmp_path / name(3)).exists()
    assert not (tmp_path / name(30)).exists()
    compressed = tmp_path / (name(30) + ".gz")
    assert gzip.open(compressed, "rt").read().count("Toyota") == 100


def test_files_that_are_not_archives_are_left_alone(db, tmp_path):
    (tmp_path / "notes.txt").write_text("mine")
    run_retention(
        db, RetentionConfig(archive_delete_days=1), archive_dir=tmp_path, now=time.time()
    )
    assert (tmp_path / "notes.txt").exists()


@pytest.mark.parametrize(
    "name,expected",
    [
        ("rtl433-2026-08-26.jsonl", True),
        ("rtl433-2026-08-26.jsonl.gz", True),
        ("rtl433-2026-13-99.jsonl", False),
        ("something-else.jsonl", False),
    ],
)
def test_archive_names(name, expected):
    assert (archive_date(name) is not None) is expected


# -- the band rollup ------------------------------------------------------

def test_band_counts_track_readings_as_they_arrive(ingestor, db):
    for ts in (1000, 1060, 1120):
        ingestor.ingest(_reading("aaa", ts))
    ingestor.ingest(Reading(model="Toyota-TPMS", sensor_id="aaa", ts=1200,
                            rssi=-20.0, freq_mhz=433.92))
    pk = db.list_sensors()[0].pk
    bands = {b["label"]: b["count"] for b in q.sensor_bands(db, pk)}
    assert bands == {"315 MHz": 3, "433.92 MHz": 1}


def test_the_rollup_is_backfilled_for_an_existing_database(tmp_path):
    """An upgrade must not start every sensor's band history from zero."""
    path = tmp_path / "old.db"
    db = Database(path)
    db.execute("INSERT INTO sensors (model, sensor_id, first_seen, last_seen) "
               "VALUES ('Toyota-TPMS', 'aaa', 1000, 1000)")
    pk = db.list_sensors()[0].pk
    for ts in (1000, 1060):
        db.execute("INSERT INTO readings (sensor_pk, ts, freq_mhz) VALUES (?, ?, 315.01)",
                   (pk, ts))
    db.execute("DELETE FROM band_counts")
    db.execute("PRAGMA user_version=4")
    db.close()

    reopened = Database(path)
    assert [b["count"] for b in q.sensor_bands(reopened, pk)] == [2]


# -- the service ----------------------------------------------------------

def test_the_service_reports_what_the_capture_costs(tmp_path):
    service = Service(Config(database=str(tmp_path / "s.db")), start_radio=False)
    now = time.time()
    for age_days in (10, 5, 0):
        service.ingestor.ingest(_reading("aaa", now - age_days * DAY))
    storage = service.status()["storage"]
    assert storage["bytes"] > 0
    assert storage["readings"] == 3
    assert storage["bytes_per_day"] > 0
    assert storage["policy"]["raw_days"] == 7


def test_housekeeping_through_the_service_records_its_run(tmp_path):
    service = Service(Config(database=str(tmp_path / "s.db")), start_radio=False)
    service.ingestor.ingest(_reading("aaa", time.time() - 30 * DAY))
    report = service.housekeep()
    assert report.raw_dropped == 1
    assert service.status()["storage"]["last_housekeeping"] is not None


def test_human_bytes():
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 kB"
    assert human_bytes(815_000_000).endswith("MB")
