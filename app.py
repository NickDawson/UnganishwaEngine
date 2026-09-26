from datetime import datetime, timedelta, timezone
import secrets
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, abort, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from api import register_api
from editorial import Newsroom
from websub import WebSub
from ingestion import download, feed_articles
from visitor_geo import visitor_country
from translation import (COUNTRY_LANGUAGES, resolve_language, translate_page,
                         TranslationUnavailable)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
DATA_DIRECTORY = Path(os.environ.get('UNGANISHWA_DATA_DIR', str(Path(__file__).parent)))
DATABASE = DATA_DIRECTORY / 'unganishwa_analytics.sqlite3'
SOURCE_DATABASE = DATA_DIRECTORY / 'trusted_sources.sqlite3'
CURATED_SOURCES_FILE = Path(__file__).with_name('data') / 'east_africa_news_sources.json'
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
ADMIN_USERNAME = os.environ.get('UNGANISHWA_ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.environ.get('UNGANISHWA_ADMIN_KEY', 'admin123')
ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD, method='pbkdf2:sha256')
GOOGLE_ADS_CLIENT = os.environ.get('GOOGLE_ADS_CLIENT', '')
GOOGLE_ADS_SLOT = os.environ.get('GOOGLE_ADS_SLOT', '')
API_CORS_ORIGIN = os.environ.get('API_CORS_ORIGIN', '*')
SPONSOR_NAME = os.environ.get('SPONSOR_NAME', '')
SPONSOR_URL = os.environ.get('SPONSOR_URL', '')
AFFILIATE_LABEL = os.environ.get('AFFILIATE_LABEL', 'Recommended for our readers')
AFFILIATE_URL = os.environ.get('AFFILIATE_URL', '')
PUBLIC_SITE_URL = os.environ.get('PUBLIC_SITE_URL', 'https://www.unganishwa.com').rstrip('/')

COUNTRIES = {
    'tanzania': {'name': 'Tanzania', 'code': 'TZ', 'accent': 'teal'},
    'kenya': {'name': 'Kenya', 'code': 'KE', 'accent': 'coral'},
    'uganda': {'name': 'Uganda', 'code': 'UG', 'accent': 'gold'},
    'rwanda': {'name': 'Rwanda', 'code': 'RW', 'accent': 'blue'},
    'burundi': {'name': 'Burundi', 'code': 'BI', 'accent': 'green'},
    'congo': {'name': 'Congo (DRC)', 'code': 'CD', 'accent': 'blue'},
    'somalia': {'name': 'Somalia', 'code': 'SO', 'accent': 'teal'},
    'south-sudan': {'name': 'South Sudan', 'code': 'SS', 'accent': 'gold'},
}

TOPICS = ['Top Stories', 'World', 'National', 'Business', 'Technology',
          'Entertainment', 'Sports', 'Science', 'Health']
TOPIC_SLUGS = {topic: re.sub(r'[^a-z0-9]+', '-', topic.lower()).strip('-') for topic in TOPICS}
SLUG_TOPICS = {slug: topic for topic, slug in TOPIC_SLUGS.items()}

INFO_PAGES = {
    'about': {'title': 'About Unganishwa', 'intro': 'Unganishwa brings trusted East African reporting into one clear, reader-friendly briefing.', 'sections': [('Our purpose', 'We help readers follow the stories shaping Tanzania, Kenya, Uganda, Rwanda and Burundi without losing the local context.'), ('How it works', 'Our platform collects stories from reviewed publishers, organises them by region and topic, and always links readers to the original report.'), ('Our standard', 'Clarity, source transparency and regional relevance guide how stories appear on Unganishwa.')]},
    'privacy': {'title': 'Privacy Policy', 'intro': 'This policy explains the limited information Unganishwa uses to operate and improve the service.', 'sections': [('Information we use', 'We may store anonymous visit statistics, your preferences, and an email address only when you choose to subscribe.'), ('Cookies and analytics', 'A first-party cookie may distinguish returning visitors. We use aggregate analytics to understand which editions and topics are useful.'), ('External services', 'Articles and advertisements may link to third-party websites. Their own privacy policies apply after you leave Unganishwa.'), ('Your choices', 'You may request removal of subscription information by emailing hello@unganishwa.com.')]},
    'terms': {'title': 'Terms of Use', 'intro': 'By using Unganishwa, you agree to use the service lawfully and responsibly.', 'sections': [('News links', 'Unganishwa is a discovery platform. Linked publishers own and are responsible for their original reporting.'), ('Availability', 'We work to keep the service accurate and available, but feeds, links and features may change without notice.'), ('Acceptable use', 'Do not misuse the service, attempt unauthorised access, or interfere with other readers.')]},
    'editorial-policy': {'title': 'Editorial Policy', 'intro': 'Our editorial choices make regional news easier to discover while preserving source transparency.', 'sections': [('Source review', 'Sources are selected for relevance, publication consistency and a clear record of original reporting.'), ('Attribution', 'Every story names its publisher and links to the original page. Unganishwa summaries do not replace the full report.'), ('Independence', 'Advertising and sponsorship do not determine which stories are selected or how they are ranked.')]},
    'corrections': {'title': 'Corrections Policy', 'intro': 'Accuracy matters. We review credible correction requests promptly and transparently.', 'sections': [('Report an issue', 'Send the story title, link and a short explanation to hello@unganishwa.com.'), ('What we correct', 'We correct errors introduced by our summaries, labels or source information. Corrections to an original article should also be sent to its publisher.')]},
    'contact': {'title': 'Contact Unganishwa', 'intro': 'Questions, source suggestions, partnerships and correction requests are welcome.', 'sections': [('Email us', 'Write to hello@unganishwa.com and include enough detail for our team to respond efficiently.'), ('Source submissions', 'Publishers may send their website, RSS feed, country and coverage topics for editorial review.')]},
}



def db_connect(sqlite_path):
    if DATABASE_URL:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    return connection


def execute_sql(connection, query, parameters=()):
    if DATABASE_URL:
        query = query.replace('?', '%s')
        cursor = connection.cursor()
        cursor.execute(query, parameters)
        return cursor
    return connection.execute(query, parameters)


def get_db():
    if 'db' not in g:
        g.db = db_connect(DATABASE)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_analytics():
    with db_connect(DATABASE) as db:
        execute_sql(db, 'CREATE TABLE IF NOT EXISTS portal_stats (region TEXT PRIMARY KEY, visits INTEGER NOT NULL DEFAULT 0, visitors INTEGER NOT NULL DEFAULT 0)')
        execute_sql(db, '''
            CREATE TABLE IF NOT EXISTS anonymous_visitors (
                visitor_id TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                page_views INTEGER NOT NULL DEFAULT 0
            )
        ''')
        execute_sql(db, '''
            CREATE TABLE IF NOT EXISTS traffic_daily (
                day TEXT PRIMARY KEY,
                page_views INTEGER NOT NULL DEFAULT 0,
                unique_visitors INTEGER NOT NULL DEFAULT 0
            )
        ''')
        execute_sql(db, '''
            CREATE TABLE IF NOT EXISTS subscriptions (
                email TEXT PRIMARY KEY,
                subscribed_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'web'
            )
        ''')
        execute_sql(db, '''
            CREATE TABLE IF NOT EXISTS notification_subscriptions (
                token TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        ''')
        execute_sql(db, """CREATE TABLE IF NOT EXISTS visitor_country_daily (
            day TEXT NOT NULL, country_code TEXT NOT NULL, page_views INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, country_code))""")
        execute_sql(db, """CREATE TABLE IF NOT EXISTS visitor_country_browsers (
            day TEXT NOT NULL, country_code TEXT NOT NULL, visitor_hash TEXT NOT NULL,
            PRIMARY KEY (day, country_code, visitor_hash))""")
        execute_sql(db, '''CREATE TABLE IF NOT EXISTS reader_feedback (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, comment TEXT NOT NULL,
            country TEXT NOT NULL, created_at TEXT NOT NULL)''')
        for region in COUNTRIES:
            execute_sql(db, 'INSERT INTO portal_stats (region) VALUES (?) ON CONFLICT (region) DO NOTHING', (region,))
        db.commit()


def init_trusted_sources():
    id_definition = 'INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY' if DATABASE_URL else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    with db_connect(SOURCE_DATABASE) as db:
        execute_sql(db, f'''
            CREATE TABLE IF NOT EXISTS trusted_sources (
                id {id_definition},
                country TEXT NOT NULL,
                topic TEXT NOT NULL,
                source_name TEXT NOT NULL,
                feed_url TEXT,
                site_url TEXT,
                source_type TEXT NOT NULL DEFAULT 'rss',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        execute_sql(db, '''
            DELETE FROM trusted_sources
            WHERE id NOT IN (
                SELECT MIN(id)
                FROM trusted_sources
                GROUP BY country, topic, source_name, COALESCE(feed_url, ''), COALESCE(site_url, '')
            )
        ''')
        execute_sql(db, '''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trusted_sources_unique
            ON trusted_sources (country, topic, source_name, feed_url, site_url)
        ''')
        existing = execute_sql(db, 'SELECT COUNT(*) AS count FROM trusted_sources').fetchone()['count']
        if existing == 0:
            seeds = [
                ('tanzania', 'Top Stories', 'The Citizen', 'https://www.thecitizen.co.tz/tanzania/rss', 'https://www.thecitizen.co.tz/', 'rss', 1),
                ('tanzania', 'National', 'Mwananchi', 'https://www.mwananchi.co.tz/rss', 'https://www.mwananchi.co.tz/', 'rss', 1),
                ('kenya', 'Top Stories', 'The Standard', 'https://www.standardmedia.co.ke/rss/headlines.php', 'https://www.standardmedia.co.ke/', 'rss', 1),
                ('kenya', 'Business', 'Business Daily', 'https://www.businessdailyafrica.com/bd/rss', 'https://www.businessdailyafrica.com/', 'rss', 1),
                ('uganda', 'Top Stories', 'Daily Monitor', 'https://www.monitor.co.ug/uganda/rss', 'https://www.monitor.co.ug/', 'rss', 1),
                ('rwanda', 'Top Stories', 'The New Times', 'https://www.newtimes.co.rw/rss.xml', 'https://www.newtimes.co.rw/', 'rss', 1),
                ('burundi', 'Top Stories', 'Iwacu', 'https://www.iwacu-burundi.org/feed/', 'https://www.iwacu-burundi.org/', 'rss', 1),
            ]
            for seed in seeds:
                execute_sql(db,
                'INSERT INTO trusted_sources (country, topic, source_name, feed_url, site_url, source_type, is_active) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING',
                seed,
                )
            db.commit()

        if CURATED_SOURCES_FILE.exists():
            curated_sources = json.loads(CURATED_SOURCES_FILE.read_text(encoding='utf-8'))
            for source in curated_sources:
                existing_source = execute_sql(
                    db,
                    'SELECT id, feed_url FROM trusted_sources WHERE country = ? AND source_name = ? ORDER BY id LIMIT 1',
                    (source['country'], source['source_name']),
                ).fetchone()
                if existing_source:
                    # Do not reset administrator edits or paused sources on restart.
                    continue
                else:
                    execute_sql(
                        db,
                        'INSERT INTO trusted_sources (country, topic, source_name, feed_url, site_url, source_type, is_active) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING',
                        (source['country'], source['topic'], source['source_name'], source['feed_url'], source['site_url'], source['source_type'], source['is_active']),
                    )
            db.commit()


def deduplicate_articles(articles):
    seen = set()
    result = []
    for article in articles:
        normalized = normalize_article(article)
        link = (normalized.get('link') or '').strip().lower()
        title = (normalized.get('title') or '').strip().lower()
        key = link or title or (normalized.get('summary') or '').strip().lower()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


init_analytics()
init_trusted_sources()


def record_visit(region, is_new_visitor, visitor_id):
    db = get_db()
    execute_sql(db, 'UPDATE portal_stats SET visits = visits + 1, visitors = visitors + ? WHERE region = ?',
                (1 if is_new_visitor else 0, region))
    now = datetime.now(timezone.utc).isoformat()
    visitor_hash = hashlib.sha256(visitor_id.encode('utf-8')).hexdigest()
    execute_sql(db, '''
        INSERT INTO anonymous_visitors (visitor_id, first_seen, last_seen, page_views)
        VALUES (?, ?, ?, 1)
        ON CONFLICT (visitor_id) DO UPDATE SET
            last_seen = excluded.last_seen,
            page_views = anonymous_visitors.page_views + 1
    ''', (visitor_hash, now, now))
    day = now[:10]
    execute_sql(db, '''
        INSERT INTO traffic_daily (day, page_views, unique_visitors)
        VALUES (?, 1, ?)
        ON CONFLICT (day) DO UPDATE SET
            page_views = traffic_daily.page_views + 1,
            unique_visitors = traffic_daily.unique_visitors + ?
    ''', (day, 1 if is_new_visitor else 0, 0 if is_new_visitor else 0))
    origin = visitor_country(request)
    execute_sql(db, '''INSERT INTO visitor_country_daily (day, country_code, page_views)
        VALUES (?, ?, 1) ON CONFLICT (day, country_code) DO UPDATE SET
        page_views = visitor_country_daily.page_views + 1''', (day, origin))
    execute_sql(db, '''INSERT INTO visitor_country_browsers (day, country_code, visitor_hash)
        VALUES (?, ?, ?) ON CONFLICT DO NOTHING''', (day, origin, visitor_hash))
    db.commit()


@app.after_request
def add_visitor_cookie(response):
    if request.path.startswith('/api/'):
        response.headers['Access-Control-Allow-Origin'] = API_CORS_ORIGIN
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
    if getattr(g, 'new_visitor_id', None):
        response.set_cookie('unganishwa_visitor', g.new_visitor_id,
                            max_age=60 * 60 * 24 * 365, httponly=True,
                            samesite='Lax')
    return response


@app.before_request
def track_portal_visit():
    if (request.method != 'GET' or not request.endpoint or request.path.startswith(('/admin', '/websub/', '/static/'))
            or request.endpoint in ('analytics', 'api_health', 'api_countries', 'api_topics')):
        return
    region = (request.view_args or {}).get('country', request.args.get('country', 'tanzania')).lower()
    if region not in COUNTRIES:
        region = 'tanzania'
    visitor_id = request.cookies.get('unganishwa_visitor')
    is_new_visitor = not visitor_id
    if is_new_visitor:
        g.new_visitor_id = str(uuid.uuid4())
        visitor_id = g.new_visitor_id
    record_visit(region, is_new_visitor, visitor_id)


@app.before_request
def enforce_admin_session():
    return newsroom.authorize()


@app.context_processor
def inject_analytics_link():
    private_page = request.path.startswith('/admin') or request.path.startswith('/analytics')
    return {
        'analytics_url': '/analytics',
        'google_ads_client': GOOGLE_ADS_CLIENT,
        'google_ads_slot': GOOGLE_ADS_SLOT,
        'sponsor_name': SPONSOR_NAME,
        'sponsor_url': SPONSOR_URL,
        'affiliate_label': AFFILIATE_LABEL,
        'affiliate_url': AFFILIATE_URL,
        'public_site_url': PUBLIC_SITE_URL,
        'default_robots_meta': 'noindex,nofollow' if private_page else 'index,follow,max-image-preview:large',
        'site_schema': {
            '@context': 'https://schema.org',
            '@graph': [
                {'@type': 'Organization', '@id': f'{PUBLIC_SITE_URL}/#organization', 'name': 'Unganishwa', 'url': f'{PUBLIC_SITE_URL}/', 'email': 'hello@unganishwa.com'},
                {'@type': 'WebSite', '@id': f'{PUBLIC_SITE_URL}/#website', 'name': 'Unganishwa', 'url': f'{PUBLIC_SITE_URL}/', 'publisher': {'@id': f'{PUBLIC_SITE_URL}/#organization'}},
            ],
        },
    }


@app.route('/subscribe', methods=['POST'])
def subscribe():
    email = request.form.get('email', '').strip().lower()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        return redirect(url_for('index', subscribed='invalid'))
    now = datetime.now(timezone.utc).isoformat()
    with db_connect(DATABASE) as db:
        execute_sql(db, '''
            INSERT INTO subscriptions (email, subscribed_at, source)
            VALUES (?, ?, 'web')
            ON CONFLICT (email) DO UPDATE SET subscribed_at = excluded.subscribed_at
        ''', (email, now))
        db.commit()
    return redirect(url_for('index', subscribed='success'))


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if newsroom.current_user():
        return redirect(url_for('newsroom.inbox'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if newsroom.authenticate(username, password):
            return redirect(url_for('newsroom.inbox'))
        return render_template('admin_login.html', error='Invalid username or password.')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('admin_login'))


@app.route('/admin/sources', methods=['GET', 'POST'])
def admin_sources():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        country = request.form.get('country', '').lower()
        topic = request.form.get('topic', 'Top Stories')
        source_name = request.form.get('source_name', '').strip()
        feed_url = request.form.get('feed_url', '').strip()
        site_url = request.form.get('site_url', '').strip()
        source_type = request.form.get('source_type', 'rss').lower()

        if country not in COUNTRIES or topic not in TOPICS or not source_name or not (feed_url or site_url):
            page_data = get_admin_source_page()
            return render_template('admin_sources.html', countries=COUNTRIES, topics=TOPICS,
                                   error='Please provide a valid country, topic, source name, and at least one source URL.',
                                   **page_data)

        with db_connect(SOURCE_DATABASE) as db:
            execute_sql(db,
                        'INSERT INTO trusted_sources (country, topic, source_name, feed_url, site_url, source_type, is_active) VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT DO NOTHING',
                        (country, topic, source_name, feed_url or None, site_url or None, source_type))
            db.commit()
        return redirect(url_for('admin_sources', added='1'))

    return render_template('admin_sources.html', countries=COUNTRIES, topics=TOPICS,
                           **get_admin_source_page())


def get_admin_source_page():
    search_query = request.args.get('q', '').strip()
    country_filter = request.args.get('country', '').lower()
    topic_filter = request.args.get('topic', '')
    status_filter = request.args.get('status', 'all').lower()
    try:
        requested_page = max(1, int(request.args.get('page', '1')))
    except ValueError:
        requested_page = 1
    page_size = 10

    filters = []
    params = []
    if search_query:
        filters.append('(LOWER(source_name) LIKE ? OR LOWER(COALESCE(site_url, \'\')) LIKE ?)')
        search_pattern = f'%{search_query.lower()}%'
        params.extend([search_pattern, search_pattern])
    if country_filter in COUNTRIES:
        filters.append('country = ?')
        params.append(country_filter)
    else:
        country_filter = ''
    if topic_filter in TOPICS:
        filters.append('topic = ?')
        params.append(topic_filter)
    else:
        topic_filter = ''
    if status_filter == 'active':
        filters.append('is_active = 1')
    elif status_filter == 'paused':
        filters.append('is_active = 0')
    else:
        status_filter = 'all'

    where_clause = f" WHERE {' AND '.join(filters)}" if filters else ''
    with db_connect(SOURCE_DATABASE) as db:
        total_filtered = execute_sql(db, f'SELECT COUNT(*) AS count FROM trusted_sources{where_clause}', params).fetchone()['count']
        total_pages = max(1, (total_filtered + page_size - 1) // page_size)
        current_page = min(requested_page, total_pages)
        offset = (current_page - 1) * page_size
        sources = execute_sql(
            db,
            f'''SELECT * FROM trusted_sources{where_clause}
                ORDER BY country, source_name
                LIMIT ? OFFSET ?''',
            [*params, page_size, offset],
        ).fetchall()
        summary = execute_sql(db, '''
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END), 0) AS active,
                   COALESCE(SUM(CASE WHEN feed_url IS NOT NULL AND feed_url != '' THEN 1 ELSE 0 END), 0) AS rss
            FROM trusted_sources
        ''').fetchone()

    return {
        'sources': sources,
        'summary': summary,
        'total_filtered': total_filtered,
        'page': current_page,
        'total_pages': total_pages,
        'page_size': page_size,
        'search_query': search_query,
        'country_filter': country_filter,
        'topic_filter': topic_filter,
        'status_filter': status_filter,
    }


@app.route('/admin/sources/edit/<int:source_id>', methods=['GET', 'POST'])
def admin_edit_source(source_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))

    db = db_connect(SOURCE_DATABASE)
    source = execute_sql(db, 'SELECT * FROM trusted_sources WHERE id = ?', (source_id,)).fetchone()
    db.close()

    if request.method == 'POST':
        country = request.form.get('country', '').lower()
        topic = request.form.get('topic', 'Top Stories')
        source_name = request.form.get('source_name', '').strip()
        feed_url = request.form.get('feed_url', '').strip()
        site_url = request.form.get('site_url', '').strip()
        source_type = request.form.get('source_type', 'rss').lower()
        is_active = 1 if request.form.get('is_active') == 'on' else 0

        if not source or country not in COUNTRIES or topic not in TOPICS or not source_name or not (feed_url or site_url):
            return render_template('admin_edit_source.html', source=source, countries=COUNTRIES, topics=TOPICS,
                                   error='Please provide valid data for the source revision.')

        with db_connect(SOURCE_DATABASE) as db:
            execute_sql(db,
                'UPDATE trusted_sources SET country = ?, topic = ?, source_name = ?, feed_url = ?, site_url = ?, source_type = ?, is_active = ? WHERE id = ?',
                (country, topic, source_name, feed_url or None, site_url or None, source_type, is_active, source_id),
            )
            db.commit()
        return redirect(url_for('admin_sources'))

    return render_template('admin_edit_source.html', source=source, countries=COUNTRIES, topics=TOPICS)


@app.route('/admin/sources/toggle/<int:source_id>', methods=['POST'])
def admin_toggle_source(source_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    with db_connect(SOURCE_DATABASE) as db:
        row = execute_sql(db, 'SELECT is_active FROM trusted_sources WHERE id = ?', (source_id,)).fetchone()
        if row:
            execute_sql(db, 'UPDATE trusted_sources SET is_active = ? WHERE id = ?', (0 if row['is_active'] else 1, source_id))
            db.commit()
    return redirect(url_for('admin_sources'))


@app.route('/admin/sources/delete/<int:source_id>', methods=['POST'])
def admin_delete_source(source_id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_login'))
    with db_connect(SOURCE_DATABASE) as db:
        execute_sql(db, 'DELETE FROM trusted_sources WHERE id = ?', (source_id,))
        db.commit()
    return redirect(url_for('admin_sources'))


@app.route('/')
def index():
    country = request.args.get('country', 'tanzania').lower()
    if country not in COUNTRIES:
        country = 'tanzania'
    topic = request.args.get('topic', 'Top Stories')
    if topic not in TOPICS:
        topic = 'Top Stories'
    language = request.args.get('language', 'Original')
    return render_edition(country, topic, language)


@app.route('/news/<country>/<topic_slug>')
def edition(country, topic_slug):
    country = country.lower()
    topic = SLUG_TOPICS.get(topic_slug.lower())
    if country not in COUNTRIES or not topic:
        abort(404)
    return render_edition(country, topic, request.args.get('language', 'Original'))


def render_edition(country, topic, language='Original'):
    language = resolve_language(country, language) or 'Original'
    articles = load_articles(country, topic)
    timestamps = [datetime.fromisoformat(article['updated_at']).astimezone(timezone.utc)
                  for article in articles if article.get('updated_at')]
    updated = max(timestamps).strftime('%d %b %Y · %H:%M') if timestamps else None
    country_name = COUNTRIES[country]['name']
    canonical_path = '/' if country == 'tanzania' and topic == 'Top Stories' else f'/news/{country}/{TOPIC_SLUGS[topic]}'
    seo_title = ('Unganishwa | East Africa News and Daily Briefings' if canonical_path == '/'
                 else f'{country_name} {topic} News | Unganishwa')
    seo_description = (f'Latest {topic.lower()} news from {country_name}, curated from trusted East African publishers. '
                       'Read a clear daily briefing and visit the original sources.')
    item_list = {'@context': 'https://schema.org', '@type': 'ItemList',
                 'name': f'{country_name} {topic} news',
                 'itemListElement': [
                     {'@type': 'ListItem', 'position': position, 'name': article['title'], 'url': article['link']}
                     for position, article in enumerate(articles, start=1)
                     if article.get('link') and article['link'] != '#']}
    html = render_template('index.html', articles=articles, country=country,
                           country_info=COUNTRIES[country], countries=COUNTRIES,
                           topics=TOPICS, topic=topic, language=language,
                           languages=['Original', *COUNTRY_LANGUAGES[country]],
                           interests=['Top Stories', 'Business', 'Technology'],
                           updated=updated, topic_slugs=TOPIC_SLUGS,
                           seo_title=seo_title, seo_description=seo_description,
                           canonical_url=f'{PUBLIC_SITE_URL}{canonical_path}', structured_data=item_list)
    try:
        return translate_page(html, language)
    except TranslationUnavailable:
        notice = ('<p class="container" role="status">Translation is temporarily unavailable. '
                  'Showing original text. / Tafsiri haipatikani kwa sasa; habari ziko katika lugha ya asili.</p>')
        return html.replace('<main class="container editorial-page">',
                            '<main class="container editorial-page">' + notice, 1)



#Search function
@app.route('/search')
def search():
    query = request.args.get('q', '').strip()
    articles = []
    for country in COUNTRIES:
        articles.extend(load_articles(country, 'Top Stories'))
    articles = [article for article in articles if query.lower() in
                (article['title'] + article['summary']).lower()]
    return render_template('search_results.html', articles=articles, query=query,
                           countries=COUNTRIES, topics=TOPICS,
                           seo_title=f'Search results for {query} | Unganishwa' if query else 'Search | Unganishwa',
                           seo_description='Search trusted East African news sources on Unganishwa.',
                           canonical_url=f'{PUBLIC_SITE_URL}/search', robots_meta='noindex,follow')


@app.route('/robots.txt')
def robots_txt():
    body = (f'User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /analytics\n'
            f'Sitemap: {PUBLIC_SITE_URL}/sitemap.xml\n')
    return Response(body, mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap_xml():
    paths = ['/']
    paths.extend(f'/news/{country}/{TOPIC_SLUGS[topic]}' for country in COUNTRIES for topic in TOPICS
                 if not (country == 'tanzania' and topic == 'Top Stories'))
    paths.extend(f'/{slug}' for slug in INFO_PAGES)
    urls = ''.join(f'<url><loc>{PUBLIC_SITE_URL}{path}</loc></url>' for path in paths)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>', mimetype='application/xml')


@app.route('/about')
@app.route('/privacy')
@app.route('/terms')
@app.route('/editorial-policy')
@app.route('/corrections')
@app.route('/contact')
def information_page():
    slug = request.path.strip('/')
    page = INFO_PAGES[slug]
    return render_template('info_page.html', page=page, seo_title=f"{page['title']} | Unganishwa",
                           seo_description=page['intro'], canonical_url=f'{PUBLIC_SITE_URL}/{slug}')


@app.route('/admin/feedback')
def admin_feedback():
    term = request.args.get('q', '').strip()[:200]
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1
    where = ''
    params = []
    if term:
        where = ' WHERE LOWER(name) LIKE ? OR LOWER(country) LIKE ? OR LOWER(comment) LIKE ?'
        params = ['%' + term.lower() + '%'] * 3
    db = get_db()
    total = execute_sql(db, 'SELECT COUNT(*) AS count FROM reader_feedback' + where, params).fetchone()['count']
    pages = max(1, (total + 19) // 20)
    page = min(page, pages)
    feedback = execute_sql(db, 'SELECT name, country, comment, created_at FROM reader_feedback' + where +
                           ' ORDER BY created_at DESC, id DESC LIMIT 20 OFFSET ?',
                           [*params, (page - 1) * 20]).fetchall()
    return render_template('admin_feedback.html', feedback=feedback, total=total,
                           page=page, pages=pages, q=term)


@app.route('/analytics')
def analytics():
    rows = execute_sql(get_db(), 'SELECT region, visits, visitors FROM portal_stats ORDER BY visits DESC').fetchall()
    traffic = execute_sql(get_db(), '''
        SELECT COALESCE(SUM(page_views), 0) AS page_views
        FROM traffic_daily
    ''').fetchone()
    daily_traffic = execute_sql(
        get_db(),
        'SELECT COALESCE(unique_visitors, 0) AS unique_visitors FROM traffic_daily WHERE day = ?',
        (datetime.now(timezone.utc).date().isoformat(),),
    ).fetchone()
    total_visits = traffic['page_views']
    total_visitors = execute_sql(get_db(), 'SELECT COUNT(*) AS count FROM anonymous_visitors').fetchone()['count']
    origin_rows = execute_sql(get_db(), '''SELECT d.country_code, SUM(d.page_views) AS visits,
        (SELECT COUNT(DISTINCT b.visitor_hash) FROM visitor_country_browsers b
         WHERE b.country_code = d.country_code) AS visitors
        FROM visitor_country_daily d GROUP BY d.country_code ORDER BY visits DESC''').fetchall()
    origin_names = {info['code']: info['name'] for info in COUNTRIES.values()}
    origin_names['Unknown'] = 'Diaspora'
    leader = rows[0] if rows and rows[0]['visits'] else None
    return render_template('analytics.html', stats=rows, total_visits=total_visits,
                           total_visitors=total_visitors,
                           daily_unique_visitors=daily_traffic['unique_visitors'] if daily_traffic else 0,
                           leader=leader, origin_rows=origin_rows, origin_names=origin_names,
                           geo_configured=bool(os.environ.get('GEOIP_DATABASE_PATH') or
                               (os.environ.get('GEO_COUNTRY_HEADER') and os.environ.get('GEO_TRUSTED_PROXY_CIDRS'))),
                           countries=COUNTRIES)


def normalize_article(article):
    cleaned = article.copy()
    cleaned['title'] = (cleaned.get('title') or 'Untitled story').strip()
    cleaned['summary'] = (cleaned.get('summary') or 'Read the full report from the original source.').strip()
    cleaned['source'] = (cleaned.get('source') or 'Source').strip()
    cleaned['link'] = cleaned.get('link') or '#'
    cleaned['published'] = cleaned.get('published') or 'Recently'
    return cleaned


def fetch_html(url):
    payload, _, _ = download(url)
    return payload.decode('utf-8', errors='replace')


def extract_html_articles(site_url, source_name, country, feed_topic):
    html = fetch_html(site_url)
    if not html:
        return []
    candidates = []
    seen = set()
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup.select('script, style, noscript, template, nav, footer, form'):
        node.decompose()

    for link in soup.select('article a, h2 a, h3 a, .story a, .headline a, .item a, .news-item a'):
        href = link.get('href') or ''
        text = ' '.join(link.stripped_strings)
        if not href or len(text) < 20 or len(text) > 180:
            continue
        full_url = urljoin(site_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        # Stay inside this story card; ancestor-wide text mixes unrelated stories
        # and can include page CSS before truncation.
        card = link.find_parent('article') or link.find_parent(['li', 'div', 'section'])
        summary = ''
        if card:
            excerpt = card.select_one('.elementor-post__excerpt, .entry-summary, .excerpt, .summary, .description')
            nodes = [excerpt] if excerpt else card.select('p')
            summary = ' '.join(' '.join(node.stripped_strings) for node in nodes)
        summary = ' '.join(summary.split())
        if not summary or summary == text:
            summary = 'Read the full report from the original source.'
        candidates.append({
            'title': text,
            'summary': summary[:220],
            'source': source_name,
            'country': country,
            'topic': feed_topic,
            'link': full_url,
            'published': 'Recently',
        })
    return deduplicate_articles(candidates[:8])


def load_articles(country, topic):
    return newsroom.public_articles(country, topic)


def collect_news_source(source):
    articles = []
    if source['feed_url']:
        try:
            payload, _, _ = download(source['feed_url'])
            articles = feed_articles(payload, source)
        except (requests.RequestException, OSError, ValueError):
            if not source['site_url']:
                raise
    if not articles and source['site_url']:
        articles = extract_html_articles(source['site_url'], source['source_name'], source['country'], 'Uncategorized')
    return newsroom.ingest(source, articles)


newsroom = Newsroom(app, db_connect, execute_sql, SOURCE_DATABASE, COUNTRIES, TOPICS,
                    ADMIN_USERNAME, ADMIN_PASSWORD_HASH, postgres=bool(DATABASE_URL))
newsroom.collect_source = collect_news_source
websub = WebSub(app, newsroom, PUBLIC_SITE_URL)


register_api(app, COUNTRIES, TOPICS, load_articles, deduplicate_articles,
             API_CORS_ORIGIN, get_db, execute_sql, DATABASE)


if __name__ == '__main__':
    init_analytics()
    app.run(debug=True)
