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
                                         'FLASK_SECRET_KEY': 'test-only', 'NEWS_AUTO_CATEGORIZE': 'local', 'UNGANISHWA_ADMIN_KEY': 'test-owner-password'})
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

    def test_html_summary_excludes_css_and_neighboring_stories(self):
        html = """<section><style>.elementor-15437 {display:flex}</style>
        <article><h2><a href='/one'>First important local news story</a></h2>
        <div class='elementor-post__excerpt'><style>.bad{color:red}</style>
        <p>Correct first summary.</p><script>alert('bad')</script></div></article>
        <article><h2><a href='/two'>Second important local news story</a></h2>
        <p>Different second summary.</p></article></section>"""
        with patch.object(self.module, 'fetch_html', return_value=html):
            articles = self.module.extract_html_articles('https://example.com', 'Test', 'tanzania', 'Uncategorized')
        self.assertEqual(articles[0]['summary'], 'Correct first summary.')
        self.assertEqual(articles[1]['summary'], 'Different second summary.')
        self.room.ingest(self.source, articles)
        response = self.client.get('/api/v1/articles?country=tanzania').json
        self.assertEqual({a['summary'] for a in response['data']},
                         {'Correct first summary.', 'Different second summary.'})

    def test_html_without_excerpt_uses_neutral_fallback(self):
        html = """<div><style>.bad{display:flex}</style><h2>
        <a href='/one'>An important local news headline</a></h2></div>"""
        with patch.object(self.module, 'fetch_html', return_value=html):
            articles = self.module.extract_html_articles('https://example.com', 'Test', 'tanzania', 'Uncategorized')
        self.assertEqual(articles[0]['summary'], 'Read the full report from the original source.')

    def test_ingest_removes_non_content_elements(self):
        self.article['summary'] = '<style>.bad{display:flex}</style><p>Real summary</p><script>bad()</script><template>hidden</template>'
        self.assertEqual(self.seed()['summary'], 'Real summary')

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

    def test_archive_categories_and_search_beyond_200_stories(self):
        story = self.seed()
        with self.room.db() as db:
            self.room.sql(db, """UPDATE newsroom_articles SET status = 'published',
                topic = 'Business', created_at = ? WHERE id = ?""",
                ('2020-01-01T00:00:00+00:00', story['id']))
        self.room.ingest(self.source, [
            {'title': 'Recent report ' + str(i), 'link': 'https://example.com/recent/' + str(i)}
            for i in range(205)])
        self.assertEqual(len(self.room.public_articles('tanzania', 'Top Stories')), 206)
        self.assertEqual(self.room.public_articles('tanzania', 'Business')[0]['id'], story['id'])
        self.assertIn(self.article['title'], self.client.get('/news/tanzania/business').text)
        self.assertIn(self.article['title'], self.client.get('/search?q=business').text)
        result = self.client.get('/api/v1/search?q=business').json
        self.assertEqual(result['pagination']['total'], 1)
        self.assertEqual(result['data'][0]['title'], self.article['title'])
        self.assertEqual(self.client.get('/api/v1/articles?page=11').json['pagination']['total'], 206)

    def test_empty_public_requests_do_not_collect_news(self):
        with patch.object(self.module, 'collect_news_source') as collect:
            for path in ('/', '/news/tanzania/business', '/search?q=business',
                         '/api/v1/articles', '/api/v1/search?q=business'):
                self.assertEqual(self.client.get(path).status_code, 200, path)
            collect.assert_not_called()
        self.assertIn('Awaiting news', self.client.get('/').text)

    def test_edition_timestamp_comes_from_stored_news(self):
        story = self.seed()
        with self.room.db() as db:
            self.room.sql(db, 'UPDATE newsroom_articles SET updated_at = ? WHERE id = ?',
                          ('2020-01-02T03:04:00+00:00', story['id']))
        for _ in range(2):
            self.assertIn('Updated 02 Jan 2020 · 03:04', self.client.get('/').text)

    def test_admin_feedback_receives_searches_and_paginates_app_submissions(self):
        self.assertEqual(self.client.get('/admin/feedback').status_code, 302)
        with self.app.app_context():
            db = self.module.get_db()
            self.module.execute_sql(db, 'DELETE FROM reader_feedback')
            db.commit()
        self.login()
        self.assertIn('No feedback yet', self.client.get('/admin/feedback').text)
        for index in range(21):
            response = self.client.post('/api/v1/feedback', json={
                'name': f'Reader {index}', 'country': 'Tanzania',
                'comment': '<script>alert(1)</script>' if index == 20 else 'Useful news'})
            self.assertEqual(response.status_code, 201)
        first = self.client.get('/admin/feedback')
        self.assertEqual(first.status_code, 200)
        self.assertIn('21 submissions', first.text)
        self.assertIn('Source: App', first.text)
        self.assertIn('Page 1 of 2', first.text)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', first.text)
        self.assertNotIn('<script>alert(1)</script>', first.text)
        self.assertIn('Reader 0', self.client.get('/admin/feedback?page=2').text)
        for query in ['Reader 20', 'alert(1)', 'Tanzania']:
            result = self.client.get('/admin/feedback', query_string={'q': query})
            self.assertIn('Reader 20', result.text)
        self.assertIn('No matching feedback', self.client.get('/admin/feedback?q=missing').text)
        self.assertEqual(self.client.get('/admin/feedback?page=invalid').status_code, 200)
        self.assertIn('Page 2 of 2', self.client.get('/admin/feedback?page=99999').text)

    def test_public_feedback_validates_and_reaches_admin(self):
        page = self.client.get('/feedback')
        self.assertEqual(page.status_code, 200)
        self.assertIn('Share your feedback', page.text)
        self.assertIn('href="/feedback"', self.client.get('/').text)
        invalid = self.client.post('/feedback', data={'name': 'Asha', 'country': '', 'comment': 'Hello'})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('value="Asha"', invalid.text)
        self.assertIn('aria-invalid="true"', invalid.text)
        response = self.client.post('/feedback', data={'name': 'Web Reader', 'country': 'Kenya', 'comment': 'More local news please'})
        self.assertEqual(response.status_code, 303)
        self.assertIn('Thank you for your feedback!', self.client.get(response.location).text)
        self.assertNotIn('Thank you for your feedback!', self.client.get('/feedback').text)
        self.login()
        admin = self.client.get('/admin/feedback?q=Web+Reader')
        self.assertIn('More local news please', admin.text)
        self.assertIn('Source: Web', admin.text)
        self.assertIn('Kenya', admin.text)

    def test_feedback_source_migration_preserves_old_rows(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'legacy.sqlite3')
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE reader_feedback (id TEXT PRIMARY KEY, name TEXT, comment TEXT, country TEXT, created_at TEXT)')
                db.execute("INSERT INTO reader_feedback VALUES ('old', 'Reader', 'Hello', 'Kenya', '2026-09-01')")
            with patch.object(self.module, 'DATABASE', path):
                self.module.init_analytics()
                self.module.init_analytics()
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('SELECT comment, source FROM reader_feedback').fetchone(), ('Hello', 'unknown'))

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
        self.assertEqual(worker.get('/admin/feedback').status_code, 403)
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

    def test_malformed_admin_session_returns_to_login(self):
        self.login()
        with self.client.session_transaction() as session:
            session['admin_last_seen'] = 'invalid'
        response = self.client.get('/admin/news')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/admin/login')

        self.login()
        with self.client.session_transaction() as session:
            session['csrf_token'] = 123
        self.assertEqual(self.client.post('/admin/sources').status_code, 400)

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

    def test_auto_category_audit_manual_override_and_replay(self):
        with patch.dict(os.environ, {'NEWS_AUTO_CATEGORIZE': 'local'}):
            self.article.update(title='Kocha ajiandaa kwa ligi', summary='Timu yafunga mabao matatu')
            story = self.seed()
        self.assertEqual(story['topic'], 'Sports')
        self.assertEqual(story['categorized_by'], 'Automatic')
        self.assertEqual(story['status'], 'published')
        with self.room.db() as db:
            audit = self.room.sql(db, "SELECT * FROM newsroom_audit WHERE action = 'auto_categorize'").fetchone()
        self.assertEqual(audit['new_topic'], 'Sports')
        self.login()
        self.assertIn('Automatically categorized', self.client.get('/admin/news').text)
        response = self.client.post('/admin/news/' + story['id'], data={
            'csrf_token': self.token(), 'version': story['version'], 'action': 'categorize', 'topic': 'Health'})
        self.assertEqual(response.status_code, 302)
        with patch('editorial.classify') as classifier:
            self.assertEqual(self.room.ingest(self.source, [self.article]), 0)
            classifier.assert_not_called()
        self.assertEqual(self.room.public_articles('tanzania', 'Health')[0]['topic'], 'Health')

    def test_editor_wins_during_classification(self):
        def edit(*args):
            with self.room.db() as db:
                self.room.sql(db, "UPDATE newsroom_articles SET version = 2, status = 'rejected'")
            return 'Sports', 'Automatic result'
        with patch('editorial.classify', side_effect=edit):
            story = self.seed()
        self.assertEqual(story['status'], 'rejected')
        self.assertIsNone(story['topic'])

    def test_home_pagination_search_and_no_duplicate_stories(self):
        with patch.dict(os.environ, {'NEWS_AUTO_CATEGORIZE': 'off'}):
            self.room.ingest(self.source, [
                {'title': f'Archive headline {i}', 'summary': 'Reporting',
                 'link': f'https://example.com/archive/{i}'} for i in range(45)])
        def story_links(response):
            soup = BeautifulSoup(response.text, 'html.parser')
            return [a['href'] for a in soup.select('.lead-story, .brief-item, .editorial-row')]
        with patch.object(self.room, 'public_articles', side_effect=AssertionError('Full archive loaded')):
            first = self.client.get('/')
            second = self.client.get('/?page=2')
            last = self.client.get('/?page=999')
            self.assertEqual(len(story_links(first)), 20)
            self.assertEqual(len(set(story_links(first))), 20)
            self.assertEqual(len(story_links(second)), 20)
            self.assertEqual(len(story_links(last)), 5)
            self.assertFalse(set(story_links(first)) & set(story_links(second)))
            self.assertNotIn('aria-label="Lead stories"', second.text)
            self.assertIn('Page 3 of 3', last.text)
            self.assertEqual(len(story_links(self.client.get('/?page=bad'))), 20)
            self.assertIn('1 stories', self.client.get('/?q=Archive+headline+44').text)
            self.assertIn('No matching stories.', self.client.get('/?q=%25').text)
            search = self.client.get('/search?q=Archive&page=2')
            self.assertEqual(len(BeautifulSoup(search.text, 'html.parser').select('a.article')), 20)
            self.assertIn('Page 2 of 3', search.text)
        with self.room.db() as db:
            self.room.sql(db, "UPDATE newsroom_articles SET status = 'published', topic = 'Business'")
        edition = self.client.get('/news/tanzania/business?language=Original&q=Archive&page=2')
        soup = BeautifulSoup(edition.text, 'html.parser')
        following = soup.select_one('.news-pagination a:last-child')['href']
        self.assertIn('page=3', following)
        self.assertIn('q=Archive', following)
        self.assertIn('language=Original', following)
        self.assertEqual(self.client.get(following).status_code, 200)

    def test_inline_feedback_json_and_no_javascript(self):
        self.assertIn('id="home-feedback-form"', self.client.get('/').text)
        invalid = self.client.post('/', data={'name': 'Asha', 'comment': 'Hello'},
                                   headers={'Accept': 'application/json'})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('country', invalid.json['errors'])
        valid = self.client.post('/?page=2', data={'name': 'Inline Reader', 'country': 'Kenya', 'comment': 'Inline message'},
                                 headers={'Accept': 'application/json'})
        self.assertTrue(valid.json['ok'])
        response = self.client.post('/news/tanzania/business?language=Original',
            data={'name': 'Fallback Reader', 'country': 'Kenya', 'comment': 'Fallback message'}, follow_redirects=True)
        self.assertIn('Thank you for your feedback!', response.text)
        self.assertIn('id="home-feedback-form"', response.text)
        invalid = self.client.post('/', data={'name': 'Retained name'})
        self.assertEqual(invalid.status_code, 400)
        self.assertIn('value="Retained name"', invalid.text)
        self.login()
        inbox = self.client.get('/admin/feedback?q=Inline+Reader')
        self.assertIn('Inline message', inbox.text)
        self.assertIn('Source: Web', inbox.text)

    def test_archive_reclassification_preview_apply_and_editor_protection(self):
        with patch.dict(os.environ, {'NEWS_AUTO_CATEGORIZE': 'off'}):
            self.article['title'] = 'Mikopo ya asilimia kumi kuinua wananchi Bukoba'
            story = self.seed()
            self.room.ingest(self.source, [{**self.article, 'link': 'https://example.com/protected'}])
        with self.room.db() as db:
            self.room.sql(db, "UPDATE newsroom_articles SET version = 2 WHERE link = ?", ('https://example.com/protected',))
        runner = self.app.test_cli_runner()
        preview = runner.invoke(args=['categorize-news', '--country', 'tanzania'])
        self.assertEqual(preview.exit_code, 0, preview.output)
        self.assertIn('Would assign: 1', preview.output)
        self.assertEqual(len(self.room.public_articles('tanzania', 'Uncategorized')), 2)
        applied = runner.invoke(args=['categorize-news', '--country', 'tanzania', '--apply'])
        self.assertEqual(applied.exit_code, 0, applied.output)
        self.assertIn('Assigned: 1', applied.output)
        self.assertEqual(len(self.room.public_articles('tanzania', 'Business')), 1)
        self.assertEqual(len(self.room.public_articles('tanzania', 'Uncategorized')), 1)
        self.assertIn('Assigned: 0', runner.invoke(args=['categorize-news', '--apply']).output)


if __name__ == '__main__':
    unittest.main()
