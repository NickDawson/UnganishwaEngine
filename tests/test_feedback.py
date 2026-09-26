import sqlite3
import unittest
from flask import Flask
from api import register_api


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE reader_feedback (id TEXT PRIMARY KEY, name TEXT, comment TEXT, country TEXT, created_at TEXT, source TEXT)')
        app = Flask(__name__)
        register_api(app, {}, [], lambda *args: [], lambda items: items,
                     get_db=lambda: self.db,
                     execute_sql=lambda db, sql, values: db.execute(sql, values))
        self.client = app.test_client()

    def test_persists_trimmed_feedback(self):
        result = self.client.post('/api/v1/feedback', json={'name': ' Asha ', 'comment': ' Good news ', 'country': ' Tanzania '})
        self.assertEqual(result.status_code, 201)
        self.assertEqual(self.db.execute('SELECT source FROM reader_feedback').fetchone()[0], 'app')
        self.assertEqual(self.db.execute('SELECT name, comment, country FROM reader_feedback').fetchone(), ('Asha', 'Good news', 'Tanzania'))

    def test_rejects_invalid_input(self):
        for payload in [[], {}, {'name': 12}, {'name': 'A', 'comment': 'x' * 2001, 'country': 'TZ'}, {'name': ' ', 'comment': 'OK', 'country': 'TZ'}]:
            self.assertEqual(self.client.post('/api/v1/feedback', json=payload).status_code, 400)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM reader_feedback').fetchone()[0], 0)
