import os
import unittest
from unittest.mock import Mock, patch

import requests
from categorization import classify, local_category, TERMS


class ClassificationTests(unittest.TestCase):
    def test_bilingual_and_ambiguous(self):
        cases = [
            ('Kocha ajiandaa kwa ligi', 'Timu yafunga mabao matatu', 'Sports'),
            ('Bank investment rises', 'Investors increase trade and exports', 'Business'),
            ('Chanjo mpya hospitali', 'Madaktari wahudumia wagonjwa', 'Health'),
            ('Artificial intelligence software launched', 'Digital technology update', 'Technology'),
            ('Msanii atangaza album', 'Tamasha la muziki', 'Entertainment'),
            ('Scientists discover planet', 'Astronomy discovery', 'Science'),
            ('Bunge lajadili katiba', 'Wabunge wakutana', 'National'),
            ('United Nations urges ceasefire', 'Security council meets', 'World'),
            ('Latest updates today', 'Read more', None),
            ('Football vaccine', 'Football vaccine', None),
            ('Banking on victory', 'A personal story', None),
        ]
        for title, summary, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(local_category(title, summary, list(TERMS))[0], expected)

    @patch.dict(os.environ, {'NEWS_AUTO_CATEGORIZE': 'ai', 'OPENAI_API_KEY': 'test-only'})
    @patch('categorization.requests.post')
    def test_ai_validation_and_failures(self, post):
        import json
        def output(category='Sports', confidence=0.95):
            return {'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': json.dumps({'category': category,
                    'confidence': confidence, 'reason': 'Primary subject'})}]}]}
        post.return_value = Mock()
        for category, confidence, expected in [('Sports', .95, 'Sports'), ('Sports', .6, None),
                                               ('Unknown', .99, None), ('Sports', True, None),
                                               ('Sports', float('nan'), None)]:
            post.return_value.json.return_value = output(category, confidence)
            self.assertEqual(classify('Headline', 'Summary', 'tanzania', list(TERMS))[0], expected)
        post.side_effect = requests.Timeout()
        self.assertIsNone(classify('Headline', '', 'tanzania', list(TERMS))[0])

    @patch.dict(os.environ, {'NEWS_AUTO_CATEGORIZE': 'ai', 'OPENAI_API_KEY': ''})
    @patch('categorization.requests.post')
    def test_missing_key(self, post):
        self.assertIsNone(classify('Football league', 'Coach', 'tanzania', list(TERMS))[0])
        post.assert_not_called()

