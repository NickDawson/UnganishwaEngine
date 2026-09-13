from datetime import datetime, timedelta, timezone
import hashlib
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, g, redirect, render_template, request, session, url_for
from scrapy import Selector
from werkzeug.security import check_password_hash, generate_password_hash
from api import register_api

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'unganishwa-admin-secret')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(minutes=30)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
DATABASE = Path(__file__).with_name('unganishwa_analytics.sqlite3')
SOURCE_DATABASE = Path(__file__).with_name('trusted_sources.sqlite3')
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

COUNTRIES = {
    'tanzania': {'name': 'Tanzania', 'code': 'TZ', 'accent': 'teal'},
    'kenya': {'name': 'Kenya', 'code': 'KE', 'accent': 'coral'},
    'uganda': {'name': 'Uganda', 'code': 'UG', 'accent': 'gold'},
    'rwanda': {'name': 'Rwanda', 'code': 'RW', 'accent': 'blue'},
    'burundi': {'name': 'Burundi', 'code': 'BI', 'accent': 'green'},
}

TOPICS = ['Top Stories', 'World', 'National', 'Business', 'Technology',
          'Entertainment', 'Sports', 'Science', 'Health']

# Feeds are intentionally kept editable: a source can be added without changing the UI.
RSS_FEEDS = {
    'tanzania': {'Top Stories': {'The Citizen': 'https://www.thecitizen.co.tz/tanzania/rss'},
                 'National': {'Mwananchi': 'https://www.mwananchi.co.tz/rss'}},
    'kenya': {'Top Stories': {'The Standard': 'https://www.standardmedia.co.ke/rss/headlines.php'},
              'Business': {'Business Daily': 'https://www.businessdailyafrica.com/bd/rss'}},
    'uganda': {'Top Stories': {'Daily Monitor': 'https://www.monitor.co.ug/uganda/rss'}},
    'rwanda': {'Top Stories': {'The New Times': 'https://www.newtimes.co.rw/rss.xml'}},
    'burundi': {'Top Stories': {'Iwacu': 'https://www.iwacu-burundi.org/feed/'}},
}

DEMO_ARTICLES = [
    {'title': 'East Africa puts local innovation at the centre of a connected future', 'summary': 'New partnerships are helping founders, communities and public services turn practical ideas into everyday progress.', 'source': 'Unganishwa Desk', 'topic': 'Technology', 'country': 'tanzania', 'minutes': '5 min read', 'published': 'Today', 'link': 'https://www.thecitizen.co.tz/'},
    {'title': 'Regional markets watch food prices as harvest season approaches', 'summary': 'Traders and households are tracking supply, transport costs and the latest market signals across the region.', 'source': 'The East African', 'topic': 'Business', 'country': 'kenya', 'minutes': '4 min read', 'published': 'Today', 'link': 'https://www.theeastafrican.co.ke/'},
    {'title': 'A new generation of creators is reshaping East African entertainment', 'summary': 'Music, film and digital storytelling are travelling further than ever, bringing local voices to global audiences.', 'source': 'Culture Wire', 'topic': 'Entertainment', 'country': 'uganda', 'minutes': '6 min read', 'published': 'Yesterday', 'link': 'https://www.monitor.co.ug/'},
    {'title': 'What healthier cities could look like for the Great Lakes region', 'summary': 'Urban planners and health workers are exploring simple changes that make daily life safer and more active.', 'source': 'Health Africa', 'topic': 'Health', 'country': 'rwanda', 'minutes': '7 min read', 'published': 'Yesterday', 'link': 'https://www.newtimes.co.rw/'},
    {'title': 'The community projects quietly changing life in Burundi', 'summary': 'Local groups are building new paths to opportunity through education, farming and neighbourhood collaboration.', 'source': 'Iwacu', 'topic': 'National', 'country': 'burundi', 'minutes': '5 min read', 'published': '2 days ago', 'link': 'https://www.iwacu-burundi.org/'},
]


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
    if not request.endpoint or request.endpoint.startswith('admin_') or request.endpoint == 'analytics':
        return
    region = request.args.get('country', 'tanzania').lower()
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
    if request.endpoint and request.endpoint.startswith('admin_'):
        if request.endpoint not in {'admin_login'} and not session.get('admin_logged_in'):
            return redirect(url_for('admin_login'))
        if session.get('admin_logged_in'):
            session_lifetime = 30 * 60
            last_seen = session.get('admin_last_seen', 0)
            if time.time() - last_seen > session_lifetime:
                session.pop('admin_logged_in', None)
                session.pop('admin_user', None)
                session.pop('admin_last_seen', None)
                return redirect(url_for('admin_login'))
            session['admin_last_seen'] = time.time()


@app.context_processor
def inject_analytics_link():
    return {
        'analytics_url': '/analytics',
        'google_ads_client': GOOGLE_ADS_CLIENT,
        'google_ads_slot': GOOGLE_ADS_SLOT,
        'sponsor_name': SPONSOR_NAME,
        'sponsor_url': SPONSOR_URL,
        'affiliate_label': AFFILIATE_LABEL,
        'affiliate_url': AFFILIATE_URL,
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
    if session.get('admin_logged_in'):
        return redirect(url_for('admin_sources'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session.permanent = True
            session['admin_logged_in'] = True
            session['admin_user'] = username
            session['admin_last_seen'] = time.time()
            return redirect(url_for('admin_sources'))
        return render_template('admin_login.html', error='Invalid administrator username or password.')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    session.pop('admin_user', None)
    session.pop('admin_last_seen', None)
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
            rows = get_source_rows()
            return render_template('admin_sources.html', sources=rows, countries=COUNTRIES, topics=TOPICS,
                                   error='Please provide a valid country, topic, source name, and at least one source URL.')

        with db_connect(SOURCE_DATABASE) as db:
            execute_sql(db,
                        'INSERT INTO trusted_sources (country, topic, source_name, feed_url, site_url, source_type, is_active) VALUES (?, ?, ?, ?, ?, ?, 1) ON CONFLICT DO NOTHING',
                        (country, topic, source_name, feed_url or None, site_url or None, source_type))
            db.commit()

    rows = get_source_rows()
    return render_template('admin_sources.html', sources=rows, countries=COUNTRIES, topics=TOPICS)


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
    language = request.args.get('language', 'English')
    interests = request.args.getlist('interest') or ['Top Stories', 'Business', 'Technology']
    articles = load_articles(country, topic)
    return render_template('index.html', articles=articles, country=country,
                           country_info=COUNTRIES[country], countries=COUNTRIES,
                           topics=TOPICS, topic=topic, language=language,
                           languages=['English', 'Kiswahili', 'Kinyarwanda', 'Luganda', 'Kirundi'],
                           interests=interests, updated=datetime.now(timezone.utc).strftime('%H:%M'))



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
                           countries=COUNTRIES, topics=TOPICS)


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
    leader = rows[0] if rows and rows[0]['visits'] else None
    return render_template('analytics.html', stats=rows, total_visits=total_visits,
                           total_visitors=total_visitors,
                           daily_unique_visitors=daily_traffic['unique_visitors'] if daily_traffic else 0,
                           leader=leader,
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
    try:
        response = requests.get(url, timeout=12, headers={'User-Agent': 'UnganishwaBot/1.0'})
        response.raise_for_status()
        return response.text
    except requests.RequestException:
        return None


def extract_html_articles(site_url, source_name, country, feed_topic):
    html = fetch_html(site_url)
    if not html:
        return []
    candidates = []
    seen = set()

    selector = Selector(text=html)
    links = selector.css('article a, h2 a, h3 a, .story a, .headline a, .item a, .news-item a').getall()

    if not links:
        soup = BeautifulSoup(html, 'html.parser')
        for link in soup.select('article a, h2 a, h3 a, .story a, .headline a, .item a, .news-item a'):
            href = link.get('href') or ''
            text = ' '.join(link.stripped_strings)
            if not href or len(text) < 20 or len(text) > 180:
                continue
            full_url = urljoin(site_url, href)
            if full_url in seen:
                continue
            seen.add(full_url)
            parent = link.find_parent(['article', 'li', 'div', 'section'])
            summary = parent.get_text(' ', strip=True) if parent else text
            summary = ' '.join(summary.split())
            if not summary or summary == text:
                summary = 'Read the latest local story from this source.'
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

    for node in selector.css('article a, h2 a, h3 a, .story a, .headline a, .item a, .news-item a'):
        href = node.attrib.get('href', '')
        text = ' '.join(node.css('::text').getall()).strip()
        if not href or len(text) < 20 or len(text) > 180:
            continue
        full_url = urljoin(site_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        parent = node.xpath('ancestor::article | ancestor::li | ancestor::div | ancestor::section')
        summary = ' '.join(parent.xpath('.//text()').getall()).strip() if parent else text
        summary = ' '.join(summary.split())
        if not summary or summary == text:
            summary = 'Read the latest local story from this source.'
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


def extract_feed_articles(feed_url, source_name, country, feed_topic):
    parsed_feed = feedparser.parse(feed_url)
    entries = []
    for entry in parsed_feed.entries[:8]:
        entries.append(normalize_article({
            'title': entry.get('title', 'Untitled story'),
            'summary': entry.get('summary', 'Read the latest report from this source.'),
            'source': source_name,
            'country': country,
            'topic': feed_topic,
            'link': entry.get('link', '#'),
            'published': entry.get('published', 'Recently'),
        }))
    return deduplicate_articles(entries)


def get_source_rows(country=None, topic=None):
    db = db_connect(SOURCE_DATABASE)
    query = 'SELECT * FROM trusted_sources WHERE is_active = 1'
    params = []
    if country:
        query += ' AND country = ?'
        params.append(country)
    if topic and topic != 'Top Stories':
        query += ' AND topic = ?'
        params.append(topic)
    rows = execute_sql(db, query, params).fetchall()
    db.close()
    return rows


def load_articles(country, topic):
    articles = []
    source_rows = get_source_rows(country, topic)
    if source_rows:
        for row in source_rows:
            source_name = row['source_name']
            source_topic = row['topic']
            if topic != 'Top Stories' and source_topic != topic:
                continue
            feed_url = row['feed_url']
            site_url = row['site_url']
            if feed_url:
                feed_articles = extract_feed_articles(feed_url, source_name, country, source_topic)
                if feed_articles:
                    articles.extend(feed_articles)
                    continue
            if site_url:
                articles.extend(extract_html_articles(site_url, source_name, country, source_topic))

    if not articles:
        feeds = RSS_FEEDS.get(country, {})
        selected = feeds if topic == 'Top Stories' else {topic: feeds.get(topic, {})}
        for feed_topic, sources in selected.items():
            for source, source_config in sources.items():
                config = source_config if isinstance(source_config, dict) else {'rss': source_config, 'site': source_config}
                feed_url = config.get('rss') or config.get('feed') or config.get('url')
                site_url = config.get('site') or config.get('url') or config.get('rss')
                if feed_url:
                    feed_articles = extract_feed_articles(feed_url, source, country, feed_topic)
                    if feed_articles:
                        articles.extend(feed_articles)
                        continue
                if site_url:
                    articles.extend(extract_html_articles(site_url, source, country, feed_topic))

    fallback = [article for article in DEMO_ARTICLES if article['country'] == country and
                (topic == 'Top Stories' or article['topic'] == topic)]
    return deduplicate_articles(articles + fallback)


register_api(app, COUNTRIES, TOPICS, load_articles, deduplicate_articles,
             API_CORS_ORIGIN, get_db, execute_sql, DATABASE)


if __name__ == '__main__':
    init_analytics()
    app.run(debug=True)
