"""The operation-start display shows hour/minute only; stored timestamps are unchanged."""
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(encoding="utf-8")


def test_operation_start_hour_minute_no_seconds():
    assert "const brasilDateTime=(iso)=>{" in HTML
    formatter = HTML.split("const brasilDateTime=(iso)=>{", 1)[1].split("\n};", 1)[0]
    assert "timeZone:'America/Sao_Paulo'" in formatter
    assert "hour:'2-digit',minute:'2-digit',hour12:false" in formatter
    assert "second:" not in formatter


def test_history_and_dashboard_share_operation_start_formatter():
    assert "brasilDateTime(order.started_at_brasilia||order.broker_purchase_at)" in HTML
    assert "brasilDateTime(o.started_at_brasilia||o.broker_purchase_at)" in HTML
