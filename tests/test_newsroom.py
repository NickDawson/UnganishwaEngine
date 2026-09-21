import hashlib
import hmac
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, Mock

from bs4 import BeautifulSoup
from visitor_geo import visitor_country


class NewsroomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {'UNGANISHWA_DATA_DIR': cls.temp.name, 'DATABASE_URL': '',
                                         'FLASK_SECRET_KEY': 'test-only', 'UNGANISHWA_ADMIN_KEY': 'test-owner-password'})
        cls.env.start()
        import app
        cls.module = app
        cls.app = app.app
        cls.app.config['TESTING'] = True
        cls.room = app.newsroom

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.client = self.app.test_client()
        with self.room.db() as db:
            for table in ('newsroom_audit', 'newsroom_articles', 'newsroom_users', 'websub_subscriptions', 'trusted_sources'):
                self.room.sql(db, f'DELETE FROM {table}')
            self.room.sql(db, '''INSERT INTO trusted_sources (id, country, topic, source_name, feed_url, is_active)
                VALUES (1, 'tanzania', 'Business', 'Test Publisher', 'https://example.com/feed', 1)''')
        self.source = {'id': 1, 'country': 'tanzania', 'source_name': 'Test Publisher',
                       'feed_url': 'https://example.com/feed', 'is_active': 1}
        self.article = {'title': 'A new local business story', 'summary': '<p>Local reporting</p>',
                        'link': 'https://example.com/news/1'}

    def token(self, client=None):
        client = client or self.client
        with client.session_transaction() as session:
            return session['csrf_token']

    def login(self, username='admin', password='test-owner-password', client=None):
        client = client or self.client
        page = client.get('/admin/login')
        self.assertEqual(page.status_code, 200)
        self.assertIn('csrf_token', page.text)
        response = client.post('/admin/login', data={'username': username, 'password': password,
                                                    'csrf_token': self.token(client)})
        self.assertEqual(response.status_code, 302)
        client.get('/admin/news')

    def seed(self):
        self.room.ingest(self.source, [self.article])
        with self.room.db() as db:
            return dict(self.room.sql(db, 'SELECT * FROM newsroom_articles').fetchone())

    def test_public_uncategorized_then_category_and_replay(self):
        story = self.seed()
        self.assertEqual(self.room.public_articles('tanzania', 'Uncategorized')[0]['topic'], 'Uncategorized')
        self.assertFalse(self.room.public_articles('tanzania', 'Business'))
        self.login()
        response = self.client.post('/admin/news/' + story['id'], data={
            'csrf_token': self.token(), 'version': 1, 'action': 'categorize', 'topic': 'Business'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.room.public_articles('tanzania', 'Uncategorized'))
        self.assertEqual(self.room.public_articles('tanzania', 'Business')[0]['title'], self.article['title'])
        self.assertEqual(self.room.ingest(self.source, [self.article]), 0)
        self.assertEqual(self.room.public_articles('tanzania', 'Business')[0]['version'], 2)
        response = self.client.post('/admin/news/' + story['id'], data={
            'csrf_token': self.token(), 'version': 1, 'action': 'categorize', 'topic': 'Sports'})
        self.assertEqual(response.status_code, 409)
        self.assertIn('admin', self.client.get('/admin/news/' + story['id']).text)
        self.assertEqual(self.client.get('/api/v1/articles?country=tanzania&topic=Business').json['data'][0]['topic'], 'Business')

    def test_staff_scope_and_revocation(self):
        story = self.seed()
        self.login()
        response = self.client.post('/admin/users', data={
            'csrf_token': self.token(), 'username': 'reporter', 'password': 'long-enough-password',
            'role': 'categorizer', 'countries': ['tanzania'], 'categories': ['Business']})
        self.assertEqual(response.status_code, 302)
        worker = self.app.test_client()
        self.login('reporter', 'long-enough-password', worker)
        self.assertEqual(worker.get('/admin/users').status_code, 403)
        self.assertEqual(worker.get('/admin/sources').status_code, 403)
        self.assertEqual(worker.get('/analytics').status_code, 403)
        for action, topic in [('reject', 'Business'), ('categorize', 'Sports')]:
            response = worker.post('/admin/news/' + story['id'], data={
                'csrf_token': self.token(worker), 'version': 1, 'action': action, 'topic': topic})
            self.assertEqual(response.status_code, 403)
        response = worker.post('/admin/news/' + story['id'], data={
            'csrf_token': self.token(worker), 'version': 1, 'action': 'categorize', 'topic': 'Business'})
        self.assertEqual(response.status_code, 302)
        with self.room.db() as db:
            user = self.room.sql(db, 'SELECT * FROM newsroom_users').fetchone()
        response = self.client.post('/admin/users/' + user['id'], data={
            'csrf_token': self.token(), 'role': 'categorizer', 'countries': ['tanzania'], 'categories': ['Business']})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(worker.get('/admin/news').status_code, 302)

    def test_today_filter_and_older_news(self):
        story = self.seed()
        self.login()
        self.assertIn(self.article['title'], self.client.get('/admin/news').text)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        with self.room.db() as db:
            self.room.sql(db, 'UPDATE newsroom_articles SET created_at = ? WHERE id = ?', (yesterday, story['id']))
        self.assertNotIn(self.article['title'], self.client.get('/admin/news').text)
        self.assertIn(self.article['title'], self.client.get('/admin/news?day=').text)
        self.assertEqual(self.client.get('/admin/news?day=not-a-date').status_code, 400)

    def test_all_admin_pages_and_csrf(self):
        self.login()
        for path in ['/admin/news', '/admin/news/add', '/admin/users', '/admin/sources', '/admin/sources/edit/1', '/admin/websub', '/analytics']:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            soup = BeautifulSoup(response.text, 'html.parser')
            for form in soup.select('form[method=post]'):
                if form.get('action') == '/subscribe':
                    continue
                self.assertIsNotNone(form.select_one('input[name=csrf_token]'), path)
        self.assertEqual(self.client.post('/admin/sources/toggle/1').status_code, 400)
        self.assertEqual(self.client.post('/admin/news/add', data={
            'csrf_token': self.token(), 'country': 'tanzania', 'title': 'Manual news', 'source': 'Publisher',
            'link': 'javascript:alert(1)'}).status_code, 400)

    def test_signed_websub_delivery_and_paused_source(self):
        with self.room.db() as db:
            self.room.sql(db, '''INSERT INTO websub_subscriptions
                (id, source_id, source_feed, topic_url, hub_url, secret, status, pending_mode, requested_at)
                VALUES ('callback-test', 1, 'https://example.com/feed', 'https://example.com/feed',
                'https://hub.example.com', 'test-secret', 'pending', 'subscribe', ?)''', (int(time.time()),))
        endpoint = '/websub/callback/callback-test'
        params = {'hub.mode': 'subscribe', 'hub.topic': 'https://example.com/feed',
                  'hub.challenge': '<script>test</script>', 'hub.lease_seconds': '3600'}
        bad = {**params, 'hub.topic': 'https://attacker.example/feed'}
        self.assertEqual(self.client.get(endpoint, query_string=bad).status_code, 404)
        response = self.client.get(endpoint, query_string=params)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith('text/plain'))
        payload = b'<rss version="2.0"><channel><title>News</title><item><title>Incoming</title><link>https://example.com/1</link><description>Story</description></item></channel></rss>'
        self.assertEqual(self.client.post(endpoint, data=payload).status_code, 403)
        signature = hmac.new(b'test-secret', payload, hashlib.sha256).hexdigest()
        for _ in range(2):
            self.assertEqual(self.client.post(endpoint, data=payload, headers={'X-Hub-Signature': 'sha256=' + signature}).status_code, 204)
        self.assertEqual(len(self.room.public_articles('tanzania', 'Uncategorized')), 1)
        with self.room.db() as db:
            self.room.sql(db, 'UPDATE trusted_sources SET is_active = 0 WHERE id = 1')
        self.assertEqual(self.client.post(endpoint, data=payload, headers={'X-Hub-Signature': 'sha256=' + signature}).status_code, 410)

    def test_websub_discovery_and_request(self):
        websub = self.module.websub
        with patch('websub.download', return_value=(b'', {'hub': {'url': 'https://hub.example.com'}, 'self': {'url': self.source['feed_url']}}, self.source['feed_url'])), patch('websub.public_url', side_effect=lambda url: url), patch('websub.requests.post', return_value=Mock(status_code=202)) as post:
            websub.send(self.source)
        args = post.call_args.kwargs['data']
        self.assertEqual(args['hub.mode'], 'subscribe')
        self.assertIn('/websub/callback/', args['hub.callback'])
        self.assertTrue(args['hub.secret'])
        subscription = websub.subscription(1)
        self.assertEqual(subscription['pending_mode'], 'subscribe')
        self.assertEqual(subscription['status'], 'pending')

    def test_rejected_news_disappears_and_can_be_reopened(self):
        story = self.seed()
        self.login()
        path = '/admin/news/' + story['id']
        response = self.client.post(path, data={'csrf_token': self.token(), 'version': 1, 'action': 'reject'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.room.public_articles('tanzania', 'Top Stories'))
        self.assertFalse(self.client.get('/api/v1/search?q=business').json['data'])
        response = self.client.post(path, data={'csrf_token': self.token(), 'version': 2, 'action': 'reset'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.room.public_articles('tanzania', 'Uncategorized')), 1)

    def test_east_africa_midnight_boundary(self):
        story = self.seed()
        self.login()
        with self.room.db() as db:
            self.room.sql(db, 'UPDATE newsroom_articles SET created_at = ? WHERE id = ?',
                          ('2026-09-16T21:01:00+00:00', story['id']))
        self.assertIn(self.article['title'], self.client.get('/admin/news?day=2026-09-17').text)
        self.assertNotIn(self.article['title'], self.client.get('/admin/news?day=2026-09-16').text)

    def test_paused_source_cannot_ingest(self):
        with self.room.db() as db:
            self.room.sql(db, 'UPDATE trusted_sources SET is_active = 0 WHERE id = 1')
        self.assertEqual(self.room.ingest(self.source, [self.article]), 0)
        self.assertFalse(self.room.public_articles('tanzania', 'Top Stories'))

    def test_visitor_country_ignores_untrusted_headers(self):
        from flask import request
        with patch.dict(os.environ, {'GEO_COUNTRY_HEADER': 'CF-IPCountry', 'GEO_TRUSTED_PROXY_CIDRS': '10.0.0.0/8', 'GEOIP_DATABASE_PATH': ''}):
            with self.app.test_request_context('/', headers={'CF-IPCountry': 'KE'}, environ_base={'REMOTE_ADDR': '8.8.8.8'}):
                self.assertEqual(visitor_country(request), 'Unknown')
            with self.app.test_request_context('/', headers={'CF-IPCountry': 'KE'}, environ_base={'REMOTE_ADDR': '10.0.0.1'}):
                self.assertEqual(visitor_country(request), 'KE')
            with self.app.test_request_context('/', headers={'CF-IPCountry': 'XX'}, environ_base={'REMOTE_ADDR': '10.0.0.1'}):
                self.assertEqual(visitor_country(request), 'Unknown')

    def test_visitor_origin_is_separate_from_news_country(self):
        with patch('app.visitor_country', return_value='KE'):
            self.client.get('/news/tanzania/top-stories')
        db = self.module.db_connect(self.module.DATABASE)
        try:
            row = self.module.execute_sql(db, "SELECT page_views FROM visitor_country_daily WHERE country_code = 'KE'").fetchone()
            self.assertGreaterEqual(row['page_views'], 1)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
