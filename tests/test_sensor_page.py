"""The sensor page is the hub every other page links into.

Before it existed a sensor appeared on five pages with five different subsets
of its data and no way to drill into it, which is what made the UI feel
scattered. These tests pin the linking and the cross-references.
"""

import re

import pytest
from fastapi.testclient import TestClient

from tpms import queries as q
from tpms.config import Config
from tpms.models import Reading, now as now_ts
from tpms.service import Service
from tpms.web.app import create_app


@pytest.fixture
def service(tmp_path):
    svc = Service(Config(database=str(tmp_path / "web.db")), start_radio=False)
    # Two wheels heard together on six passes, plus a duplicate decode of one.
    for p in range(6):
        base = 1_000_000 + p * 600
        for model, sid, slot in [
            ("Toyota-TPMS", "aaa", 0),
            ("Toyota-TPMS", "bbb", 1),
            ("Citroen", "bbb", 1),  # same burst as the wheel above
        ]:
            svc.ingestor.ingest(
                Reading(
                    model=model, sensor_id=sid, ts=base + slot * 3.0,
                    rssi=-8.0 - slot * 2.0, snr=12.0, freq_mhz=315.01,
                    pressure_kpa=230 + slot, temperature_c=28, battery_ok=1,
                    raw='{"model":"%s","id":"%s"}' % (model, sid),
                )
            )
    svc.ingestor.sweep(when=9e9)
    svc.detect_aliases()
    svc.recluster()
    return svc


@pytest.fixture
def client(service):
    return TestClient(create_app(service))


def _pk(service, display):
    return next(s.pk for s in service.db.list_sensors() if s.display == display)


def test_sensor_page_renders(client, service):
    pk = _pk(service, "Toyota-TPMS/aaa")
    body = client.get(f"/sensors/{pk}").text
    assert "Toyota-TPMS/aaa" in body
    assert "Heard alongside" in body
    assert "Sightings" in body


def test_missing_sensor_is_404(client):
    assert client.get("/sensors/9999").status_code == 404


@pytest.mark.parametrize("path", ["/sensors", "/events", "/vehicles"])
def test_every_page_links_sensors_to_their_detail_page(client, service, path):
    """A sensor mentioned anywhere has to be clickable, or the data is a dead end."""
    body = client.get(path).text
    assert re.search(r'href="/sensors/\d+"', body), f"{path} has no sensor links"


def test_the_live_page_links_the_sensors_it_is_hearing(tmp_path):
    """Checked with a sighting left open -- Live lists nothing when all are closed."""
    svc = Service(Config(database=str(tmp_path / "live.db")), start_radio=False)
    svc.ingestor.ingest(
        Reading(model="Toyota-TPMS", sensor_id="aaa", ts=now_ts(), rssi=-9.0,
                freq_mhz=315.01, pressure_kpa=230.0)
    )
    body = TestClient(create_app(svc)).get("/").text
    assert re.search(r'href="/sensors/\d+"', body)


def test_heard_alongside_reports_the_clustering_evidence(service):
    pk = _pk(service, "Toyota-TPMS/aaa")
    partners = {p["display"]: p for p in q.heard_alongside(service.db, pk)}
    assert "Toyota-TPMS/bbb" in partners
    assert partners["Toyota-TPMS/bbb"]["count"] == 6
    assert partners["Toyota-TPMS/bbb"]["support"] == pytest.approx(1.0)
    assert partners["Toyota-TPMS/bbb"]["strong"] is True


def test_a_duplicate_decode_is_not_reported_as_a_co_traveller(service):
    """An alias shares every burst by definition, so 100%% support is meaningless."""
    canonical = _pk(service, "Toyota-TPMS/bbb")
    alias = _pk(service, "Citroen/bbb")
    assert service.db.get_sensor(alias).alias_of == canonical

    partners = {p["display"]: p for p in q.heard_alongside(service.db, canonical)}
    assert partners["Citroen/bbb"]["duplicate"] is True
    assert partners["Toyota-TPMS/aaa"]["duplicate"] is False


def test_sensor_detail_carries_every_field_the_list_pages_show(service):
    """The detail page is a superset; a value shown elsewhere must exist here."""
    pk = _pk(service, "Toyota-TPMS/aaa")
    detail = q.sensor_detail(service.db, pk)
    listed = q.sensor_rows(service.db)[0]
    for field in listed:
        assert field in detail, f"{field} is shown in the list but missing from detail"
    for extra in ("sightings", "history", "heard_with", "raw", "snr"):
        assert extra in detail


def test_live_fragment_renders_on_its_own(client):
    """The Live page swaps this in without reloading; it must stand alone."""
    response = client.get("/api/heard-now.html")
    assert response.status_code == 200
    assert "<html" not in response.text.lower()


def test_a_duplicate_of_a_third_sensor_is_not_listed_as_a_partner(service):
    """Otherwise one car appears twice, once per protocol that decoded it."""
    aaa = _pk(service, "Toyota-TPMS/aaa")
    partners = {p["display"] for p in q.heard_alongside(service.db, aaa)}
    assert "Toyota-TPMS/bbb" in partners      # the real second wheel
    assert "Citroen/bbb" not in partners      # its duplicate decode


def test_heard_now_folds_duplicate_decodes(service):
    """Live used to list one transmitter as two audible sensors."""
    for sensor in service.db.list_sensors():
        service.db.create_sighting(sensor.pk, 9_000_000, -8.0, 315.01)

    displays = {h["display"] for h in q.heard_now(service.db)}
    assert "Toyota-TPMS/bbb" in displays
    assert "Citroen/bbb" not in displays, "a duplicate decode is not a second sensor"
    assert len(displays) == len(q.sensor_rows(service.db))
