"""Approximate visitor country without persisting or sending IPs to third parties."""
import ipaddress
import os
import re


def trusted_peer(address):
    try:
        peer = ipaddress.ip_address(address)
        networks = [ipaddress.ip_network(value.strip()) for value in
                    os.environ.get('GEO_TRUSTED_PROXY_CIDRS', '').split(',') if value.strip()]
        return any(peer in network for network in networks)
    except ValueError:
        return False


def visitor_country(request):
    peer = request.remote_addr or ''
    if trusted_peer(peer):
        if os.environ.get('GEO_COUNTRY_HEADER') == 'CF-IPCountry':
            code = request.headers.get('CF-IPCountry', '').upper()
            if re.fullmatch('[A-Z]{2}', code) and code not in ('XX', 'T1'):
                return code
        # Only follow a forwarding chain through explicitly trusted proxies.
        chain = [part.strip() for part in request.headers.get('X-Forwarded-For', '').split(',') if part.strip()]
        while chain and trusted_peer(peer):
            peer = chain.pop()
    path = os.environ.get('GEOIP_DATABASE_PATH', '').strip()
    if not path:
        return 'Unknown'
    try:
        if not ipaddress.ip_address(peer).is_global:
            return 'Unknown'
        import geoip2.database
        import geoip2.errors
        with geoip2.database.Reader(path) as reader:
            return reader.country(peer).country.iso_code or 'Unknown'
    except (ImportError, OSError, ValueError):
        return 'Unknown'
    except Exception:
        # Missing/corrupt records must never stop a page request.
        return 'Unknown'
