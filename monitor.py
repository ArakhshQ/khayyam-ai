"""Monitoring: error alerts, cost alerts and a small error log for the admin.

Everything here is best effort and can never break a request:
- ERROR-level log lines (including unhandled 500s) are kept in a short in-memory list and sent to ALERT_WEBHOOK_URL (Slack, Discord, Telegram-bridge
  or any URL that accepts a JSON POST with "text"/"content") and/or ALERT_EMAIL_TO (uses the site's SMTP). The same message is sent at most once
  per 10 minutes and at most 20 alerts per hour, so a storm of errors cannot flood anyone.
- Spend alerts: when today's total cost passes 50% and 80% of the daily breaker (or ALERT_DAILY_USD), and when the breaker itself opens.
Alerts never contain user passwords or email addresses (only the log message and the last exception line).
"""
import collections
import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

_RING = collections.deque(maxlen=60)
_SENT = {}
_HOUR = collections.deque(maxlen=200)
_LOCK = threading.Lock()
_EMAIL = {'fn': None}
_STARTED = time.time()


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def configured():
    return bool(os.getenv('ALERT_WEBHOOK_URL', '').strip() or os.getenv('ALERT_EMAIL_TO', '').strip())


def _deliver(subject, body):
    url = os.getenv('ALERT_WEBHOOK_URL', '').strip()
    if url.startswith('https://'):
        try:
            data = json.dumps({'text': '%s\n%s' % (subject, body), 'content': ('%s\n%s' % (subject, body))[:1900]}).encode()
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json', 'User-Agent': 'khayyam-monitor'}, method='POST')
            urllib.request.urlopen(req, timeout=6).read()
        except Exception:
            pass
    to = os.getenv('ALERT_EMAIL_TO', '').strip()
    if to and _EMAIL['fn']:
        try:
            _EMAIL['fn'](to, subject, subject, body[:600], [])
        except Exception:
            pass


def notify(subject, body='', key=None, every=600):
    """Send an alert in the background. Same key: at most once per `every` seconds; at most 20 alerts per hour overall."""
    try:
        now = time.time()
        key = key or subject
        with _LOCK:
            if now - _SENT.get(key, 0) < every:
                return False
            while _HOUR and now - _HOUR[0] > 3600:
                _HOUR.popleft()
            if len(_HOUR) >= 20:
                return False
            _SENT[key] = now
            _HOUR.append(now)
            if len(_SENT) > 500:
                for k in sorted(_SENT, key=_SENT.get)[:250]:
                    _SENT.pop(k, None)
        if not configured():
            return False
        site = os.getenv('SITE_NAME', 'Khayyam')
        threading.Thread(target=_deliver, args=('[%s] %s' % (site, subject), '%s\n%s' % (_now_iso(), body)), daemon=True).start()
        return True
    except Exception:
        return False


class AlertHandler(logging.Handler):
    """Keeps ERROR/CRITICAL records and alerts on them."""
    def emit(self, record):
        try:
            if record.levelno < logging.ERROR or record.name.startswith(('monitor', 'urllib3', 'werkzeug')):
                return
            msg = record.getMessage()[:300]
            last = ''
            if record.exc_info and record.exc_info[1] is not None:
                last = '%s: %s' % (record.exc_info[0].__name__, str(record.exc_info[1])[:200])
            item = {'at': _now_iso(), 'logger': record.name, 'message': msg, 'exception': last}
            with _LOCK:
                _RING.appendleft(item)
            notify('Error: ' + msg[:90], '%s\n%s' % (msg, last), key='err:%s:%s' % (record.name, msg[:60]))
        except Exception:
            pass


def recent_errors():
    with _LOCK:
        return list(_RING)


def check_spend(spend_micro, limit_usd):
    """Called whenever the global daily spend is recomputed."""
    try:
        alert_at = float(os.getenv('ALERT_DAILY_USD', '0') or 0) or (limit_usd * 0.5 if limit_usd > 0 else 0)
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        usd = spend_micro / 1e6
        if limit_usd > 0 and usd >= limit_usd:
            notify('SPENDING STOPPED: daily limit reached', 'Today (UTC) all users together cost $%.2f, the daily limit is $%.2f. New voice sessions, tools and text requests are refused until 00:00 UTC.' % (usd, limit_usd),
                   key='spend100:' + day, every=86400)
        elif limit_usd > 0 and usd >= limit_usd * 0.8:
            notify('Spending at 80% of the daily limit', 'Today (UTC) all users together cost $%.2f of the $%.2f limit.' % (usd, limit_usd), key='spend80:' + day, every=86400)
        elif alert_at > 0 and usd >= alert_at:
            notify('Spending is high today', 'Today (UTC) all users together cost $%.2f (alert level $%.2f, daily limit $%.2f).' % (usd, alert_at, limit_usd), key='spend50:' + day, every=86400)
    except Exception:
        pass


def init(send_email=None):
    """Attach the handler once (root logger, so every module is covered)."""
    _EMAIL['fn'] = send_email
    root = logging.getLogger()
    if not any(isinstance(h, AlertHandler) for h in root.handlers):
        h = AlertHandler(level=logging.ERROR)
        root.addHandler(h)
        if root.level == logging.NOTSET or root.level > logging.ERROR:
            root.setLevel(min(root.level or logging.WARNING, logging.WARNING))


def uptime_seconds():
    return int(time.time() - _STARTED)
