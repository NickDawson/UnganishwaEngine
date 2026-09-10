# Unganishwa Engine

Flask news aggregation backend for trusted East African sources. It supports RSS-first extraction, HTML fallback extraction, admin source curation, PostgreSQL, analytics, monetization hooks, and a REST API for mobile clients.

## Clone

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/unganishwa-engine.git
cd unganishwa-engine
```

## Run locally

Create a virtual environment and install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set the required environment variables:

```powershell
$env:FLASK_SECRET_KEY="replace-with-a-long-random-secret"
$env:UNGANISHWA_ADMIN_USER="admin"
$env:UNGANISHWA_ADMIN_KEY="replace-with-a-strong-admin-password"
$env:DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/unganishwa_db"
```

The password in `DATABASE_URL` must be URL-encoded. For example, `@` becomes `%40`.

Start the development server:

```powershell
python app.py
```

Open `http://127.0.0.1:5000`.

## Migrate existing SQLite data

With PostgreSQL running and `DATABASE_URL` set:

```powershell
python migrate_to_postgres.py
```

## API

- `GET /api/v1/health`
- `GET /api/v1/countries`
- `GET /api/v1/topics`
- `GET /api/v1/articles?country=tanzania&topic=Top%20Stories&page=1&limit=20`
- `GET /api/v1/search?q=business&page=1&limit=20`
- `POST /api/v1/subscribe`
- `POST /api/v1/notifications/subscribe`

## Production

```bash
gunicorn app:app
```

Set `DATABASE_URL`, `FLASK_SECRET_KEY`, admin credentials, and any Google Ads or sponsor variables in the hosting provider’s secret environment settings. Do not commit passwords, connection strings, database files, or generated virtual environments.
