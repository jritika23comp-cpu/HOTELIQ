from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .analytics import occupancy_from_availability, percentage_change
from .auth import utcnow
from .database import ROOT, get_db, row_to_dict, rows_to_list

CATALOG_PATH = ROOT / "data" / "hotels_catalog.json"

ROOM_TYPES = ["Deluxe Room", "Suite", "Villa"]
STATUSES = ["available", "limited", "unavailable"]


def load_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def seed_properties_if_empty() -> int:
    catalog = load_catalog()
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM properties").fetchone()["c"]
        if count:
            return count
        now = utcnow()
        for p in catalog["properties"]:
            conn.execute(
                """INSERT INTO properties
                (id, name, location, market, platform, booking_url, category, rating, total_rooms, active, is_portfolio, room_type, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p["id"], p["name"], p["location"], p["market"], p["platform"], p["booking_url"],
                    p["category"], p["rating"], p["total_rooms"], 1, 1 if p.get("is_portfolio") else 0,
                    "Deluxe Room", now,
                ),
            )
            check_in = (datetime.now(timezone.utc) + timedelta(days=14)).date().isoformat()
            check_out = (datetime.now(timezone.utc) + timedelta(days=15)).date().isoformat()
            conn.execute(
                """INSERT INTO tracking_conditions
                (property_id, check_in, check_out, guests, rooms, stay_duration, created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (p["id"], check_in, check_out, 2, 1, 1, now),
            )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
            ("demo_mode", "true"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
            ("data_source", "Controlled demo dataset — not live MakeMyTrip scrape"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
            ("price_alert_threshold", "8"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?,?)",
            ("crawl_frequency", "Hourly Sync"),
        )
        return len(catalog["properties"])


def _base_price(property_row: dict) -> float:
    rating = float(property_row.get("rating") or 4.0)
    rooms = int(property_row.get("total_rooms") or 40)
    market = property_row.get("market")
    base = 9000 + rating * 2800
    if market == "goa":
        base += 4000
    if market == "karjat":
        base += 1500
    if rooms < 15:
        base += 3500
    return base


def run_collection(days_back: int = 0, snapshots: int = 1, note: str = "") -> dict:
    """Generate permitted demo observations. Does not scrape OTA sites."""
    started = utcnow()
    errors = 0
    written = 0
    logs = []

    def log(level: str, msg: str):
        logs.append((level, msg, utcnow()))

    with get_db() as conn:
        props = rows_to_list(conn.execute("SELECT * FROM properties WHERE active = 1").fetchall())
        cur = conn.execute(
            "INSERT INTO collection_runs (started_at, status, notes) VALUES (?,?,?)",
            (started, "running", note or "DEMO MODE collection from controlled dataset"),
        )
        run_id = cur.lastrowid
        log("INFO", f"Collection started for {len(props)} properties. Source=DEMO DATA")

        thresholds = {
            "price": float(conn.execute("SELECT value FROM settings WHERE key='price_alert_threshold'").fetchone()["value"]
                           if conn.execute("SELECT value FROM settings WHERE key='price_alert_threshold'").fetchone()
                           else 8),
        }

        rng = random.Random(42 if days_back else datetime.now().timestamp())
        now = datetime.now(timezone.utc).replace(microsecond=0)

        for p in props:
            cond = row_to_dict(conn.execute(
                "SELECT * FROM tracking_conditions WHERE property_id = ? ORDER BY id DESC LIMIT 1",
                (p["id"],),
            ).fetchone())
            try:
                for i in range(snapshots):
                    when = now - timedelta(days=days_back) + timedelta(hours=i * 4)
                    weekend = when.weekday() >= 4
                    price = _base_price(p) * (1.22 if weekend else 1.0)
                    price *= 1 + rng.uniform(-0.08, 0.08)
                    if days_back:
                        # slight historical drift
                        price *= 1 - (days_back * 0.004)
                    price = round(price, 0)
                    room_type = p.get("room_type") or "Deluxe Room"
                    conn.execute(
                        """INSERT INTO price_snapshots
                        (property_id, tracking_condition_id, room_type, price, currency, source, observed_at)
                        VALUES (?,?,?,?,?,?,?)""",
                        (p["id"], cond["id"] if cond else None, room_type, price, "INR", "DEMO DATA", when.isoformat()),
                    )
                    status = rng.choices(STATUSES, weights=[0.62, 0.25, 0.13])[0]
                    available_flag = 0 if status == "unavailable" else 1
                    conn.execute(
                        """INSERT INTO availability_snapshots
                        (property_id, room_type, available, status, source, observed_at)
                        VALUES (?,?,?,?,?,?)""",
                        (p["id"], room_type, available_flag, status, "DEMO DATA", when.isoformat()),
                    )
                    written += 2

                    prev = row_to_dict(conn.execute(
                        """SELECT price FROM price_snapshots
                           WHERE property_id = ? AND observed_at < ?
                           ORDER BY observed_at DESC LIMIT 1""",
                        (p["id"], when.isoformat()),
                    ).fetchone())
                    if prev:
                        pct = percentage_change(prev["price"], price)
                        if pct is not None and abs(pct) >= thresholds["price"]:
                            conn.execute(
                                """INSERT INTO detected_changes
                                (property_id, change_type, old_value, new_value, percentage_change, detected_at)
                                VALUES (?,?,?,?,?,?)""",
                                (p["id"], "price", str(prev["price"]), str(price), pct, when.isoformat()),
                            )
                            severity = "high" if abs(pct) >= 12 else "medium"
                            direction = "increased" if pct > 0 else "decreased"
                            conn.execute(
                                """INSERT INTO alerts
                                (property_id, alert_type, severity, message, supporting_data, status, created_at)
                                VALUES (?,?,?,?,?,?,?)""",
                                (
                                    p["id"], "price", severity,
                                    f"{p['name']} demo price {direction} {pct}% ({prev['price']} → {price} INR).",
                                    json.dumps({"old": prev["price"], "new": price, "pct": pct, "source": "DEMO DATA"}),
                                    "open", when.isoformat(),
                                ),
                            )

                    prev_av = row_to_dict(conn.execute(
                        """SELECT status FROM availability_snapshots
                           WHERE property_id = ? AND observed_at < ?
                           ORDER BY observed_at DESC LIMIT 1""",
                        (p["id"], when.isoformat()),
                    ).fetchone())
                    if prev_av and prev_av["status"] != status:
                        conn.execute(
                            """INSERT INTO detected_changes
                            (property_id, change_type, old_value, new_value, percentage_change, detected_at)
                            VALUES (?,?,?,?,?,?)""",
                            (p["id"], "availability", prev_av["status"], status, None, when.isoformat()),
                        )
                        if status == "unavailable":
                            conn.execute(
                                """INSERT INTO alerts
                                (property_id, alert_type, severity, message, supporting_data, status, created_at)
                                VALUES (?,?,?,?,?,?,?)""",
                                (
                                    p["id"], "availability", "medium",
                                    f"{p['name']} observed status changed to unavailable. This is not a confirmed booking.",
                                    json.dumps({"old": prev_av["status"], "new": status, "source": "DEMO DATA"}),
                                    "open", when.isoformat(),
                                ),
                            )

                    occ = occupancy_from_availability(status, p.get("total_rooms"))
                    rev = round(occ["estimated_rooms_sold"] * price, 2)
                    conn.execute(
                        """INSERT INTO estimates
                        (property_id, estimated_rooms_sold, estimated_occupancy, estimated_revenue, methodology, created_at)
                        VALUES (?,?,?,?,?,?)""",
                        (p["id"], occ["estimated_rooms_sold"], occ["estimated_occupancy"], rev, occ["methodology"], when.isoformat()),
                    )
                    written += 1
                log("SUCCESS", f"DEMO snapshot stored | {p['name']} | {p['platform']}")
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log("ERROR", f"{p['name']}: {exc}")

        # market median alert
        latest_prices = [r["price"] for r in conn.execute(
            """SELECT p1.price FROM price_snapshots p1
               JOIN (SELECT property_id, MAX(observed_at) mx FROM price_snapshots GROUP BY property_id) t
               ON p1.property_id = t.property_id AND p1.observed_at = t.mx"""
        ).fetchall()]
        if len(latest_prices) >= 5:
            latest_prices.sort()
            median = latest_prices[len(latest_prices) // 2]
            conn.execute(
                """INSERT INTO alerts (property_id, alert_type, severity, message, supporting_data, status, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    None, "market", "low",
                    f"Market median observed demo price is ₹{median:,.0f} across {len(latest_prices)} properties.",
                    json.dumps({"median": median, "n": len(latest_prices), "source": "DEMO DATA"}),
                    "open", utcnow(),
                ),
            )

        finished = utcnow()
        conn.execute(
            "UPDATE collection_runs SET finished_at=?, status=?, records_written=?, errors=? WHERE id=?",
            (finished, "success" if errors == 0 else "partial", written, errors, run_id),
        )
        for level, msg, ts in logs:
            conn.execute(
                "INSERT INTO collection_logs (run_id, level, message, created_at) VALUES (?,?,?,?)",
                (run_id, level, msg, ts),
            )

    return {
        "run_id": run_id,
        "records_written": written,
        "errors": errors,
        "logs": [{"level": a, "message": b, "created_at": c} for a, b, c in logs[-40:]],
        "data_source": "DEMO DATA",
        "started_at": started,
    }


def seed_history(days: int = 14) -> None:
    with get_db() as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM price_snapshots").fetchone()["c"]
        if n:
            return
    for d in range(days, -1, -1):
        snaps = 3 if d == 0 else 2
        run_collection(days_back=d, snapshots=snaps, note=f"Historical demo seed day -{d}")
