from datetime import datetime, timezone
import re

from flask import jsonify, request
from translation import (COUNTRY_LANGUAGES, LANGUAGES, resolve_language,
                         translate_articles, TranslationUnavailable)


def register_api(app, countries, topics, load_articles, deduplicate_articles,
                 cors_origin='*', get_db=None, execute_sql=None, database_path=None):
    def api_article(article):
        return {
            'title': article.get('title', ''),
            'summary': article.get('summary', ''),
            'source': article.get('source', ''),
            'country': article.get('country', ''),
            'topic': article.get('topic', ''),
            'link': article.get('link', '#'),
            'published': article.get('published', 'Recently'),
            'minutes': article.get('minutes'),
        }

    def api_pagination(page, limit, total):
        return {
            'page': page,
            'limit': limit,
            'total': total,
            'pages': (total + limit - 1) // limit if total else 0,
            'has_next': page * limit < total,
            'has_previous': page > 1,
        }

    @app.route('/api/v1/health', methods=['GET', 'OPTIONS'])
    def api_health():
        if request.method == 'OPTIONS':
            return ('', 204)
        return jsonify({'status': 'ok', 'service': 'unganishwa-news-api', 'version': 'v1'})

    @app.route('/api/v1/countries', methods=['GET', 'OPTIONS'])
    def api_countries():
        if request.method == 'OPTIONS':
            return ('', 204)
        return jsonify({
            'countries': [
                {'id': key, **value, 'languages': [
                    {'name': name, 'code': LANGUAGES[name]}
                    for name in COUNTRY_LANGUAGES[key]]}
                for key, value in countries.items()
            ]
        })

    @app.route('/api/v1/topics', methods=['GET', 'OPTIONS'])
    def api_topics():
        if request.method == 'OPTIONS':
            return ('', 204)
        return jsonify({'topics': topics})

    @app.route('/api/v1/articles', methods=['GET', 'OPTIONS'])
    def api_articles():
        if request.method == 'OPTIONS':
            return ('', 204)
        country = request.args.get('country', 'tanzania').lower()
        topic = request.args.get('topic', 'Top Stories')
        if country not in countries:
            return jsonify({'error': 'Unsupported country', 'available': list(countries)}), 400
        if topic not in topics:
            return jsonify({'error': 'Unsupported topic', 'available': topics}), 400
        try:
            page = max(1, int(request.args.get('page', 1)))
            limit = min(50, max(1, int(request.args.get('limit', 20))))
        except ValueError:
            return jsonify({'error': 'page and limit must be integers'}), 400

        language = resolve_language(country, request.args.get('language'))
        if language is None:
            return jsonify({'error': 'Unsupported language for this country',
                            'available': ['Original', *COUNTRY_LANGUAGES[country]]}), 400
        articles = load_articles(country, topic)
        total = len(articles)
        start = (page - 1) * limit
        try:
            translated = translate_articles(articles[start:start + limit], language)
        except TranslationUnavailable:
            return jsonify({'error': 'Translation temporarily unavailable',
                            'code': 'translation_unavailable'}), 503
        return jsonify({
            'data': [api_article(article) for article in translated],
            'pagination': api_pagination(page, limit, total),
            'filters': {'country': country, 'topic': topic, 'language': language},
        })

    @app.route('/api/v1/search', methods=['GET', 'OPTIONS'])
    def api_search():
        if request.method == 'OPTIONS':
            return ('', 204)
        query = request.args.get('q', '').strip()
        if not query:
            return jsonify({'error': 'q is required'}), 400
        try:
            page = max(1, int(request.args.get('page', 1)))
            limit = min(50, max(1, int(request.args.get('limit', 20))))
        except ValueError:
            return jsonify({'error': 'page and limit must be integers'}), 400

        articles = []
        for country in countries:
            articles.extend(load_articles(country, 'Top Stories'))
        matching = [article for article in deduplicate_articles(articles) if query.lower() in
                    (article['title'] + article['summary']).lower()]
        total = len(matching)
        start = (page - 1) * limit
        return jsonify({
            'data': [api_article(article) for article in matching[start:start + limit]],
            'pagination': api_pagination(page, limit, total),
            'query': query,
        })

    @app.route('/api/v1/subscribe', methods=['POST', 'OPTIONS'])
    def api_subscribe():
        if request.method == 'OPTIONS':
            return ('', 204)
        payload = request.get_json(silent=True) or {}
        email = str(payload.get('email', '')).strip().lower()
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
            return jsonify({'error': 'A valid email is required'}), 400
        now = datetime.now(timezone.utc).isoformat()
        db = get_db()
        execute_sql(db, '''
            INSERT INTO subscriptions (email, subscribed_at, source)
            VALUES (?, ?, 'mobile')
            ON CONFLICT (email) DO UPDATE SET subscribed_at = excluded.subscribed_at
        ''', (email, now))
        db.commit()
        return jsonify({'status': 'subscribed'}), 201

    @app.route('/api/v1/notifications/subscribe', methods=['POST', 'OPTIONS'])
    def api_notification_subscribe():
        if request.method == 'OPTIONS':
            return ('', 204)
        payload = request.get_json(silent=True) or {}
        token = str(payload.get('token', '')).strip()
        platform = str(payload.get('platform', '')).strip().lower()
        if not token or platform not in {'android', 'ios', 'web'}:
            return jsonify({'error': 'token and platform (android, ios, or web) are required'}), 400
        db = get_db()
        execute_sql(db, '''
            INSERT INTO notification_subscriptions (token, platform, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT (token) DO UPDATE SET platform = excluded.platform
        ''', (token, platform, datetime.now(timezone.utc).isoformat()))
        db.commit()
        return jsonify({'status': 'registered'}), 201

    return app
