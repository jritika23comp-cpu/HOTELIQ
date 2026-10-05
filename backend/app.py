from __future__ import annotations

import csv
import io
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_cors import CORS

from .ai_engine import generate_insights
from .analytics import competitor_score, estimated_revenue, market_stats, occupancy_from_availability, percentage_change
from .auth import authenticate, ensure_default_user, public_user
from .collector import run_collection, seed_history, seed_properties_if_empty
from .database import ROOT, get_db, init_db, row_to_dict, rows_to_list
from .ml_engine import latest_run, predict_demand, train_model

TEMPLATES = ROOT / "frontend" / "pages"
STATIC = ROOT / "frontend" / "assets"


def create_app() -> Flask:
    app = Flask(__name__, template_folder=str(TEMPLATES), static_folder=str(STATIC), static_url_path="/assets")
    app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
    CORS(app, supports_credentials=True)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"

    init_db()
    ensure_default_user()
    seed_properties_if_empty()
    seed_history(days=6)
    try:
        if latest_run() is None:
            train_model()
    except Exception:
        pass

    def current_user():
        uid = session.get("user_id")
        if not uid:
            return None
        with get_db() as conn:
            row = conn.execute("SELECT id, email, name, role FROM users WHERE id=?", (uid,)).fetchone()
        return row_to_dict(row)

    def login_required_page(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user():
                return redirect(url_for("login_page"))
            return fn(*args, **kwargs)
        return wrapper

    def api_auth(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if request.path.startswith("/api/auth/"):
                return fn(*args, **kwargs)
            if not current_user():
                return jsonify({"error": "Authentication required"}), 401
            return fn(*args, **kwargs)
        return wrapper

    def meta():
        with get_db() as conn:
            last = row_to_dict(conn.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT 1").fetchone())
            open_alerts = conn.execute("SELECT COUNT(*) AS c FROM alerts WHERE status='open'").fetchone()["c"]
        return {
            "demo_mode": True,
            "data_source": "Controlled demo dataset — not live MakeMyTrip data",
            "last_collection": last,
            "open_alerts": open_alerts,
        }

    def page(name: str):
        user = current_user()
        return render_template(name, user=user, meta=meta(), active=name)

    @app.get("/")
    def root():
        if current_user():
            return redirect("/dashboard.html")
        return redirect("/index.html")

    @app.get("/index.html")
    def login_page():
        if current_user():
            return redirect("/dashboard.html")
        return render_template("index.html", user=None, meta=meta(), active="index.html")

    pages = [
        "dashboard.html", "competitors.html", "competitor-detail.html", "pricing.html",
        "availability.html", "booking-intelligence.html", "revenue.html", "historical.html",
        "ai-insights.html", "alerts.html", "data-collection.html", "properties.html",
        "add-property.html", "ml-models.html", "settings.html",
    ]
    for p in pages:
        def make(p=p):
            @login_required_page
            def view():
                return page(p)
            return view
        app.add_url_rule(f"/{p}", p.replace(".", "_").replace("-", "_"), make())

    @app.post("/api/auth/login")
    def api_login():
        data = request.get_json(silent=True) or request.form
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        user = authenticate(email, password)
        if not user:
            return jsonify({"error": "Invalid email or password"}), 401
        session["user_id"] = user["id"]
        return jsonify({"ok": True, "user": public_user(user)})

    @app.post("/api/auth/logout")
    def api_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/auth/me")
    def api_me():
        user = current_user()
        if not user:
            return jsonify({"user": None})
        return jsonify({"user": user, **meta()})

    def require_user():
        user = current_user()
        if not user:
            return None, (jsonify({"error": "Authentication required"}), 401)
        return user, None

    @app.get("/api/dashboard")
    def api_dashboard():
        user, err = require_user()
        if err:
            return err
        market = request.args.get("market")
        with get_db() as conn:
            where = "WHERE p.active=1"
            params = []
            if market:
                where += " AND p.market=?"
                params.append(market)
            props = rows_to_list(conn.execute(f"SELECT * FROM properties p {where}", params).fetchall())
            latest = rows_to_list(conn.execute(
                f"""
                SELECT p.id, p.name, p.category, p.rating, p.platform, p.market, p.total_rooms,
                       ps.price, ps.observed_at, ps.source, av.status
                FROM properties p
                LEFT JOIN price_snapshots ps ON ps.id = (
                    SELECT id FROM price_snapshots WHERE property_id=p.id ORDER BY observed_at DESC LIMIT 1)
                LEFT JOIN availability_snapshots av ON av.id = (
                    SELECT id FROM availability_snapshots WHERE property_id=p.id ORDER BY observed_at DESC LIMIT 1)
                {where}
                ORDER BY p.name
                """,
                params,
            ).fetchall())
            alerts = rows_to_list(conn.execute(
                "SELECT a.*, p.name AS property_name FROM alerts a LEFT JOIN properties p ON p.id=a.property_id WHERE a.status='open' ORDER BY a.created_at DESC LIMIT 8"
            ).fetchall())
            # history for chart: last 14 days market avg
            hist = rows_to_list(conn.execute(
                """SELECT substr(observed_at,1,10) AS day, AVG(price) AS avg_price, COUNT(*) AS n
                   FROM price_snapshots GROUP BY day ORDER BY day DESC LIMIT 14"""
            ).fetchall())[::-1]
            changes = rows_to_list(conn.execute(
                "SELECT * FROM detected_changes ORDER BY detected_at DESC LIMIT 20"
            ).fetchall())

        prices = [r["price"] for r in latest if r.get("price") is not None]
        stats = market_stats(prices)
        occs = [occupancy_from_availability(r.get("status"), r.get("total_rooms"))["estimated_occupancy"] for r in latest]
        avg_occ = round(sum(occs) / len(occs), 1) if occs else None
        adr = stats["average"]
        est_rev = round((avg_occ / 100) * adr * sum(r.get("total_rooms") or 0 for r in latest), 2) if adr and avg_occ else None
        price_ups = [c for c in changes if c.get("change_type") == "price" and (c.get("percentage_change") or 0) > 0]
        avail_ch = [c for c in changes if c.get("change_type") == "availability"]
        return jsonify({
            **meta(),
            "competitor_count": len(props),
            "average_market_price": stats["average"],
            "median_market_price": stats["median"],
            "min_price": stats["minimum"],
            "max_price": stats["maximum"],
            "price_changes": len(price_ups),
            "availability_changes": len(avail_ch),
            "estimated_occupancy": avg_occ,
            "estimated_revenue": est_rev,
            "revenue_methodology": "Estimated market GBV proxy = mean occupancy estimate × ADR × total listed rooms. Not confirmed bookings.",
            "alerts": alerts,
            "latest_observations": latest[:12],
            "history": hist,
            "stats": stats,
        })

    @app.get("/api/competitors")
    def api_competitors():
        user, err = require_user()
        if err:
            return err
        q = (request.args.get("q") or "").strip()
        market = request.args.get("market")
        category = request.args.get("category")
        sort = request.args.get("sort") or "name"
        order = "DESC" if request.args.get("order") == "desc" else "ASC"
        page_n = max(1, int(request.args.get("page") or 1))
        per = min(50, max(5, int(request.args.get("per_page") or 12)))
        sort_map = {"name": "p.name", "rating": "p.rating", "price": "ps.price", "rooms": "p.total_rooms"}
        sort_col = sort_map.get(sort, "p.name")
        where = ["p.active >= 0"]
        params: list = []
        if q:
            where.append("(p.name LIKE ? OR p.location LIKE ? OR p.category LIKE ?)")
            params += [f"%{q}%"] * 3
        if market:
            where.append("p.market=?")
            params.append(market)
        if category:
            where.append("p.category=?")
            params.append(category)
        where_sql = " AND ".join(where)
        with get_db() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS c FROM properties p WHERE {where_sql}", params).fetchone()["c"]
            rows = rows_to_list(conn.execute(
                f"""
                SELECT p.*, ps.price AS latest_price, ps.observed_at AS last_seen, av.status AS availability_status
                FROM properties p
                LEFT JOIN price_snapshots ps ON ps.id = (
                    SELECT id FROM price_snapshots WHERE property_id=p.id ORDER BY observed_at DESC LIMIT 1)
                LEFT JOIN availability_snapshots av ON av.id = (
                    SELECT id FROM availability_snapshots WHERE property_id=p.id ORDER BY observed_at DESC LIMIT 1)
                WHERE {where_sql}
                ORDER BY {sort_col} {order}
                LIMIT ? OFFSET ?
                """,
                params + [per, (page_n - 1) * per],
            ).fetchall())
            mkt_prices = [r["latest_price"] for r in conn.execute(
                """SELECT ps.price AS latest_price FROM properties p
                   LEFT JOIN price_snapshots ps ON ps.id = (
                     SELECT id FROM price_snapshots WHERE property_id=p.id ORDER BY observed_at DESC LIMIT 1)"""
            ).fetchall() if r["latest_price"] is not None]
        stats = market_stats(mkt_prices)
        for r in rows:
            prev = None
            with get_db() as conn:
                prev = row_to_dict(conn.execute(
                    "SELECT price FROM price_snapshots WHERE property_id=? ORDER BY observed_at DESC LIMIT 1 OFFSET 1",
                    (r["id"],),
                ).fetchone())
            chg = percentage_change(prev["price"] if prev else None, r.get("latest_price"))
            r["price_change_pct"] = chg
            r["score"] = competitor_score(r.get("rating"), r.get("latest_price"), stats["average"], r.get("availability_status"), chg)
            r["data_label"] = "DEMO DATA"
        return jsonify({
            **meta(),
            "items": rows,
            "page": page_n,
            "per_page": per,
            "total": total,
            "pages": (total + per - 1) // per,
            "market_stats": stats,
        })

    @app.get("/api/competitors/<pid>")
    def api_competitor_detail(pid):
        user, err = require_user()
        if err:
            return err
        with get_db() as conn:
            prop = row_to_dict(conn.execute("SELECT * FROM properties WHERE id=?", (pid,)).fetchone())
            if not prop:
                return jsonify({"error": "Not found"}), 404
            prices = rows_to_list(conn.execute(
                "SELECT * FROM price_snapshots WHERE property_id=? ORDER BY observed_at DESC LIMIT 80", (pid,)
            ).fetchall())
            avails = rows_to_list(conn.execute(
                "SELECT * FROM availability_snapshots WHERE property_id=? ORDER BY observed_at DESC LIMIT 80", (pid,)
            ).fetchall())
            changes = rows_to_list(conn.execute(
                "SELECT * FROM detected_changes WHERE property_id=? ORDER BY detected_at DESC LIMIT 30", (pid,)
            ).fetchall())
            cond = row_to_dict(conn.execute(
                "SELECT * FROM tracking_conditions WHERE property_id=? ORDER BY id DESC LIMIT 1", (pid,)
            ).fetchone())
            estimates = rows_to_list(conn.execute(
                "SELECT * FROM estimates WHERE property_id=? ORDER BY created_at DESC LIMIT 10", (pid,)
            ).fetchall())
            mkt = [r["price"] for r in conn.execute(
                """SELECT ps.price FROM properties p JOIN price_snapshots ps ON ps.id=(
                    SELECT id FROM price_snapshots WHERE property_id=p.id ORDER BY observed_at DESC LIMIT 1)
                   WHERE p.market=?""", (prop["market"],)
            ).fetchall()]
        stats = market_stats(mkt)
        latest_p = prices[0]["price"] if prices else None
        latest_a = avails[0]["status"] if avails else None
        prev_p = prices[1]["price"] if len(prices) > 1 else None
        score = competitor_score(prop.get("rating"), latest_p, stats["average"], latest_a, percentage_change(prev_p, latest_p))
        return jsonify({
            **meta(),
            "property": prop,
            "prices": prices,
            "availability": avails,
            "changes": changes,
            "tracking": cond,
            "estimates": estimates,
            "market_stats": stats,
            "score": score,
        })

    @app.post("/api/competitors")
    def api_competitor_create():
        user, err = require_user()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        pid = data.get("id") or ("P" + uuid.uuid4().hex[:8].upper())
        required = ["name", "location", "market"]
        if any(not data.get(k) for k in required):
            return jsonify({"error": "name, location, market are required"}), 400
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with get_db() as conn:
            conn.execute(
                """INSERT INTO properties (id,name,location,market,platform,booking_url,category,rating,total_rooms,active,is_portfolio,room_type,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pid, data["name"], data["location"], data["market"], data.get("platform") or "MakeMyTrip",
                    data.get("booking_url") or "https://www.makemytrip.com/hotels/",
                    data.get("category") or "Hotel", float(data.get("rating") or 4.0),
                    int(data.get("total_rooms") or 20), 1, int(bool(data.get("is_portfolio"))),
                    data.get("room_type") or "Deluxe Room", now,
                ),
            )
            cin = data.get("check_in") or (datetime.now(timezone.utc) + timedelta(days=14)).date().isoformat()
            cout = data.get("check_out") or (datetime.now(timezone.utc) + timedelta(days=15)).date().isoformat()
            conn.execute(
                """INSERT INTO tracking_conditions (property_id,check_in,check_out,guests,rooms,stay_duration,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (pid, cin, cout, int(data.get("guests") or 2), int(data.get("rooms") or 1), 1, now),
            )
        return jsonify({"ok": True, "id": pid}), 201

    @app.put("/api/competitors/<pid>")
    def api_competitor_update(pid):
        user, err = require_user()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        fields = ["name", "location", "market", "platform", "booking_url", "category", "rating", "total_rooms", "active", "is_portfolio", "room_type"]
        sets, params = [], []
        for f in fields:
            if f in data:
                sets.append(f"{f}=?")
                params.append(data[f])
        if not sets:
            return jsonify({"error": "No fields"}), 400
        params.append(pid)
        with get_db() as conn:
            cur = conn.execute(f"UPDATE properties SET {', '.join(sets)} WHERE id=?", params)
            if cur.rowcount == 0:
                return jsonify({"error": "Not found"}), 404
        return jsonify({"ok": True})

    @app.delete("/api/competitors/<pid>")
    def api_competitor_delete(pid):
        user, err = require_user()
        if err:
            return err
        with get_db() as conn:
            conn.execute("UPDATE properties SET active=0 WHERE id=?", (pid,))
        return jsonify({"ok": True})

    @app.get("/api/pricing")
    def api_pricing():
        user, err = require_user()
        if err:
            return err
        market = request.args.get("market")
        with get_db() as conn:
            where = "WHERE p.active=1"
            params = []
            if market:
                where += " AND p.market=?"
                params.append(market)
            rows = rows_to_list(conn.execute(
                f"""SELECT p.id, p.name, p.market, p.is_portfolio, ps.price, ps.observed_at, av.status
                    FROM properties p
                    LEFT JOIN price_snapshots ps ON ps.id=(SELECT id FROM price_snapshots WHERE property_id=p.id ORDER BY observed_at DESC LIMIT 1)
                    LEFT JOIN availability_snapshots av ON av.id=(SELECT id FROM availability_snapshots WHERE property_id=p.id ORDER BY observed_at DESC LIMIT 1)
                    {where} ORDER BY p.is_portfolio DESC, p.name""", params
            ).fetchall())
            hist = rows_to_list(conn.execute(
                """SELECT property_id, substr(observed_at,1,10) AS day, AVG(price) AS price
                   FROM price_snapshots GROUP BY property_id, day ORDER BY day"""
            ).fetchall())
        prices = [r["price"] for r in rows if r.get("price") is not None]
        stats = market_stats(prices)
        for r in rows:
            if r.get("price") and stats["average"]:
                r["price_difference"] = round(r["price"] - stats["average"], 2)
            else:
                r["price_difference"] = None
            r["data_label"] = "DEMO DATA"
        return jsonify({**meta(), "items": rows, "market_stats": stats, "history": hist})

    @app.get("/api/pricing/<pid>")
    def api_pricing_one(pid):
        user, err = require_user()
        if err:
            return err
        return api_competitor_detail(pid)

    @app.get("/api/availability")
    def api_availability():
        user, err = require_user()
        if err:
            return err
        market = request.args.get("market")
        with get_db() as conn:
            params = []
            where = "WHERE p.active=1"
            if market:
                where += " AND p.market=?"
                params.append(market)
            props = rows_to_list(conn.execute(f"SELECT id,name,market,is_portfolio FROM properties p {where}", params).fetchall())
            grid = {}
            days = []
            for p in props:
                rows = rows_to_list(conn.execute(
                    """SELECT substr(observed_at,1,10) AS day, status, observed_at
                       FROM availability_snapshots WHERE property_id=? ORDER BY observed_at""",
                    (p["id"],),
                ).fetchall())
                by_day = {}
                for r in rows:
                    by_day[r["day"]] = r
                    if r["day"] not in days:
                        days.append(r["day"])
                grid[p["id"]] = {"property": p, "by_day": by_day}
            days = sorted(days)[-7:]
        return jsonify({
            **meta(),
            "days": days,
            "grid": grid,
            "disclaimer": "Unavailable means the listing was not offered in the demo snapshot. It does not prove a booking occurred.",
        })

    @app.get("/api/availability/<pid>")
    def api_availability_one(pid):
        user, err = require_user()
        if err:
            return err
        with get_db() as conn:
            rows = rows_to_list(conn.execute(
                "SELECT * FROM availability_snapshots WHERE property_id=? ORDER BY observed_at DESC LIMIT 100", (pid,)
            ).fetchall())
        return jsonify({**meta(), "items": rows})

    @app.get("/api/history/<pid>")
    def api_history(pid):
        user, err = require_user()
        if err:
            return err
        with get_db() as conn:
            prices = rows_to_list(conn.execute(
                "SELECT observed_at, price, currency, source, room_type FROM price_snapshots WHERE property_id=? ORDER BY observed_at",
                (pid,),
            ).fetchall())
            av = rows_to_list(conn.execute(
                "SELECT observed_at, status, available, source FROM availability_snapshots WHERE property_id=? ORDER BY observed_at",
                (pid,),
            ).fetchall())
        series = []
        for i, p in enumerate(prices):
            old = prices[i - 1]["price"] if i else None
            series.append({**p, "percentage_change": percentage_change(old, p["price"])})
        return jsonify({**meta(), "prices": series, "availability": av})

    @app.get("/api/booking")
    def api_booking():
        user, err = require_user()
        if err:
            return err
        market = request.args.get("market")
        with get_db() as conn:
            params = []
            extra = ""
            if market:
                extra = "JOIN properties p ON p.id=e.property_id WHERE p.market=?"
                params.append(market)
            rows = rows_to_list(conn.execute(
                f"SELECT e.* FROM estimates e {extra} ORDER BY e.created_at DESC LIMIT 200", params
            ).fetchall())
        lead_buckets = {"0-3": 0, "4-7": 0, "8-14": 0, "15-30": 0, "30+": 0}
        # synthetic lead-time mix derived from occupancy estimates, labelled estimated
        for r in rows[:80]:
            occ = r.get("estimated_occupancy") or 50
            if occ > 85:
                lead_buckets["0-3"] += 1
            elif occ > 70:
                lead_buckets["4-7"] += 1
            elif occ > 60:
                lead_buckets["8-14"] += 1
            elif occ > 50:
                lead_buckets["15-30"] += 1
            else:
                lead_buckets["30+"] += 1
        return jsonify({
            **meta(),
            "kind": "estimated",
            "disclaimer": "Booking pace is inferred from availability tightness in demo snapshots, not from PMS booking records.",
            "lead_time": lead_buckets,
            "recent_estimates": rows[:40],
        })

    @app.get("/api/revenue")
    def api_revenue():
        user, err = require_user()
        if err:
            return err
        market = request.args.get("market")
        with get_db() as conn:
            params = []
            extra = ""
            if market:
                extra = "JOIN properties p ON p.id=e.property_id WHERE p.market=?"
                params.append(market)
            monthly = rows_to_list(conn.execute(
                f"""SELECT substr(e.created_at,1,7) AS month,
                           SUM(e.estimated_revenue) AS revenue,
                           AVG(e.estimated_occupancy) AS occupancy,
                           AVG(e.estimated_rooms_sold) AS rooms_sold
                    FROM estimates e {extra}
                    GROUP BY month ORDER BY month""",
                params,
            ).fetchall())
            latest = rows_to_list(conn.execute(
                f"""SELECT e.*, pr.name FROM estimates e
                    JOIN properties pr ON pr.id=e.property_id
                    {('AND' if extra else 'WHERE')} e.id IN (
                        SELECT MAX(id) FROM estimates GROUP BY property_id
                    ) {('AND pr.market=?' if market and not extra else '')}
                    LIMIT 50""",
                params if market else [],
            ).fetchall()) if False else rows_to_list(conn.execute(
                """SELECT e.*, pr.name FROM estimates e
                   JOIN properties pr ON pr.id=e.property_id
                   WHERE e.id IN (SELECT MAX(id) FROM estimates GROUP BY property_id)
                   ORDER BY e.estimated_revenue DESC"""
            ).fetchall())
        total = sum(r.get("estimated_revenue") or 0 for r in latest)
        occ = [r.get("estimated_occupancy") or 0 for r in latest]
        return jsonify({
            **meta(),
            "kind": "estimated",
            "methodology": "Estimated Revenue = Estimated Rooms Sold × observed ADR. Occupancy is inferred from availability status.",
            "total_estimated_revenue": round(total, 2),
            "avg_estimated_occupancy": round(sum(occ) / len(occ), 1) if occ else None,
            "monthly": monthly,
            "by_property": latest,
        })

    @app.get("/api/insights")
    def api_insights():
        user, err = require_user()
        if err:
            return err
        market = request.args.get("market")
        return jsonify({**meta(), "insights": generate_insights(market)})

    @app.get("/api/alerts")
    def api_alerts():
        user, err = require_user()
        if err:
            return err
        status = request.args.get("status")
        with get_db() as conn:
            q = "SELECT a.*, p.name AS property_name FROM alerts a LEFT JOIN properties p ON p.id=a.property_id"
            params = []
            if status:
                q += " WHERE a.status=?"
                params.append(status)
            q += " ORDER BY a.created_at DESC LIMIT 100"
            rows = rows_to_list(conn.execute(q, params).fetchall())
        return jsonify({**meta(), "items": rows})

    @app.post("/api/alerts/<int:aid>/status")
    def api_alert_status(aid):
        user, err = require_user()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        st = data.get("status") or "acknowledged"
        with get_db() as conn:
            conn.execute("UPDATE alerts SET status=? WHERE id=?", (st, aid))
        return jsonify({"ok": True})

    @app.post("/api/collection/run")
    def api_collection_run():
        user, err = require_user()
        if err:
            return err
        result = run_collection(note="Manual run from UI")
        return jsonify({**meta(), **result})

    @app.get("/api/collection/status")
    def api_collection_status():
        user, err = require_user()
        if err:
            return err
        with get_db() as conn:
            runs = rows_to_list(conn.execute("SELECT * FROM collection_runs ORDER BY id DESC LIMIT 10").fetchall())
            logs = rows_to_list(conn.execute("SELECT * FROM collection_logs ORDER BY id DESC LIMIT 80").fetchall())
        return jsonify({**meta(), "runs": runs, "logs": logs[::-1]})

    @app.get("/api/ml/status")
    def api_ml_status():
        user, err = require_user()
        if err:
            return err
        run = latest_run()
        if run and isinstance(run.get("metrics"), str):
            try:
                run["metrics"] = json.loads(run["metrics"])
            except json.JSONDecodeError:
                pass
        return jsonify({**meta(), "latest": run})

    @app.post("/api/ml/train")
    def api_ml_train():
        user, err = require_user()
        if err:
            return err
        try:
            result = train_model()
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({**meta(), **result})

    @app.post("/api/ml/predict")
    def api_ml_predict():
        user, err = require_user()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        pid = data.get("property_id")
        cin = data.get("check_in")
        cout = data.get("check_out")
        if not pid or not cin or not cout:
            return jsonify({"error": "property_id, check_in, check_out required"}), 400
        try:
            result = predict_demand(pid, cin, cout)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 400
        return jsonify({**meta(), **result})

    @app.get("/api/settings")
    def api_settings_get():
        user, err = require_user()
        if err:
            return err
        with get_db() as conn:
            rows = rows_to_list(conn.execute("SELECT key, value FROM settings").fetchall())
        return jsonify({**meta(), "settings": {r["key"]: r["value"] for r in rows}, "user": user})

    @app.post("/api/settings")
    def api_settings_post():
        user, err = require_user()
        if err:
            return err
        data = request.get_json(silent=True) or {}
        allowed = {"price_alert_threshold", "crawl_frequency", "default_market", "demo_mode"}
        with get_db() as conn:
            for k, v in data.items():
                if k in allowed:
                    conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (k, str(v)))
        return jsonify({"ok": True})

    @app.get("/api/export/competitors.csv")
    def api_export_csv():
        user, err = require_user()
        if err:
            return err
        with get_db() as conn:
            rows = rows_to_list(conn.execute("SELECT * FROM properties WHERE active=1 ORDER BY name").fetchall())
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["id"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return app.response_class(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=competitors.csv"})

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "service": "HotelIQ"})

    return app


app = create_app()
