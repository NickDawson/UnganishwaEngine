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

### Country-specific translation

The website language selector offers languages for the selected country. `Original`
keeps publisher text and the existing interface; selecting a language translates
visible news, menus, buttons, placeholders and accessibility labels using Google
Cloud Translation Basic v2. Source language is detected automatically. Links,
scripts and language option values stay intact. Arabic uses right-to-left layout.

Enable the Cloud Translation API in your Google Cloud project and configure
`GOOGLE_TRANSLATE_API_KEY` in the server environment (never in frontend code or Git),
then restart the app. Google billing and quotas apply. Setup reference:
https://docs.cloud.google.com/translate/docs/basic/translating-text

`GET /api/v1/countries` includes each country's language names and codes.
`GET /api/v1/articles?country=somalia&language=so` translates article titles and
summaries; a language name also works. Omit `language` or use `Original` for original
content. An incompatible language returns 400. A missing key or provider error
returns 503 with `code: translation_unavailable`; the website instead displays
original content with a visible notice. Search and admin pages remain unchanged.
Successful translations have a bounded in-memory cache per worker; restarting the
worker clears it. Tests mock Google responses and do not incur API charges:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## Newsroom, daily categorization and WebSub

Newly collected or manually submitted articles are stored as **Uncategorized**
and are immediately visible on the public website/API. A source's configured
`topic` does not categorize its articles. The newsroom inbox `/admin/news` defaults
to all arrivals **received today**, using `NEWSROOM_TIMEZONE` (default
`Africa/Nairobi`). Clear the date or select **Show all dates** to review older items.
The received timestamp is separate from the publisher's publication date.

The administrator signs in with `UNGANISHWA_ADMIN_USER` / `UNGANISHWA_ADMIN_KEY`,
then uses **People & permissions** to create accounts:

- **Categorizer:** adds news and assigns categories within assigned countries and
  categories. Uncategorized news for assigned countries is available to classify.
- **Editor:** also hides articles from readers or returns them to Uncategorized.
- **Administrator:** manages all news, users, sources, WebSub and analytics.

Moving an article to a category takes effect immediately. A change history records
actor, old/new category, timestamp and optional note. Concurrent edits produce a
409 conflict instead of silently overwriting each other. Disabling a user or
changing permissions/password revokes existing sessions. Staff accounts never use
plaintext password storage. Set a stable, random `FLASK_SECRET_KEY` in production;
the development fallback changes at process restart and is unsuitable for multiple
workers. Admin POST forms require their session CSRF token.

### Collecting and receiving news

The website reads stored news; it does not crawl on each page request. Start the
background worker alongside the web process:

```sh
python news_worker.py
```

`Procfile` defines separate `web` and `worker` process types; enable both on your
hosting platform. `NEWS_POLL_SECONDS` defaults to 300 (minimum 60) and
`NEWS_POLL_WORKERS` defaults to 4 (maximum 8). The worker polls active RSS/Atom feeds,
falls back to the source website where configured, and renews WebSub leases hourly.
On existing installations the new inbox starts empty until collection runs; demo
stories and request-time source categories are no longer shown.

For a one-off collection use **Sources → Collect news**, or:

```sh
flask --app app collect-news
flask --app app renew-websub
```

In **WebSub**, connect an active feed. The publisher must advertise a hub and self
URL in HTTP Link headers or feed/HTML links. `PUBLIC_SITE_URL` must be the publicly
reachable HTTPS site address; allow GET/POST to `/websub/callback/<id>` through the
proxy. A connection is active only after the hub verifies the challenge. Signed
RSS/Atom deliveries enter Uncategorized; duplicate URLs within a country do not
reset editorial decisions. Invalid signatures, expired subscriptions and paused
sources are rejected. **Disconnect** requests hub unsubscribe verification.
Publishers without WebSub continue through polling. Implemented against:
https://www.w3.org/TR/websub/

The database automatically adds `newsroom_users`, `newsroom_articles`,
`newsroom_audit` and `websub_subscriptions` tables. SQLite and PostgreSQL use the
same application workflow. `UNGANISHWA_DATA_DIR` optionally changes the existing
SQLite database directory (must exist); it is also used to isolate automated tests.
Run a single collection worker per deployment. Collection errors are logged by
source ID; no provider credentials or fetched payloads are logged.

## Visitor-country analytics

**Analytics → Where visitors come from** measures the approximate visitor origin,
separately from **Region activity**, which describes the news edition selected.
New tables store country totals and anonymous browser hashes, not raw IP addresses.
Unknown/private addresses and unavailable geolocation are labeled **Unknown
country**. Country codes outside the covered news countries are displayed as ISO
codes. Historical visits cannot be geolocated retroactively.

Choose one geolocation setup:

1. **Cloudflare:** enable IP Geolocation and set `GEO_COUNTRY_HEADER=CF-IPCountry`.
   Set `GEO_TRUSTED_PROXY_CIDRS` to the comma-separated CIDRs of the immediate
   trusted proxy connections. The origin must accept these headers only from a
   proxy that overwrites them with verified Cloudflare values; block direct origin
   access or strip client-supplied headers. Do not trust arbitrary forwarding
   headers or configure a wildcard proxy range.
2. **Local GeoIP:** install requirements and set `GEOIP_DATABASE_PATH` to your
   maintained MaxMind GeoLite2 Country `.mmdb` file. Obtain that file through your
   MaxMind account. Direct requests use the peer IP; deployments behind proxies
   also need `GEO_TRUSTED_PROXY_CIDRS` so the verified forwarding chain can be used.

Country detection does not request browser GPS permission or send visitor IPs to
an external lookup service. VPNs/proxies can change the inferred country. The
same anonymous browser can appear under multiple countries over time.
Cloudflare setup: https://developers.cloudflare.com/network/ip-geolocation/
GeoLite data: https://dev.maxmind.com/geoip/geolite2-free-geolocation-data/
