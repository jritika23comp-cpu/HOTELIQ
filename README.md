# HotelIQ

AI-assisted hotel competitor intelligence workspace. The application UI is in `frontend/pages`, connected to a Flask + SQLite backend. The root `index.html` is a startup page for users who open the project with a static server.

## What this version does

- Authenticates users (session cookie, hashed passwords)
- Stores 36 real competitor/portfolio properties (Lonavala, Karjat, Goa)
- Records **timestamped DEMO price and availability snapshots**
- Calculates market stats, price change %, competitor scores
- Estimates occupancy/revenue with documented assumptions
- Trains a Random Forest **demand-score** model with real MAE / RMSE / R²
- Generates AI insights from stored metrics (does not invent OTA facts)
- Creates alerts for price, availability, market, and demand

**DEMO MODE is always labelled.** Observations are a controlled dataset. They are not live MakeMyTrip scrapes. Availability going to “unavailable” is not treated as a confirmed booking.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python run.py
```

Open http://127.0.0.1:5000

Do not use VS Code Live Server to run the application. The pages under `frontend/pages` are Flask/Jinja templates and require the backend for rendering, authentication, and API data. If Live Server opens the root `index.html`, follow its instructions to start Flask with `python run.py`.

Default login:

- Email: `revenue.lead@dellavillas.com`
- Password: `hotelIQ-demo-2026`

First boot seeds the database, writes historical demo snapshots, and trains the model (can take a minute).

## Pages

These paths are Flask routes rendered from `frontend/pages/`. The authenticated login is `/index.html`; the root `index.html` is only a startup guide and is not the login template.

| File | Purpose |
| --- | --- |
| `/index.html` | Login |
| `dashboard.html` | KPIs, chart, alerts |
| `competitors.html` | Search, sort, pagination |
| `competitor-detail.html` | Profile + history |
| `pricing.html` | Price vs market |
| `availability.html` | Availability grid |
| `booking-intelligence.html` | Estimated pace |
| `revenue.html` | Estimated GBV |
| `historical.html` | Timestamped series |
| `ai-insights.html` | Data-backed narratives |
| `alerts.html` | Alert inbox |
| `data-collection.html` | Run demo collection |
| `properties.html` | Enable/disable tracking |
| `add-property.html` | Create property |
| `ml-models.html` | Train / predict |
| `settings.html` | Thresholds |

## API (authenticated)

- `POST /api/auth/login` `POST /api/auth/logout` `GET /api/auth/me`
- `GET /api/dashboard`
- `GET|POST /api/competitors` `GET|PUT|DELETE /api/competitors/:id`
- `GET /api/pricing` `GET /api/availability` `GET /api/history/:propertyId`
- `GET /api/booking` `GET /api/revenue` `GET /api/insights` `GET /api/alerts`
- `POST /api/collection/run` `GET /api/collection/status`
- `POST /api/ml/train` `POST /api/ml/predict` `GET /api/ml/status`

## Deploy

Set `FLASK_SECRET_KEY` and `DEFAULT_ADMIN_PASSWORD` in the host environment. Run with gunicorn:

```bash
gunicorn -w 2 -b 0.0.0.0:8000 backend.app:app
```

Do not commit `.env`. SQLite file is created at `data/hoteliq.db` at runtime.
