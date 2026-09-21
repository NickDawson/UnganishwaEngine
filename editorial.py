"""Persistent newsroom inbox, scoped staff accounts, and human review."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlsplit, urlunsplit

import click
from bs4 import BeautifulSoup
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

ROLES = {'admin': 'Administrator', 'editor': 'Editor (categorize and moderate)',
         'categorizer': 'Categorizer (assign categories)'}
STATES = ('uncategorized', 'published', 'rejected')


def now():
    return datetime.now(timezone.utc).isoformat()


def article_url(value):
    try:
        parts = urlsplit(value.strip())
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            return None
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))
    except (ValueError, AttributeError):
        return None


def plain_text(value, limit):
    return BeautifulSoup(str(value or ''), 'html.parser').get_text(' ', strip=True)[:limit]


class Newsroom:
    def __init__(self, app, connect, execute, database, countries, topics,
                 owner_name, owner_hash, postgres=False):
        self.app, self.connect, self.sql, self.database = app, connect, execute, database
        self.countries = countries
        self.categories = [topic for topic in topics if topic not in ('Top Stories', 'Uncategorized')]
        self.timezone = ZoneInfo(os.environ.get('NEWSROOM_TIMEZONE', 'Africa/Nairobi'))
        self.owner_name, self.owner_hash = owner_name, owner_hash
        self.postgres = postgres
        self.collect_source = None
        self.init_schema()
        bp = Blueprint('newsroom', __name__)
        bp.add_url_rule('/admin/news', view_func=self.inbox)
        bp.add_url_rule('/admin/news/<article_id>', view_func=self.story, methods=['GET', 'POST'])
        bp.add_url_rule('/admin/news/add', view_func=self.add_story, methods=['GET', 'POST'])
        bp.add_url_rule('/admin/users', view_func=self.users, methods=['GET', 'POST'])
        bp.add_url_rule('/admin/users/<user_id>', view_func=self.update_user, methods=['POST'])
        bp.add_url_rule('/admin/collect/<int:source_id>', view_func=self.collect, methods=['POST'])
        app.register_blueprint(bp)
        app.context_processor(lambda: {'csrf_token': self.csrf_token,
                                      'newsroom_user': self.current_user()})
        app.cli.command('collect-news')(self.collect_command)

    @contextmanager
    def db(self):
        connection = self.connect(self.database)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_schema(self):
        with self.db() as db:
            self.sql(db, '''CREATE TABLE IF NOT EXISTS newsroom_users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, role TEXT NOT NULL,
                countries TEXT NOT NULL, categories TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                auth_version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)''')
            self.sql(db, '''CREATE TABLE IF NOT EXISTS newsroom_articles (
                id TEXT PRIMARY KEY, source_id INTEGER, source TEXT NOT NULL,
                country TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
                link TEXT NOT NULL, published TEXT NOT NULL, topic TEXT,
                status TEXT NOT NULL DEFAULT 'uncategorized',
                version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, categorized_by TEXT, reviewed_by TEXT,
                published_at TEXT, UNIQUE(country, link))''')
            self.sql(db, '''CREATE INDEX IF NOT EXISTS newsroom_article_queue
                ON newsroom_articles(status, country, created_at)''')
            self.sql(db, '''CREATE TABLE IF NOT EXISTS newsroom_audit (
                id TEXT PRIMARY KEY, article_id TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, old_status TEXT, new_status TEXT NOT NULL,
                old_topic TEXT, new_topic TEXT, note TEXT NOT NULL, created_at TEXT NOT NULL)''')

    def current_user(self):
        if hasattr(g, 'newsroom_user'):
            return g.newsroom_user
        user = None
        if session.get('admin_logged_in'):
            if session.get('newsroom_owner') and session.get('admin_user') == self.owner_name:
                user = {'id': 'owner', 'username': self.owner_name, 'role': 'admin',
                        'countries': list(self.countries), 'categories': self.categories}
            elif session.get('newsroom_user_id'):
                with self.db() as db:
                    row = self.sql(db, 'SELECT * FROM newsroom_users WHERE id = ? AND is_active = 1',
                                   (session['newsroom_user_id'],)).fetchone()
                if row and row['auth_version'] == session.get('auth_version'):
                    user = dict(row)
                    user['countries'] = json.loads(user['countries'])
                    user['categories'] = json.loads(user['categories'])
        g.newsroom_user = user
        return user

    def authenticate(self, username, password):
        owner = username == self.owner_name and check_password_hash(self.owner_hash, password)
        row = None
        if not owner:
            with self.db() as db:
                row = self.sql(db, 'SELECT * FROM newsroom_users WHERE username = ? AND is_active = 1',
                               (username,)).fetchone()
            if not row or not check_password_hash(row['password_hash'], password):
                return False
        session.clear()
        session.permanent = True
        session.update(admin_logged_in=True, admin_user=username, admin_last_seen=time.time())
        if owner:
            session['newsroom_owner'] = True
        else:
            session.update(newsroom_user_id=row['id'], auth_version=row['auth_version'])
        g.pop('newsroom_user', None)
        self.csrf_token()
        return True

    def csrf_token(self):
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_urlsafe(32)
        return session['csrf_token']

    def authorize(self):
        if not (request.path.startswith('/admin') or request.endpoint == 'analytics'):
            return
        if request.method == 'POST':
            supplied = request.form.get('csrf_token', '')
            if not supplied or not hmac.compare_digest(supplied.encode(), session.get('csrf_token', '').encode()):
                abort(400, 'The form expired. Reload the page and try again.')
        if request.endpoint == 'admin_login':
            return
        user = self.current_user()
        if not user or time.time() - session.get('admin_last_seen', 0) > 1800:
            session.clear()
            g.newsroom_user = None
            return redirect(url_for('admin_login'))
        session['admin_last_seen'] = time.time()
        admin_only = request.endpoint not in {
            'newsroom.inbox', 'newsroom.story', 'newsroom.add_story', 'admin_logout'}
        if admin_only and user['role'] != 'admin':
            abort(403)

    def can_access(self, user, article):
        return user['role'] == 'admin' or (
            article['country'] in user['countries'] and
            (not article['topic'] or article['topic'] in user['categories']))

    def ingest(self, source, articles, actor=None):
        """Never overwrite decisions when polling or a hub redelivers an article."""
        inserted = 0
        with self.db() as db:
            if source.get('id') is not None:
                current = self.sql(db, 'SELECT country, is_active FROM trusted_sources WHERE id = ?',
                                   (source['id'],)).fetchone()
                if not current or not current['is_active'] or current['country'] != source['country']:
                    return 0
            for article in articles:
                link = article_url(article.get('link', ''))
                title = plain_text(article.get('title'), 500)
                if not link or not title or source['country'] not in self.countries:
                    continue
                identity = hashlib.sha256((source['country'] + '\n' + link).encode()).hexdigest()
                cursor = self.sql(db, '''INSERT INTO newsroom_articles
                    (id, source_id, source, country, title, summary, link, published, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING''',
                    (identity, source.get('id'), source['source_name'], source['country'], title,
                     plain_text(article.get('summary'), 10000), link,
                     plain_text(article.get('published') or 'Recently', 200), now(), now()))
                if cursor.rowcount == 1:
                    inserted += 1
                    self.sql(db, """INSERT INTO newsroom_audit
                        (id, article_id, actor, action, old_status, new_status, old_topic, new_topic, note, created_at)
                        VALUES (?, ?, ?, 'received', NULL, 'uncategorized', NULL, NULL, '', ?)""",
                        (secrets.token_hex(16), identity, actor or 'Feed: ' + source['source_name'], now()))
        return inserted

    def public_articles(self, country, topic):
        query = "SELECT * FROM newsroom_articles WHERE status IN ('uncategorized', 'published') AND country = ?"
        parameters = [country]
        if topic == 'Uncategorized':
            query += " AND status = 'uncategorized'"
        elif topic != 'Top Stories':
            query += ' AND topic = ?'
            parameters.append(topic)
        with self.db() as db:
            rows = self.sql(db, query + ' ORDER BY created_at DESC, id LIMIT 200', parameters).fetchall()
        return [{**dict(row), 'topic': row['topic'] or 'Uncategorized'} for row in rows]

    def inbox(self):
        user = self.current_user()
        state = request.args.get('status', 'all')
        country, category = request.args.get('country', ''), request.args.get('category', '')
        query, params = ' FROM newsroom_articles WHERE 1 = 1', []
        day = request.args.get('day', datetime.now(self.timezone).date().isoformat())
        if day:
            try:
                start = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=self.timezone)
            except ValueError:
                abort(400, 'Choose a valid date.')
            query += ' AND created_at >= ? AND created_at < ?'
            params.extend([start.astimezone(timezone.utc).isoformat(),
                           (start + timedelta(days=1)).astimezone(timezone.utc).isoformat()])
        if state in STATES:
            query += ' AND status = ?'
            params.append(state)
        else:
            state = 'all'
        if user['role'] != 'admin':
            query += ' AND country IN (' + ','.join('?' for _ in user['countries']) + ')'
            params.extend(user['countries'])
            query += ' AND (topic IS NULL OR topic IN (' + ','.join('?' for _ in user['categories']) + '))'
            params.extend(user['categories'])
        if country in self.countries:
            query += ' AND country = ?'
            params.append(country)
        if category in self.categories:
            query += ' AND topic = ?'
            params.append(category)
        term = request.args.get('q', '').strip()[:200]
        if term:
            query += ' AND (LOWER(title) LIKE ? OR LOWER(source) LIKE ?)'
            params.extend(['%' + term.lower() + '%'] * 2)
        try:
            page = max(1, int(request.args.get('page', 1)))
        except ValueError:
            page = 1
        with self.db() as db:
            total = self.sql(db, 'SELECT COUNT(*) AS count' + query, params).fetchone()['count']
            pages = max(1, (total + 19) // 20)
            page = min(page, pages)
            rows = self.sql(db, 'SELECT *' + query + ' ORDER BY created_at DESC, id LIMIT 20 OFFSET ?',
                            [*params, (page - 1) * 20]).fetchall()
        return render_template('newsroom_inbox.html', articles=rows, states=STATES, status=state,
                               countries=self.countries, categories=self.categories, country=country,
                               category=category, q=term, page=page, pages=pages, total=total, day=day, newsroom_timezone=str(self.timezone))

    def story(self, article_id):
        user = self.current_user()
        with self.db() as db:
            row = self.sql(db, 'SELECT * FROM newsroom_articles WHERE id = ?', (article_id,)).fetchone()
            if not row:
                abort(404)
            article = dict(row)
            if not self.can_access(user, article):
                abort(403)
            if request.method == 'POST':
                action = request.form.get('action')
                topic = request.form.get('topic')
                note = request.form.get('note', '').strip()[:2000]
                try:
                    version = int(request.form.get('version', ''))
                except ValueError:
                    abort(400, 'Missing article version.')
                new_status = {'categorize': 'published', 'reject': 'rejected', 'reset': 'uncategorized'}.get(action)
                if not new_status:
                    abort(400)
                if action != 'categorize' and user['role'] not in ('admin', 'editor'):
                    abort(403)
                if action == 'categorize' and article['status'] not in ('uncategorized', 'published'):
                    abort(409, 'Reopen this article before changing its category.')
                if action == 'categorize':
                    if topic not in self.categories:
                        abort(400, 'Choose a category.')
                    if user['role'] != 'admin' and topic not in user['categories']:
                        abort(403)
                else:
                    topic = None if action == 'reset' else article['topic']
                changed = self.sql(db, '''UPDATE newsroom_articles SET topic = ?, status = ?,
                    version = version + 1, updated_at = ?, categorized_by = ?, reviewed_by = ?, published_at = ?
                    WHERE id = ? AND version = ?''',
                    (topic, new_status, now(), user['username'] if action == 'categorize' else article['categorized_by'],
                     user['username'] if action in ('publish', 'reject') else None,
                     now() if action == 'categorize' else None, article_id, version))
                if changed.rowcount != 1:
                    abort(409, 'Someone changed this article. Reload before saving.')
                self.sql(db, '''INSERT INTO newsroom_audit
                    (id, article_id, actor, action, old_status, new_status, old_topic, new_topic, note, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (secrets.token_hex(16), article_id, user['username'], action, article['status'],
                     new_status, article['topic'], topic, note, now()))
                flash('Article updated successfully.')
                return redirect(url_for('newsroom.story', article_id=article_id))
            history = self.sql(db, 'SELECT * FROM newsroom_audit WHERE article_id = ? ORDER BY created_at DESC',
                               (article_id,)).fetchall()
        return render_template('newsroom_story.html', article=article, history=history,
                               categories=self.categories if user['role'] == 'admin' else user['categories'],
                               countries=self.countries)

    def add_story(self):
        user = self.current_user()
        countries = self.countries if user['role'] == 'admin' else {k: self.countries[k] for k in user['countries']}
        if request.method == 'POST':
            country = request.form.get('country')
            title = request.form.get('title', '').strip()
            source = request.form.get('source', '').strip()
            link = article_url(request.form.get('link', ''))
            if country not in countries or not title or not source or not link:
                abort(400, 'Provide a title, publisher, valid article URL and permitted country.')
            count = self.ingest({'source_name': source[:200], 'country': country},
                               [{'title': title, 'summary': request.form.get('summary', ''), 'link': link}],
                               actor=user['username'])
            flash('Article added to Uncategorized.' if count else 'This article is already in the inbox.')
            return redirect(url_for('newsroom.inbox'))
        return render_template('newsroom_add.html', countries=countries)

    def user_fields(self):
        role = request.form.get('role')
        countries = request.form.getlist('countries')
        categories = request.form.getlist('categories')
        if role not in ROLES:
            abort(400, 'Choose a valid role.')
        if role == 'admin':
            countries, categories = list(self.countries), self.categories
        if not countries or not categories or set(countries) - self.countries.keys() or set(categories) - set(self.categories):
            abort(400, 'Select at least one valid country and category.')
        return role, json.dumps(countries), json.dumps(categories)

    def users(self):
        error = None
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            role, countries, categories = self.user_fields()
            if not re.fullmatch(r'[A-Za-z0-9_.-]{3,60}', username) or username == self.owner_name:
                error = 'Use a unique username of 3–60 letters, numbers, dots, underscores or hyphens.'
            elif len(password) < 12:
                error = 'Use a password of at least 12 characters.'
            else:
                with self.db() as db:
                    cursor = self.sql(db, '''INSERT INTO newsroom_users
                        (id, username, password_hash, role, countries, categories, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (username) DO NOTHING''',
                        (secrets.token_hex(16), username, generate_password_hash(password, method='pbkdf2:sha256'),
                         role, countries, categories, now()))
                    if not cursor.rowcount:
                        error = 'That username is already in use.'
                if not error:
                    flash('Staff account created.')
                    return redirect(url_for('newsroom.users'))
        with self.db() as db:
            users = [dict(row) for row in self.sql(db, 'SELECT id, username, role, countries, categories, is_active FROM newsroom_users ORDER BY username').fetchall()]
        for user in users:
            user['countries'], user['categories'] = json.loads(user['countries']), json.loads(user['categories'])
        return render_template('newsroom_users.html', users=users, roles=ROLES, countries=self.countries,
                               categories=self.categories, error=error), 400 if error else 200

    def update_user(self, user_id):
        if user_id == self.current_user()['id']:
            abort(400, 'Ask another administrator to change your own access.')
        role, countries, categories = self.user_fields()
        password = request.form.get('password', '')
        if password and len(password) < 12:
            abort(400, 'Use a password of at least 12 characters.')
        with self.db() as db:
            cursor = self.sql(db, '''UPDATE newsroom_users SET role = ?, countries = ?, categories = ?,
                is_active = ?, auth_version = auth_version + 1 WHERE id = ?''',
                (role, countries, categories, int(request.form.get('is_active') == 'on'), user_id))
            if not cursor.rowcount:
                abort(404)
            if password:
                self.sql(db, 'UPDATE newsroom_users SET password_hash = ? WHERE id = ?',
                         (generate_password_hash(password, method='pbkdf2:sha256'), user_id))
        flash('Staff permissions updated. Previous sessions have been revoked.')
        return redirect(url_for('newsroom.users'))

    def collect(self, source_id):
        with self.db() as db:
            source = self.sql(db, 'SELECT * FROM trusted_sources WHERE id = ? AND is_active = 1', (source_id,)).fetchone()
        if not source:
            abort(404)
        try:
            count = self.collect_source(dict(source))
            flash(f'{count} new articles added to Uncategorized.')
        except Exception:
            self.app.logger.warning('News collection failed for source %s', source_id)
            flash('Could not collect this source. Check its feed URL and try again.')
        return redirect(url_for('newsroom.inbox'))

    def collect_command(self):
        """Collect active sources as publicly visible Uncategorized news."""
        with self.db() as db:
            sources = self.sql(db, 'SELECT * FROM trusted_sources WHERE is_active = 1 ORDER BY id').fetchall()
        failures = 0
        for source in sources:
            try:
                count = self.collect_source(dict(source))
                click.echo(f'{source["source_name"]}: {count} new articles')
            except Exception:
                failures += 1
                click.echo(f'{source["source_name"]}: collection failed', err=True)
        if failures:
            raise click.ClickException(f'{failures} sources failed; other sources were processed.')
