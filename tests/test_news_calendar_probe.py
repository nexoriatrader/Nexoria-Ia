"""The legacy calendar probe must not activate trading or leak user data."""
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from app import main, news_calendar_probe


def test_calendar_probe_validates_epoch_currency_and_impact():
    data={"economic_calendar":[
        {"date":1795000000,"currency":"USD","event":"Rate decision","impact":"high"},
        {"date":"bad","currency":"EUR","event":"Invalid"},
        {"date":1795000000,"currency":"FAKE","event":"Invalid"},
    ]}
    events=news_calendar_probe._validated_events(data)
    assert len(events)==1
    assert events[0]["currency"]=="USD"
    assert events[0]["impact_raw"]=="high"


def test_calendar_probe_rejects_invalid_structure():
    try:
        news_calendar_probe._validated_events({"economic_calendar":{"events":[]}})
    except ValueError:
        pass
    else:
        assert False, "Malformed calendar must be rejected"


def test_news_calendar_diagnostic_requires_admin():
    with TestClient(main.app) as client:
        resp=client.get("/api/admin/news/calendar-diagnostic")
        assert resp.status_code==401
