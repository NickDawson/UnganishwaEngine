# Unganishwa Engine

Flask news aggregation backend for trusted East African sources. It provides RSS-first extraction, HTML fallback extraction, admin source curation, PostgreSQL analytics, monetization hooks, and the REST API used by the Flutter app.

## 1. Prerequisites

Install:

- Git
- Python 3.12 or newer
- PostgreSQL 14 or newer
- Visual Studio Code
- VS Code Python extension

Confirm the tools:

```powershell
git --version
py --version
psql --version
```

## 2. Clone in VS Code

Open VS Code, press `Ctrl+Shift+P`, choose `Git: Clone`, and enter:

```text
https://github.com/NickDawson/UnganishwaEngine.git
```

Choose a parent folder, then select `Open` when VS Code asks to open the cloned repository.

You can also clone from a terminal:

```powershell
git clone https://github.com/NickDawson/UnganishwaEngine.git
cd UnganishwaEngine
code .
```

## 3. Create the Python environment

In the VS Code terminal:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation, run the project interpreter directly:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

In VS Code, press `Ctrl+Shift+P`, choose `Python: Select Interpreter`, and select `.venv`.

## 4. Create the PostgreSQL database

Create a database named `unganishwa_db` using pgAdmin or `psql`:

```powershell
psql -U postgres
CREATE DATABASE unganishwa_db;
\q
```

## 5. Configure environment variables

Set these variables in the same VS Code terminal before starting the app:

```powershell
$env:FLASK_SECRET_KEY="replace-with-a-long-random-secret"
$env:UNGANISHWA_ADMIN_USER="admin"
$env:UNGANISHWA_ADMIN_KEY="replace-with-a-strong-admin-password"
$env:DATABASE_URL="postgresql://postgres:YOUR_PASSWORD@localhost:5432/unganishwa_db"
```

URL-encode special password characters. For example, `@` becomes `%40`.

Optional monetization settings:

```powershell
$env:GOOGLE_ADS_CLIENT="ca-pub-..."
$env:GOOGLE_ADS_SLOT="..."
$env:SPONSOR_NAME="Sponsor name"
$env:SPONSOR_URL="https://sponsor.example"
$env:AFFILIATE_URL="https://affiliate.example"
```

## 6. Migrate local SQLite data

If the repository contains existing SQLite data, migrate it after PostgreSQL is running:

```powershell
python migrate_to_postgres.py
```

The migration creates the analytics and trusted-source tables and preserves existing rows.

## 7. Run in VS Code

Start the Flask development server:

```powershell
python app.py
```

Open:

- Portal: `http://127.0.0.1:5000`
- Admin login: `http://127.0.0.1:5000/admin/login`
- API health: `http://127.0.0.1:5000/api/v1/health`
- Analytics: `http://127.0.0.1:5000/analytics`

Default admin username is `admin`. Use the value assigned to `UNGANISHWA_ADMIN_KEY` as the password.

## 8. API endpoints

- `GET /api/v1/health`
- `GET /api/v1/countries`
- `GET /api/v1/topics`
- `GET /api/v1/articles?country=tanzania&topic=Top%20Stories&page=1&limit=20`
- `GET /api/v1/search?q=business&page=1&limit=20`
- `POST /api/v1/subscribe`
- `POST /api/v1/notifications/subscribe`

## 9. Production run

```powershell
gunicorn app:app
```

Never commit passwords, connection strings, `.env` files, database files, or `.venv` directories.
