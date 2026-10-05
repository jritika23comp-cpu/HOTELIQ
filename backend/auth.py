from __future__ import annotations

import os
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from .database import get_db, row_to_dict


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_default_user() -> None:
    email = os.getenv("DEFAULT_ADMIN_EMAIL", "revenue.lead@dellavillas.com")
    password = os.getenv("DEFAULT_ADMIN_PASSWORD", "hotelIQ-demo-2026")
    with get_db() as conn:
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if row:
            return
        conn.execute(
            "INSERT INTO users (email, password_hash, name, role, created_at) VALUES (?,?,?,?,?)",
            (email, generate_password_hash(password), "Rajesh Sharma", "RevPAR Strategist", utcnow()),
        )


def authenticate(email: str, password: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email.strip().lower(),)).fetchone()
    user = row_to_dict(row)
    if not user:
        return None
    if not check_password_hash(user["password_hash"], password):
        return None
    user.pop("password_hash", None)
    return user


def public_user(user: dict) -> dict:
    return {
        "id": user.get("id"),
        "email": user.get("email"),
        "name": user.get("name"),
        "role": user.get("role"),
    }
