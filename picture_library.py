"""The shared picture library: a picture the tutor makes for a class is kept and reused for every student who needs it.

Why: making a picture costs money and 10-30 seconds. Many students learn the same topics (the human heart, a map of
Afghanistan, the water cycle), so the same picture is asked for again and again in slightly different words.

What is kept: ONLY pictures Khayyam made itself from a text description, with no letters or words in them. Never a
student's photo, page or handwriting, and never a chat picture (those can be personal). So nothing about one student
ever reaches another.

Matching: the picture's short topic (or else its description) is reduced to its content words (filler and style words dropped, simple plurals folded);
a stored picture is reused when the shape (aspect ratio) is the same and the two word sets overlap strongly
(shared words / the larger set >= MATCH). Strict on purpose: a wrong picture in a class is worse than a new one.
A reused picture costs nothing and does not count toward the student's monthly pictures.
"""
import hashlib
import json
import os
import re
import uuid
from datetime import datetime

from sqlalchemy import text

MAX_ROWS = int(os.getenv('PICTURE_LIBRARY_MAX', '3000'))           # about 150 KB each: ~450 MB at most; least used go first
MATCH = float(os.getenv('PICTURE_LIBRARY_MATCH', '0.75'))
MIN_WORDS = 3                                                        # a description this short says too little to reuse safely
ENABLED = os.getenv('PICTURE_LIBRARY', 'on').lower() != 'off'

_STOP = set('''a an the of with and or in on at to for from by into onto over under its it is are be as this that these those
there their them they which who very some any all each few more most so such than then too also just only same
showing shows show shown depicting depicts depict featuring feature illustration illustrated picture image drawing diagram
style simple clean educational education school student students kid kids child children friendly clear clearly plain
background white light soft bright colorful colourful color colors colour colours detailed detail high quality vector flat
no text letters letter words word label labels labeled labelled writing written'''.split())
_WORD = re.compile(r'[a-z0-9]+')


def _fold(w):
    if len(w) > 4 and w.endswith('ies'):
        return w[:-3] + 'y'
    if len(w) > 4 and w.endswith('es') and w[-3] in 'sxz':
        return w[:-2]
    if len(w) > 3 and w.endswith('s') and not w.endswith('ss'):
        return w[:-1]
    return w


def words(prompt):
    """The content words of a description, folded and sorted (so word order and plurals do not matter)."""
    out = set()
    for w in _WORD.findall(str(prompt or '').lower()):
        if len(w) < 2 or w in _STOP:
            continue
        w = _fold(w)
        if w not in _STOP:
            out.add(w)
    return sorted(out)


def keys(prompt, topic=None):
    """The words a picture is filed under: its short topic when the tutor gave one (2+ words), else its description (3+ words)."""
    t = words(topic)
    if len(t) >= 2:
        return t
    p = words(prompt)
    return p if len(p) >= MIN_WORDS else []


def similarity(a, b):
    a, b = set(a), set(b)
    if len(a) < 2 or len(b) < 2:
        return 0.0
    return len(a & b) / float(max(len(a), len(b)))


def ensure(db):
    blob = 'BYTEA' if db.engine.dialect.name == 'postgresql' else 'BLOB'
    with db.engine.begin() as conn:
        conn.execute(text(f'''CREATE TABLE IF NOT EXISTS picture_library (
            id VARCHAR(32) PRIMARY KEY,
            sig VARCHAR(64) NOT NULL,
            words TEXT NOT NULL,
            prompt TEXT NOT NULL,
            aspect VARCHAR(8) NOT NULL,
            mime VARCHAR(40) NOT NULL,
            w INTEGER NOT NULL DEFAULT 0,
            h INTEGER NOT NULL DEFAULT 0,
            meta TEXT,
            uses INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0,
            data {blob} NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)'''))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_piclib_aspect ON picture_library (aspect, blocked)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_piclib_sig ON picture_library (sig)'))


def _sig(ws, aspect):
    return hashlib.sha256((aspect + '|' + ' '.join(ws)).encode('utf-8')).hexdigest()


def find(db, prompt, aspect, topic=None):
    """The best stored picture for this description, or None. -> {'id','prompt','w','h','mime','data','meta','score'}"""
    if not ENABLED:
        return None
    ws = keys(prompt, topic)
    if not ws:
        return None
    rows = db.session.execute(text('SELECT id, sig, words FROM picture_library WHERE aspect=:a AND blocked=0'),
                              {'a': aspect}).all()
    sig = _sig(ws, aspect)
    best, best_score = None, 0.0
    for rid, rsig, rwords in rows:
        score = 1.0 if rsig == sig else similarity(ws, (rwords or '').split())
        if score > best_score:
            best, best_score = rid, score
    if not best or best_score < MATCH:
        return None
    row = db.session.execute(text('SELECT id, prompt, w, h, mime, meta, data FROM picture_library WHERE id=:i'), {'i': best}).mappings().first()
    if not row or row['data'] is None:
        return None
    try:
        meta = json.loads(row['meta']) if row['meta'] else {}
    except ValueError:
        meta = {}
    return {'id': row['id'], 'prompt': row['prompt'], 'w': int(row['w'] or 0), 'h': int(row['h'] or 0), 'mime': row['mime'],
            'data': bytes(row['data']), 'meta': meta if isinstance(meta, dict) else {}, 'score': round(best_score, 3)}


def used(db, lid):
    db.session.execute(text('UPDATE picture_library SET uses=uses+1, last_used=:n WHERE id=:i'), {'n': datetime.utcnow(), 'i': lid})
    db.session.commit()


def add(db, prompt, aspect, data, w, h, mime='image/jpeg', topic=None):
    """Keep a newly made picture. -> its id, or None when it was not kept (too vague, a twin exists, library off)."""
    if not ENABLED or not data:
        return None
    ws = keys(prompt, topic)
    if not ws:
        return None
    sig = _sig(ws, aspect)
    if db.session.execute(text('SELECT 1 FROM picture_library WHERE sig=:s'), {'s': sig}).first():
        return None
    lid = uuid.uuid4().hex
    now = datetime.utcnow()
    db.session.execute(text('''INSERT INTO picture_library (id, sig, words, prompt, aspect, mime, w, h, meta, uses, blocked, data, created_at, last_used)
        VALUES (:i, :s, :ws, :p, :a, :m, :w, :h, NULL, 0, 0, :d, :n, :n)'''),
        {'i': lid, 's': sig, 'ws': ' '.join(ws), 'p': str(prompt)[:700], 'a': aspect, 'm': mime, 'w': int(w or 0), 'h': int(h or 0), 'd': data, 'n': now})
    db.session.commit()
    _prune(db)
    return lid


def _prune(db):
    n = db.session.execute(text('SELECT COUNT(*) FROM picture_library')).scalar() or 0
    extra = int(n) - MAX_ROWS
    if extra <= 0:
        return
    ids = [r[0] for r in db.session.execute(text('SELECT id FROM picture_library ORDER BY uses ASC, last_used ASC LIMIT :k'), {'k': extra}).all()]
    for i in ids:
        db.session.execute(text('DELETE FROM picture_library WHERE id=:i'), {'i': i})
    db.session.commit()


def save_look(db, lid, look):
    """What the picture reader found in it (summary and regions to point at), so the next class can point at once."""
    if not lid or not isinstance(look, dict):
        return
    keep = {k: look.get(k) for k in ('kind', 'summary', 'text', 'regions') if look.get(k) is not None}
    if not keep.get('regions'):
        return
    db.session.execute(text('UPDATE picture_library SET meta=:m WHERE id=:i'), {'m': json.dumps(keep, ensure_ascii=False), 'i': lid})
    db.session.commit()


def block(db, lid):
    """Never offer this picture again (an admin found it wrong or poor)."""
    db.session.execute(text('UPDATE picture_library SET blocked=1 WHERE id=:i'), {'i': lid})
    db.session.commit()


def stats(db):
    r = db.session.execute(text('SELECT COUNT(*), COALESCE(SUM(uses),0) FROM picture_library WHERE blocked=0')).first()
    return {'pictures': int(r[0] or 0), 'reuses': int(r[1] or 0)}
