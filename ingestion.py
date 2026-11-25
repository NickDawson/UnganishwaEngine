"""Bounded feed downloads and RSS/Atom parsing for polling and WebSub."""
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import feedparser
import requests

MAX_FEED_BYTES = 2 * 1024 * 1024


def public_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('A public HTTP(S) URL is required')
    addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == 'https' else 80))
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('Private network addresses are not allowed')
    return url


def download(url):
    """Check each redirect, bound response size and use explicit timeouts."""
    for _ in range(5):
        public_url(url)
        with requests.get(url, timeout=(5, 15), stream=True, allow_redirects=False,
                          headers={'User-Agent': 'UnganishwaBot/1.0'}) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers['Location'])
                continue
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise ValueError('Feed exceeds maximum size')
                chunks.append(chunk)
            return b''.join(chunks), dict(response.links), url
    raise ValueError('Too many redirects')


def feed_image(entry):
    candidates = [*entry.get('media_content', []), *entry.get('links', [])]
    for item in candidates:
        mime = item.get('type', '').lower()
        if item.get('medium') != 'image' and not mime.startswith('image/'):
            continue
        url = item.get('url') or item.get('href', '')
        try:
            parts = urlsplit(url)
            if (len(url) <= 2048 and parts.scheme == 'https' and parts.hostname
                    and not parts.username and not parts.password):
                return url
        except ValueError:
            continue
    return None


def feed_articles(payload, source):
    parsed = feedparser.parse(payload)
    if not parsed.version and not parsed.entries:
        raise ValueError('Not an RSS or Atom feed')
    return [{'title': entry.get('title', ''), 'summary': entry.get('summary', ''),
             'link': entry.get('link', ''), 'image_url': feed_image(entry), 'published': entry.get('published', 'Recently')}
            for entry in parsed.entries[:200]]
