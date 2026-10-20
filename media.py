"""Files the chat creates for a user (pictures, PDFs, Word and Excel files).

Stored in the database so nothing is lost when the web server restarts or redeploys.
A file is only ever served to the account that owns it (see the /media route in app.py).
Old files are removed by cleanup_media(), called from the daily sweep.
"""
import json
import os
import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text

_ID = re.compile(r'^[0-9a-f]{32}$')
MAX_BYTES = 12 * 1024 * 1024
KINDS = ('image', 'pdf', 'docx', 'xlsx')


def valid_id(x):
    return bool(isinstance(x, str) and _ID.match(x))


def ensure_media_storage(db):
    blob = 'BYTEA' if db.engine.dialect.name == 'postgresql' else 'BLOB'
    with db.engine.begin() as conn:
        conn.execute(text(f'''CREATE TABLE IF NOT EXISTS media_files (
            id VARCHAR(32) PRIMARY KEY,
            owner_key VARCHAR(160) NOT NULL,
            conversation_id INTEGER,
            kind VARCHAR(8) NOT NULL,
            mime VARCHAR(100) NOT NULL,
            name VARCHAR(160) NOT NULL,
            size INTEGER NOT NULL,
            hidden INTEGER NOT NULL DEFAULT 0,
            meta TEXT,
            data {blob} NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)'''))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_media_owner ON media_files (owner_key, created_at)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_media_conv ON media_files (conversation_id)'))


def safe_name(name, ext):
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', ' ', str(name or '').strip())
    base = re.sub(r'\s+', '_', base).strip('._ ')[:80] or 'khayyam'
    return '%s.%s' % (re.sub(r'\.(pdf|docx|xlsx|jpe?g|png)$', '', base, flags=re.I) or 'khayyam', ext)


def save(db, owner, kind, mime, name, data, conversation_id=None, meta=None, hidden=False):
    if kind not in KINDS:
        raise ValueError('bad kind')
    if not data or len(data) > MAX_BYTES:
        raise ValueError('file too large or empty')
    mid = uuid.uuid4().hex
    db.session.execute(text('''INSERT INTO media_files
        (id, owner_key, conversation_id, kind, mime, name, size, hidden, meta, data)
        VALUES (:id, :o, :c, :k, :m, :n, :s, :h, :meta, :d)'''),
        {'id': mid, 'o': owner, 'c': conversation_id, 'k': kind, 'm': mime, 'n': name[:160], 's': len(data),
         'h': 1 if hidden else 0, 'meta': json.dumps(meta, ensure_ascii=False) if meta else None, 'd': data})
    db.session.commit()
    return mid


def get(db, mid, with_data=True):
    if not valid_id(mid):
        return None
    cols = 'id, owner_key, conversation_id, kind, mime, name, size, hidden, meta, created_at' + (', data' if with_data else '')
    row = db.session.execute(text('SELECT %s FROM media_files WHERE id=:i' % cols), {'i': mid}).mappings().first()
    if not row:
        return None
    out = dict(row)
    if out.get('data') is not None:
        out['data'] = bytes(out['data'])
    try:
        out['meta'] = json.loads(out['meta']) if out.get('meta') else {}
    except ValueError:
        out['meta'] = {}
    return out


def recent(db, owner, conversation_id, limit=6):
    """Newest visible files of one conversation (for 'make it bluer' style follow-ups)."""
    if conversation_id is None:
        return []
    rows = db.session.execute(text('''SELECT id, kind, name, meta FROM media_files
        WHERE owner_key=:o AND conversation_id=:c AND hidden=0 ORDER BY created_at DESC LIMIT :n'''),
        {'o': owner, 'c': conversation_id, 'n': int(limit)}).mappings().all()
    out = []
    for r in rows:
        try:
            meta = json.loads(r['meta']) if r['meta'] else {}
        except ValueError:
            meta = {}
        out.append({'id': r['id'], 'kind': r['kind'], 'name': r['name'], 'meta': meta})
    return out


def attach_to_conversation(db, ids, owner, conversation_id):
    for mid in ids:
        if valid_id(mid):
            db.session.execute(text('UPDATE media_files SET conversation_id=:c WHERE id=:i AND owner_key=:o'),
                               {'c': conversation_id, 'i': mid, 'o': owner})
    db.session.commit()


def delete_for_conversation(db, owner, conversation_id):
    db.session.execute(text('DELETE FROM media_files WHERE owner_key=:o AND conversation_id=:c'),
                       {'o': owner, 'c': conversation_id})
    db.session.commit()


def cleanup_media(db, days=None):
    """Delete files older than MEDIA_RETENTION_DAYS (default 60). Returns how many were removed."""
    days = int(days or os.getenv('MEDIA_RETENTION_DAYS', '60'))
    cutoff = datetime.utcnow() - timedelta(days=days)
    res = db.session.execute(text('DELETE FROM media_files WHERE created_at < :c'), {'c': cutoff})
    db.session.commit()
    return res.rowcount or 0