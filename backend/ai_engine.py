from __future__ import annotations

import os
from statistics import median

from .analytics import competitor_score, market_stats, occupancy_from_availability, percentage_change
from .database import get_db, row_to_dict, rows_to_list


def _latest_prices(conn, market: str | None = None) -> list[dict]:
    q = """
        SELECT p.*, ps.price, ps.observed_at AS price_observed_at, ps.source AS price_source,
               av.status AS availability_status, av.observed_at AS availability_observed_at
        FROM properties p
        LEFT JOIN price_snapshots ps ON ps.id = (
            SELECT id FROM price_snapshots WHERE property_id = p.id ORDER BY observed_at DESC LIMIT 1
        )
        LEFT JOIN availability_snapshots av ON av.id = (
            SELECT id FROM availability_snapshots WHERE property_id = p.id ORDER BY observed_at DESC LIMIT 1
        )
        WHERE p.active = 1
    """
    params: list = []
    if market:
        q += " AND p.market = ?"
        params.append(market)
    return rows_to_list(conn.execute(q, params).fetchall())


def generate_insights(market: str | None = None) -> list[dict]:
    with get_db() as conn:
        rows = _latest_prices(conn, market)
        changes = rows_to_list(conn.execute(
            """SELECT dc.*, p.name FROM detected_changes dc
               JOIN properties p ON p.id = dc.property_id
               WHERE dc.change_type = 'price'
               ORDER BY dc.detected_at DESC LIMIT 40"""
        ).fetchall())
        run = row_to_dict(conn.execute("SELECT * FROM model_runs ORDER BY id DESC LIMIT 1").fetchone())

    prices = [r["price"] for r in rows if r.get("price") is not None]
    stats = market_stats(prices)
    insights = []

    up = [c for c in changes if (c.get("percentage_change") or 0) > 0]
    down = [c for c in changes if (c.get("percentage_change") or 0) < 0]
    avg_up = round(sum(c["percentage_change"] for c in up) / len(up), 2) if up else 0

    if stats["median"]:
        insights.append({
            "title": "Market median from latest demo snapshots",
            "observed": f"Latest observed prices cover {stats['count']} active properties.",
            "calculated": f"Average ₹{stats['average']:,.0f}, median ₹{stats['median']:,.0f}, range ₹{stats['minimum']:,.0f}–₹{stats['maximum']:,.0f}.",
            "ai": "Several listed competitors sit around the median; properties priced well below median may be leaving revenue on the table, while those far above median need occupancy evidence before holding rate.",
            "kind": "ai_generated",
            "confidence_note": "Narrative is generated from calculated market statistics, not an external LLM unless configured.",
            "supporting": stats,
        })

    if up:
        insights.append({
            "title": "Upward price movement detected",
            "observed": f"{len(up)} recent price-increase change records in the database.",
            "calculated": f"Average increase among those records is {avg_up}%.",
            "ai": "When multiple competitors raise demo rates together, the set is signalling stronger weekend/event pricing pressure. Review your target ADR against the new median rather than last week's rate.",
            "kind": "ai_generated",
            "supporting": {"sample": up[:5]},
        })

    sold_out = [r for r in rows if (r.get("availability_status") or "") == "unavailable"]
    if sold_out:
        names = ", ".join(r["name"] for r in sold_out[:4])
        insights.append({
            "title": "Observed unavailability in the competitor set",
            "observed": f"{len(sold_out)} properties currently show status=unavailable, including {names}.",
            "calculated": "Unavailability share of the active set is "
                          f"{round(len(sold_out)/max(len(rows),1)*100,1)}%.",
            "ai": "Treat this as compression of visible inventory, not proof that those rooms were booked. Residual demand may still exist on other channels or room types.",
            "kind": "ai_generated",
            "supporting": {"count": len(sold_out)},
        })

    if run and run.get("metrics"):
        insights.append({
            "title": "Demand model is trained on this workspace's observations",
            "observed": f"Latest model version {run['version']} trained {run['training_date']}.",
            "calculated": run["metrics"],
            "ai": "Use the demand score as a relative 0–100 proxy. It is not a guarantee of occupancy. Retrain after each collection so metrics stay honest to the current demo set.",
            "kind": "ai_generated",
        })

    if not insights:
        insights.append({
            "title": "Insufficient observations",
            "observed": "No price snapshots were found for this market.",
            "calculated": "Market statistics cannot be computed.",
            "ai": "Run data collection to generate the controlled demo dataset, then refresh insights.",
            "kind": "ai_generated",
        })

    # Optional LLM layer — only if a key exists; still grounded in stats above
    api_key = os.getenv("OPENAI_API_KEY") or ""
    if api_key.strip():
        insights[0]["llm_note"] = "OPENAI_API_KEY is set but insights remain grounded in local metrics; LLM rewrite is skipped in v1 to avoid inventing facts."

    return insights
