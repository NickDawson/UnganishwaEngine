"""WebSub subscriber: discovery, verified leases, signed delivery and renewal."""
import hashlib
import hmac
import secrets
import re
import time
from urllib.parse import urljoin

import click
import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for

from ingestion import MAX_FEED_BYTES, download, feed_articles, public_url


class WebSub:
    def __init__(self, app, newsroom, public_site_url):
        self.app, self.newsroom = app, newsroom
        self.public_site_url = public_site_url.rstrip('/')
        with newsroom.db() as db:
            newsroom.sql(db, '''CREATE TABLE IF NOT EXISTS websub_subscriptions (
                id TEXT PRIMARY KEY, source_id INTEGER NOT NULL UNIQUE,
                source_feed TEXT NOT NULL, topic_url TEXT NOT NULL, hub_url TEXT NOT NULL,
                secret TEXT NOT NULL, status TEXT NOT NULL, expires_at BIGINT NOT NULL DEFAULT 0,
                pending_mode TEXT, requested_at BIGINT NOT NULL DEFAULT 0,
                last_delivery BIGINT, last_error TEXT NOT NULL DEFAULT '')''')
        bp = Blueprint('websub', __name__)
        bp.add_url_rule('/admin/websub', view_func=self.dashboard)
        bp.add_url_rule('/admin/websub/<int:source_id>/<mode>', view_func=self.manage, methods=['POST'])
        bp.add_url_rule('/websub/callback/<subscription_id>', view_func=self.callback, methods=['GET', 'POST'])
        app.register_blueprint(bp)
        app.cli.command('renew-websub')(self.renew_command)

    def discover(self, feed_url):
        payload, headers, final_url = download(feed_url)
        hub = headers.get('hub', {}).get('url')
        topic = headers.get('self', {}).get('url')
        if not (hub and topic):
            parsed = feedparser.parse(payload)
            links = parsed.feed.get('links', [])
            hub = hub or next((link.get('href') for link in links if link.get('rel') == 'hub'), None)
            topic = topic or next((link.get('href') for link in links if link.get('rel') == 'self'), None)
        if not (hub and topic):
            soup = BeautifulSoup(payload, 'html.parser')
            for link in soup.select('link[rel][href]'):
                if 'hub' in link.get('rel', []):
                    hub = hub or link['href']
                if 'self' in link.get('rel', []):
                    topic = topic or link['href']
        if not hub or not topic:
            raise ValueError('Publisher does not advertise WebSub. Use feed collection instead.')
        return public_url(urljoin(final_url, hub)), public_url(urljoin(final_url, topic))

    def subscription(self, source_id):
        with self.newsroom.db() as db:
            row = self.newsroom.sql(db, 'SELECT * FROM websub_subscriptions WHERE source_id = ?', (source_id,)).fetchone()
        return dict(row) if row else None

    def send(self, source, mode='subscribe'):
        existing = self.subscription(source['id'])
        if mode == 'subscribe':
            if not source['is_active'] or not source['feed_url']:
                raise ValueError('An active RSS/Atom source is required.')
            if not self.public_site_url.startswith('https://'):
                raise ValueError('Set PUBLIC_SITE_URL to the public HTTPS address first.')
            if existing and existing['source_feed'] == source['feed_url']:
                hub, topic = existing['hub_url'], existing['topic_url']
            else:
                if existing and existing['status'] == 'active':
                    raise ValueError('Unsubscribe the previous feed before connecting a new URL.')
                hub, topic = self.discover(source['feed_url'])
            identity = existing['id'] if existing else secrets.token_urlsafe(32)
            secret = existing['secret'] if existing else secrets.token_hex(32)
            with self.newsroom.db() as db:
                self.newsroom.sql(db, '''INSERT INTO websub_subscriptions
                    (id, source_id, source_feed, topic_url, hub_url, secret, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending') ON CONFLICT (source_id) DO UPDATE SET
                    source_feed = excluded.source_feed, topic_url = excluded.topic_url,
                    hub_url = excluded.hub_url''',
                    (identity, source['id'], source['feed_url'], topic, hub, secret))
        elif mode != 'unsubscribe' or not existing:
            raise ValueError('No subscription to remove.')
        subscription = self.subscription(source['id'])
        public_url(subscription['hub_url'])
        with self.newsroom.db() as db:
            self.newsroom.sql(db, '''UPDATE websub_subscriptions SET pending_mode = ?,
                requested_at = ?, last_error = '' WHERE source_id = ?''',
                (mode, int(time.time()), source['id']))
        try:
            response = requests.post(subscription['hub_url'], data={
                'hub.mode': mode, 'hub.topic': subscription['topic_url'],
                'hub.callback': self.public_site_url + '/websub/callback/' + subscription['id'],
                'hub.secret': subscription['secret'], 'hub.lease_seconds': '86400',
            }, timeout=(5, 15), allow_redirects=False)
            if response.status_code != 202:
                raise ValueError('Hub did not accept the subscription request.')
        except (requests.RequestException, ValueError):
            with self.newsroom.db() as db:
                self.newsroom.sql(db, '''UPDATE websub_subscriptions SET last_error = ?
                    WHERE source_id = ? AND pending_mode IS NOT NULL''',
                    ('Hub request failed; retry the subscription.', source['id']))
            raise ValueError('Hub request failed; retry the subscription.') from None

    def callback(self, subscription_id):
        with self.newsroom.db() as db:
            row = self.newsroom.sql(db, 'SELECT * FROM websub_subscriptions WHERE id = ?', (subscription_id,)).fetchone()
            source = self.newsroom.sql(db, 'SELECT * FROM trusted_sources WHERE id = ?', (row['source_id'],)).fetchone() if row else None
        if not row or not source:
            abort(404)
        subscription, source = dict(row), dict(source)
        current_time = int(time.time())
        if request.method == 'GET':
            mode = request.args.get('hub.mode')
            if not subscription['pending_mode'] or current_time - subscription['requested_at'] > 600:
                abort(404)
            if request.args.get('hub.topic') != subscription['topic_url']:
                abort(404)
            if mode == 'denied':
                with self.newsroom.db() as db:
                    self.newsroom.sql(db, '''UPDATE websub_subscriptions SET pending_mode = NULL,
                        last_error = 'Hub denied the request' WHERE id = ?''', (subscription_id,))
                return '', 204
            challenge = request.args.get('hub.challenge', '')
            if mode != subscription['pending_mode'] or not challenge or len(challenge) > 4096:
                abort(404)
            if mode == 'subscribe':
                if not source['is_active'] or source['feed_url'] != subscription['source_feed']:
                    abort(404)
                try:
                    lease = int(request.args.get('hub.lease_seconds', ''))
                    if lease <= 0:
                        raise ValueError()
                except ValueError:
                    abort(400)
                expires = current_time + min(lease, 31536000)
                state = 'active'
            else:
                expires, state = 0, 'unsubscribed'
            with self.newsroom.db() as db:
                self.newsroom.sql(db, '''UPDATE websub_subscriptions SET status = ?, expires_at = ?,
                    pending_mode = NULL, last_error = '' WHERE id = ?''', (state, expires, subscription_id))
            return Response(challenge, content_type='text/plain; charset=utf-8',
                            headers={'X-Content-Type-Options': 'nosniff'})
        if (subscription['status'] != 'active' or subscription['expires_at'] <= current_time
                or not source['is_active'] or source['feed_url'] != subscription['source_feed']):
            abort(410)
        if request.content_length and request.content_length > MAX_FEED_BYTES:
            abort(413)
        payload = request.stream.read(MAX_FEED_BYTES + 1)
        if len(payload) > MAX_FEED_BYTES:
            abort(413)
        algorithm, separator, signature = request.headers.get('X-Hub-Signature', '').partition('=')
        if not separator or algorithm not in ('sha1', 'sha256', 'sha384', 'sha512'):
            abort(403)
        expected = hmac.new(subscription['secret'].encode(), payload, getattr(hashlib, algorithm)).hexdigest()
        if not re.fullmatch(r'[0-9a-fA-F]+', signature) or not hmac.compare_digest(expected, signature.lower()):
            abort(403)
        try:
            articles = feed_articles(payload, source)
        except ValueError:
            abort(400, 'An RSS or Atom payload is required.')
        self.newsroom.ingest(source, articles)
        with self.newsroom.db() as db:
            self.newsroom.sql(db, 'UPDATE websub_subscriptions SET last_delivery = ? WHERE id = ?',
                              (current_time, subscription_id))
        return '', 204

    def dashboard(self):
        with self.newsroom.db() as db:
            sources = self.newsroom.sql(db, '''SELECT s.id, s.source_name, s.country, s.is_active,
                w.status, w.expires_at, w.pending_mode, w.last_error, w.last_delivery
                FROM trusted_sources s LEFT JOIN websub_subscriptions w ON w.source_id = s.id
                WHERE s.feed_url IS NOT NULL AND s.feed_url != '' ORDER BY s.country, s.source_name''').fetchall()
        return render_template('newsroom_websub.html', sources=sources, current_time=int(time.time()))

    def manage(self, source_id, mode):
        if mode not in ('subscribe', 'unsubscribe'):
            abort(404)
        with self.newsroom.db() as db:
            source = self.newsroom.sql(db, 'SELECT * FROM trusted_sources WHERE id = ?', (source_id,)).fetchone()
        if not source:
            abort(404)
        try:
            self.send(dict(source), mode)
            flash('Request sent. Waiting for the publisher hub to verify it.')
        except (ValueError, OSError, requests.RequestException) as exc:
            flash(str(exc) if isinstance(exc, ValueError) else 'Could not connect to the publisher hub.')
        return redirect(url_for('websub.dashboard'))

    def renew_command(self):
        """Renew subscriptions approaching expiry; run hourly with your server scheduler."""
        with self.newsroom.db() as db:
            sources = self.newsroom.sql(db, '''SELECT s.* FROM trusted_sources s
                JOIN websub_subscriptions w ON w.source_id = s.id WHERE s.is_active = 1
                AND w.status != 'unsubscribed' AND w.expires_at < ?
                AND (w.pending_mode IS NULL OR (w.pending_mode = 'subscribe' AND w.requested_at < ?))''',
                (int(time.time()) + 3600, int(time.time()) - 600)).fetchall()
        failures = 0
        for source in sources:
            try:
                self.send(dict(source))
                click.echo(f'Renewal requested: {source["source_name"]}')
            except (ValueError, OSError, requests.RequestException):
                failures += 1
                click.echo(f'Renewal failed: {source["source_name"]}', err=True)
        if failures:
            raise click.ClickException(f'{failures} subscription renewals failed.')
