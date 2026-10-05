"""API smoke tests against a temporary SQLite database."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")
os.environ.setdefault("DEFAULT_ADMIN_EMAIL", "revenue.lead@dellavillas.com")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "hotelIQ-demo-2026")

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
tmp.close()
os.environ["DATABASE_PATH"] = tmp.name

from backend.app import create_app  # noqa: E402
from backend.ml_engine import train_model  # noqa: E402

app = create_app()


def main() -> None:
    client = app.test_client()
    assert client.get("/health").json["ok"] is True
    login = client.post("/api/auth/login", json={
        "email": "revenue.lead@dellavillas.com",
        "password": "hotelIQ-demo-2026",
    })
    assert login.status_code == 200, login.data
    dash = client.get("/api/dashboard")
    assert dash.status_code == 200
    body = dash.get_json()
    assert body["competitor_count"] >= 30
    assert body["demo_mode"] is True
    comps = client.get("/api/competitors?per_page=5").get_json()
    assert comps["total"] >= 30
    pid = comps["items"][0]["id"]
    detail = client.get(f"/api/competitors/{pid}")
    assert detail.status_code == 200
    hist = client.get(f"/api/history/{pid}").get_json()
    assert len(hist["prices"]) >= 2
    pricing = client.get("/api/pricing")
    assert pricing.status_code == 200
    avail = client.get("/api/availability")
    assert avail.status_code == 200
    insights = client.get("/api/insights").get_json()
    assert insights["insights"]
    alerts = client.get("/api/alerts")
    assert alerts.status_code == 200
    coll = client.post("/api/collection/run")
    assert coll.status_code == 200
    train_model()
    pred = client.post("/api/ml/predict", json={
        "property_id": pid,
        "check_in": "2026-10-20",
        "check_out": "2026-10-21",
    })
    assert pred.status_code == 200, pred.data
    pjson = pred.get_json()
    assert "prediction" in pjson
    assert pjson["kind"] == "predicted"
    created = client.post("/api/competitors", json={
        "name": "Test Villa Smoke",
        "location": "Lonavala",
        "market": "lonavala",
    })
    assert created.status_code == 201
    print("SMOKE_OK", {
        "competitors": body["competitor_count"],
        "prediction": pjson["prediction"],
        "mae_note": "train_model executed",
    })


if __name__ == "__main__":
    try:
        main()
    finally:
        Path(tmp.name).unlink(missing_ok=True)
