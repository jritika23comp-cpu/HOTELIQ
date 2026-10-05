from __future__ import annotations

import json
import math
import statistics
from datetime import datetime


def _prices(values: list[float]) -> list[float]:
    return [float(v) for v in values if v is not None]


def market_stats(prices: list[float]) -> dict:
    vals = _prices(prices)
    if not vals:
        return {
            "count": 0,
            "average": None,
            "median": None,
            "minimum": None,
            "maximum": None,
            "distribution": [],
        }
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    buckets = {"<10k": 0, "10-18k": 0, "18-28k": 0, "28k+": 0}
    for p in vals_sorted:
        if p < 10000:
            buckets["<10k"] += 1
        elif p < 18000:
            buckets["10-18k"] += 1
        elif p < 28000:
            buckets["18-28k"] += 1
        else:
            buckets["28k+"] += 1
    return {
        "count": n,
        "average": round(sum(vals_sorted) / n, 2),
        "median": round(statistics.median(vals_sorted), 2),
        "minimum": round(vals_sorted[0], 2),
        "maximum": round(vals_sorted[-1], 2),
        "distribution": [{"bucket": k, "count": v} for k, v in buckets.items()],
        "kind": "calculated",
    }


def percentage_change(old_value: float | None, new_value: float | None) -> float | None:
    if old_value in (None, 0) or new_value is None:
        return None
    return round(((new_value - old_value) / old_value) * 100, 2)


def competitor_score(rating: float | None, price: float | None, market_avg: float | None,
                     availability_status: str | None, price_change_pct: float | None) -> dict:
    """Transparent 0-100 score. Higher = stronger competitive position in the set."""
    score = 50.0
    breakdown = []

    if rating is not None:
        rating_pts = (rating / 5.0) * 20
        score += rating_pts - 10
        breakdown.append({"factor": "Rating", "points": round(rating_pts - 10, 2), "detail": f"Guest rating {rating}/5"})

    if price and market_avg:
        # Closer-to-or-below market average is more price-competitive
        delta = (market_avg - price) / market_avg
        pts = max(-15, min(15, delta * 40))
        score += pts
        breakdown.append({"factor": "Price competitiveness", "points": round(pts, 2), "detail": f"Price vs market average"})

    avail_map = {"available": 10, "limited": 2, "unavailable": -12}
    pts = avail_map.get((availability_status or "").lower(), 0)
    score += pts
    breakdown.append({"factor": "Availability", "points": pts, "detail": availability_status or "unknown"})

    if price_change_pct is not None:
        pts = max(-10, min(10, -price_change_pct / 2))
        score += pts
        breakdown.append({"factor": "Price movement", "points": round(pts, 2), "detail": f"{price_change_pct}% vs prior snapshot"})

    if market_avg and price:
        pos = "below market" if price < market_avg else "above market" if price > market_avg else "at market"
        breakdown.append({"factor": "Market position", "points": 0, "detail": pos})

    return {
        "score": round(max(0, min(100, score)), 1),
        "methodology": "Score starts at 50. Rating contributes up to ±10, price vs market ±15, availability +10/ +2/ -12, recent price movement ±10.",
        "breakdown": breakdown,
        "kind": "calculated",
    }


def occupancy_from_availability(status: str, total_rooms: int | None) -> dict:
    """Estimate occupancy from observed availability status. Not a confirmed booking count."""
    rooms = total_rooms or 20
    mapping = {"available": 0.55, "limited": 0.82, "unavailable": 0.97}
    occ = mapping.get((status or "available").lower(), 0.6)
    sold = round(rooms * occ, 1)
    return {
        "estimated_occupancy": round(occ * 100, 1),
        "estimated_rooms_sold": sold,
        "methodology": (
            "Estimated occupancy is inferred from the latest observed availability status "
            "(available≈55%, limited≈82%, unavailable≈97%) × listed room inventory. "
            "A drop in availability is NOT treated as a confirmed booking."
        ),
        "kind": "estimated",
    }


def estimated_revenue(rooms_sold: float, adr: float) -> dict:
    return {
        "estimated_revenue": round(rooms_sold * adr, 2),
        "adr": adr,
        "estimated_rooms_sold": rooms_sold,
        "methodology": "Estimated Revenue = Estimated Rooms Sold × Average observed room price (ADR). Demo observations only.",
        "kind": "estimated",
    }


def parse_iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", ""))


def json_dumps(obj) -> str:
    return json.dumps(obj, default=str)
