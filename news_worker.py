"""Background feed collection and WebSub lease renewal.

Run as a separate process: python news_worker.py
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import signal
import threading
import time

from app import app, newsroom, websub

stop = threading.Event()


def main():
    interval = max(60, int(os.environ.get('NEWS_POLL_SECONDS', '300')))
    workers = max(1, min(8, int(os.environ.get('NEWS_POLL_WORKERS', '4'))))
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    last_renewal = 0
    while not stop.is_set():
        started = time.monotonic()
        with newsroom.db() as db:
            sources = newsroom.sql(db, 'SELECT * FROM trusted_sources WHERE is_active = 1 ORDER BY id').fetchall()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            pending = {executor.submit(newsroom.collect_source, dict(source)): source['id'] for source in sources}
            for future in as_completed(pending):
                if stop.is_set():
                    for task in pending:
                        task.cancel()
                    break
                try:
                    count = future.result()
                    app.logger.info('Source %s: %s new stories', pending[future], count)
                except Exception:
                    app.logger.warning('Source %s collection failed', pending[future])
        if time.monotonic() - last_renewal >= 3600 and not stop.is_set():
            try:
                websub.renew_command()
            except Exception:
                app.logger.warning('Some WebSub leases could not be renewed; retrying next cycle')
            else:
                last_renewal = time.monotonic()
        stop.wait(max(1, interval - (time.monotonic() - started)))


if __name__ == '__main__':
    main()
