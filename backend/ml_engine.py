from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split

from .analytics import percentage_change
from .auth import utcnow
from .database import ROOT, get_db, row_to_dict, rows_to_list

MODEL_DIR = ROOT / "models"
MODEL_DIR.mkdir(parents=True, exist_ok=True)
MODEL_PATH = MODEL_DIR / "demand_rf.joblib"
VERSION = "demand-rf-v1"


def _feature_row(price, market_avg, rating, rooms, weekend, month, avail_code, price_chg, stay, lead) -> list[float]:
    return [
        float(price or 0),
        float(market_avg or 0),
        float(rating or 0),
        float(rooms or 0),
        float(weekend),
        float(month),
        float(avail_code),
        float(price_chg or 0),
        float(stay or 1),
        float(lead or 14),
    ]


def build_dataset() -> tuple[np.ndarray, np.ndarray, list[str]]:
    with get_db() as conn:
        rows = rows_to_list(conn.execute(
            """
            SELECT ps.property_id, ps.price, ps.observed_at, p.rating, p.total_rooms, p.market,
                   av.status, tc.stay_duration, tc.check_in
            FROM price_snapshots ps
            JOIN properties p ON p.id = ps.property_id
            LEFT JOIN availability_snapshots av
              ON av.property_id = ps.property_id AND av.observed_at = ps.observed_at
            LEFT JOIN tracking_conditions tc ON tc.property_id = ps.property_id
            ORDER BY ps.observed_at
            """
        ).fetchall())

    # market average by timestamp bucket (date)
    by_day: dict[str, list[float]] = {}
    for r in rows:
        day = r["observed_at"][:10]
        by_day.setdefault(day, []).append(float(r["price"]))
    day_avg = {d: sum(v) / len(v) for d, v in by_day.items()}

    last_price: dict[str, float] = {}
    X, y = [], []
    avail_map = {"available": 0, "limited": 1, "unavailable": 2}
    for r in rows:
        day = r["observed_at"][:10]
        dt = datetime.fromisoformat(r["observed_at"])
        weekend = 1 if dt.weekday() >= 4 else 0
        prev = last_price.get(r["property_id"])
        chg = percentage_change(prev, r["price"]) or 0
        last_price[r["property_id"]] = r["price"]
        avail_code = avail_map.get((r.get("status") or "available"), 0)
        lead = 14
        if r.get("check_in"):
            try:
                lead = max(0, (datetime.fromisoformat(r["check_in"]) - dt.replace(tzinfo=None)).days)
            except Exception:
                lead = 14
        features = _feature_row(
            r["price"], day_avg[day], r["rating"], r["total_rooms"], weekend, dt.month,
            avail_code, chg, r.get("stay_duration") or 1, lead,
        )
        # Demand proxy: tighter availability + weekend + cheaper vs market => higher demand score 0-100
        rel = 0
        if day_avg[day]:
            rel = (day_avg[day] - r["price"]) / day_avg[day]
        demand = 40 + weekend * 12 + avail_code * 18 + rel * 20 + (float(r["rating"] or 4) - 4) * 8
        demand = max(5, min(98, demand))
        X.append(features)
        y.append(demand)
    cols = ["price", "market_avg", "rating", "rooms", "weekend", "month", "avail_code", "price_chg", "stay", "lead"]
    return np.array(X, dtype=float), np.array(y, dtype=float), cols


def train_model() -> dict:
    X, y, cols = build_dataset()
    if len(y) < 30:
        raise RuntimeError("Not enough snapshots to train. Run data collection first.")
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    model = RandomForestRegressor(n_estimators=160, max_depth=12, random_state=42, n_jobs=-1)
    model.fit(X_train, y_train)
    pred = model.predict(X_test)
    mae = float(mean_absolute_error(y_test, pred))
    rmse = float(np.sqrt(mean_squared_error(y_test, pred)))
    r2 = float(r2_score(y_test, pred))
    metrics = {
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "r2": round(r2, 4),
        "train_size": int(len(y_train)),
        "test_size": int(len(y_test)),
        "features": cols,
        "target": "demand_score (proxy 0-100 from availability tightness, weekend, price vs market, rating)",
        "note": "Metrics are from a real train/test split on the demo observation dataset.",
    }
    payload = {"model": model, "features": cols, "version": VERSION, "metrics": metrics}
    joblib.dump(payload, MODEL_PATH)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO model_runs (model_name, version, training_date, metrics, dataset_version, status)
               VALUES (?,?,?,?,?,?)""",
            ("RandomForestRegressor demand", VERSION, utcnow(), json.dumps(metrics), f"n={len(y)}", "ready"),
        )
    return {"version": VERSION, "metrics": metrics, "path": str(MODEL_PATH)}


def load_bundle() -> dict | None:
    if not MODEL_PATH.exists():
        return None
    return joblib.load(MODEL_PATH)


def latest_run() -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM model_runs ORDER BY id DESC LIMIT 1").fetchone()
    return row_to_dict(row)


def predict_demand(property_id: str, check_in: str, check_out: str) -> dict:
    bundle = load_bundle()
    if not bundle:
        raise RuntimeError("Model not trained yet.")
    with get_db() as conn:
        prop = row_to_dict(conn.execute("SELECT * FROM properties WHERE id=?", (property_id,)).fetchone())
        if not prop:
            raise ValueError("Unknown property_id")
        latest = row_to_dict(conn.execute(
            "SELECT * FROM price_snapshots WHERE property_id=? ORDER BY observed_at DESC LIMIT 1",
            (property_id,),
        ).fetchone())
        prev = row_to_dict(conn.execute(
            "SELECT * FROM price_snapshots WHERE property_id=? ORDER BY observed_at DESC LIMIT 1 OFFSET 1",
            (property_id,),
        ).fetchone())
        av = row_to_dict(conn.execute(
            "SELECT * FROM availability_snapshots WHERE property_id=? ORDER BY observed_at DESC LIMIT 1",
            (property_id,),
        ).fetchone())
        market = conn.execute(
            """SELECT AVG(price) AS a FROM price_snapshots p1
               JOIN (SELECT property_id, MAX(observed_at) mx FROM price_snapshots GROUP BY property_id) t
               ON p1.property_id=t.property_id AND p1.observed_at=t.mx"""
        ).fetchone()["a"]

    price = latest["price"] if latest else 15000
    chg = percentage_change(prev["price"] if prev else None, price)
    dt = datetime.fromisoformat(check_in)
    weekend = 1 if dt.weekday() >= 4 else 0
    lead = max(0, (dt.date() - datetime.now(timezone.utc).date()).days)
    stay = 1
    try:
        stay = max(1, (datetime.fromisoformat(check_out).date() - dt.date()).days)
    except Exception:
        stay = 1
    avail_map = {"available": 0, "limited": 1, "unavailable": 2}
    avail_code = avail_map.get((av or {}).get("status", "available"), 0)
    x = np.array([_feature_row(
        price, market, prop["rating"], prop["total_rooms"], weekend, dt.month,
        avail_code, chg or 0, stay, lead,
    )])
    est = bundle["model"].predict(x)[0]
    # tree variance as uncertainty
    tree_preds = np.array([t.predict(x)[0] for t in bundle["model"].estimators_])
    std = float(tree_preds.std())
    conf = max(0.0, min(0.95, 1 - (std / 40)))
    created = utcnow()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO predictions
            (property_id, prediction_type, prediction_value, confidence, predicted_for, model_version, created_at)
            VALUES (?,?,?,?,?,?,?)""",
            (property_id, "demand_score", float(est), conf, check_in, bundle["version"], created),
        )
        if est >= 72:
            conn.execute(
                """INSERT INTO alerts (property_id, alert_type, severity, message, supporting_data, status, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    property_id, "demand", "medium",
                    f"Predicted demand score {est:.1f} for {prop['name']} on {check_in} (model {bundle['version']}).",
                    json.dumps({"prediction": float(est), "confidence": conf, "check_in": check_in}),
                    "open", created,
                ),
            )
    return {
        "property_id": property_id,
        "property_name": prop["name"],
        "prediction_type": "demand_score",
        "prediction": round(float(est), 2),
        "confidence": round(conf, 3),
        "uncertainty_std": round(std, 3),
        "model_version": bundle["version"],
        "predicted_for": check_in,
        "check_out": check_out,
        "timestamp": created,
        "kind": "predicted",
        "disclaimer": "Demand score is a model proxy (0-100), not a confirmed booking forecast.",
    }
