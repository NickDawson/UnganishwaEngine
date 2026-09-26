"""Country language choices and server-side Google Cloud Translation."""
from collections import OrderedDict
from html import unescape
from urllib.parse import urlencode
import os
from threading import Lock

from bs4 import BeautifulSoup
import requests

LANGUAGES = {
    'English': 'en', 'Kiswahili': 'sw', 'Kinyarwanda': 'rw',
    'Luganda': 'lg', 'Kirundi': 'rn', 'Français': 'fr',
    'Lingala': 'ln', 'Kituba': 'ktu', 'Somali': 'so',
    'العربية': 'ar', 'Dinka': 'din', 'Nuer': 'nus',
}
COUNTRY_LANGUAGES = {
    'tanzania': ['Kiswahili', 'English'],
    'kenya': ['Kiswahili', 'English'],
    'uganda': ['Luganda', 'English', 'Kiswahili'],
    'rwanda': ['Kinyarwanda', 'English', 'Français', 'Kiswahili'],
    'burundi': ['Kirundi', 'Français', 'English', 'Kiswahili'],
    'congo': ['Français', 'Lingala', 'Kiswahili', 'Kituba', 'English'],
    'somalia': ['Somali', 'العربية', 'English'],
    'south-sudan': ['English', 'Dinka', 'Nuer', 'العربية'],
}
_cache = OrderedDict()
_lock = Lock()


class TranslationUnavailable(Exception):
    pass


def resolve_language(country, language):
    choices = COUNTRY_LANGUAGES[country]
    if not language or language == 'Original':
        return 'Original'
    return next((name for name in choices if language in (name, LANGUAGES[name])), None)


def google_website_url(url, language):
    """Translate a public Original-language page without exposing server credentials."""
    return 'https://translate.google.com/translate?' + urlencode({
        'sl': 'auto', 'tl': LANGUAGES[language], 'u': url})


def translate_texts(texts, language):
    """Auto-detect source language; cache only successful, complete translations."""
    if language == 'Original' or not texts:
        return list(texts)
    key = os.environ.get('GOOGLE_TRANSLATE_API_KEY', '').strip()
    if not key:
        raise TranslationUnavailable('Translation is not configured')
    target = LANGUAGES[language]
    with _lock:
        found = {text: _cache[(target, text)] for text in texts if (target, text) in _cache}
    missing = list(dict.fromkeys(text for text in texts if text not in found))
    batches = []
    batch = []
    size = 0
    for text in missing:
        if len(text) > 10000:
            raise TranslationUnavailable('Text is too long')
        if batch and (len(batch) >= 100 or size + len(text) > 10000):
            batches.append(batch)
            batch, size = [], 0
        batch.append(text)
        size += len(text)
    if batch:
        batches.append(batch)
    try:
        for batch in batches:
            response = requests.post(
                'https://translation.googleapis.com/language/translate/v2',
                headers={'X-goog-api-key': key},
                json={'q': batch, 'target': target, 'format': 'text'}, timeout=15)
            response.raise_for_status()
            rows = response.json()['data']['translations']
            if len(rows) != len(batch):
                raise ValueError('Incomplete translation')
            for original, row in zip(batch, rows):
                value = row['translatedText']
                if not isinstance(value, str) or not value.strip():
                    raise ValueError('Invalid translation')
                found[original] = unescape(value)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise TranslationUnavailable('Translation temporarily unavailable') from exc
    with _lock:
        for text in missing:
            _cache[(target, text)] = found[text]
        while len(_cache) > 4096:
            _cache.popitem(last=False)
    return [found[text] for text in texts]


def translate_page(html, language):
    if language == 'Original':
        return html
    soup = BeautifulSoup(html, 'html.parser')
    nodes = [node for node in soup.body.find_all(string=True)
             if type(node).__name__ == 'NavigableString' and node.strip()
             and not any(parent.name in {'script', 'style', 'code', 'noscript', 'option'}
                         or parent.get('translate') == 'no' for parent in node.parents)]
    attrs = [(tag, attr) for tag in soup.body.find_all(True)
             for attr in ('placeholder', 'aria-label', 'title') if tag.get(attr)]
    texts = [str(node).strip() for node in nodes] + [tag[attr] for tag, attr in attrs]
    translated = translate_texts(texts, language)
    for node, value in zip(nodes, translated):
        node.replace_with(str(node).replace(str(node).strip(), value, 1))
    for (tag, attr), value in zip(attrs, translated[len(nodes):]):
        tag[attr] = value
    soup.html['lang'] = LANGUAGES[language]
    soup.body['dir'] = 'rtl' if LANGUAGES[language] == 'ar' else 'ltr'
    return str(soup)


def translate_articles(articles, language):
    copies = [dict(article) for article in articles]
    fields = [(article, field) for article in copies for field in ('title', 'summary')
              if article.get(field)]
    values = translate_texts([article[field] for article, field in fields], language)
    for (article, field), value in zip(fields, values):
        article[field] = value
    return copies
