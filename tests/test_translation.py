import os
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from api import register_api
import translation as tr


class TranslationTests(unittest.TestCase):
    def setUp(self):
        tr._cache.clear()
        self.env = patch.dict(os.environ, {'GOOGLE_TRANSLATE_API_KEY': 'test'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self, **kwargs):
        return Mock(json=lambda: {'data': {'translations': [
            {'translatedText': 'T:' + text} for text in kwargs['json']['q']]}})

    def test_country_validation(self):
        self.assertEqual(tr.resolve_language('somalia', 'so'), 'Somali')
        self.assertIsNone(tr.resolve_language('somalia', 'Lingala'))
        self.assertEqual(tr.resolve_language('congo', None), 'Original')
        self.assertIn('Nuer', tr.COUNTRY_LANGUAGES['south-sudan'])

    @patch('translation.requests.post')
    def test_page_translation_preserves_controls_scripts_and_links(self, post):
        post.side_effect = lambda url, **kwargs: self.response(**kwargs)
        html = '<html><body><a href="/news">News</a><select><option value="Somali">Somali</option></select><input placeholder="Search"><script>secret()</script></body></html>'
        output = tr.translate_page(html, 'العربية')
        self.assertIn('dir="rtl"', output)
        self.assertIn('lang="ar"', output)
        self.assertIn('href="/news">T:News', output)
        self.assertIn('value="Somali">Somali', output)
        self.assertIn('placeholder="T:Search"', output)
        self.assertIn('<script>secret()</script>', output)
        tr.translate_page(html, 'العربية')
        self.assertEqual(post.call_count, 1)

    @patch('translation.requests.post')
    def test_articles_do_not_mutate_original_and_validate_response(self, post):
        post.side_effect = lambda url, **kwargs: self.response(**kwargs)
        original = [{'title': 'News', 'summary': 'Details', 'link': 'https://example.com'}]
        result = tr.translate_articles(original, 'Somali')
        self.assertEqual(result[0]['title'], 'T:News')
        self.assertEqual(original[0]['title'], 'News')
        self.assertEqual(result[0]['link'], original[0]['link'])
        tr._cache.clear()
        post.side_effect = None
        post.return_value = Mock(json=lambda: {'data': {'translations': []}})
        with self.assertRaises(tr.TranslationUnavailable):
            tr.translate_articles(original, 'Somali')
        self.assertFalse(tr._cache)

    def test_api_original_invalid_language_and_missing_key(self):
        app = Flask(__name__)
        register_api(app, {'somalia': {'name': 'Somalia'}}, ['Top Stories'],
                     lambda *_: [{'title': 'News', 'summary': 'Details'}], lambda x: x)
        client = app.test_client()
        self.assertEqual(client.get('/api/v1/articles?country=somalia').status_code, 200)
        self.assertEqual(client.get('/api/v1/articles?country=somalia&language=ln').status_code, 400)
        with patch.dict(os.environ, {'GOOGLE_TRANSLATE_API_KEY': ''}):
            self.assertEqual(client.get('/api/v1/articles?country=somalia&language=so').status_code, 503)
        languages = client.get('/api/v1/countries').json['countries'][0]['languages']
        self.assertIn({'name': 'Somali', 'code': 'so'}, languages)


if __name__ == '__main__':
    unittest.main()
