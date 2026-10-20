"""Verified poetry collection: search and quoting.

The poems come from Ganjoor (see tools/fetch_ganjoor.py and tools/build_poetry_db.py) and sit in a
read-only SQLite file next to the app. Nothing here calls the network or the main database, so a
lookup costs a few milliseconds.

What the chat route uses:
    ctx = build_context(user_message, history)   # None, or a prompt block + the candidate poems
    reply_with_sources = add_sources(reply, ctx)  # appends a source line for poems really quoted
"""
import os
import random
import zlib
from functools import lru_cache
import re
import sqlite3
import threading

DB_PATH = os.environ.get('POETRY_DB') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'poetry.db')

# --------------------------------------------------------------------------- normalisation
_DIAC = re.compile('[ً-ٰٟـۖ-ۭ]')
_SEP = re.compile('[‌‍‎‏ ]')
_PUNCT = re.compile(r'[^\w\s]', re.UNICODE)
_MAP = str.maketrans({
    'ي': 'ی', 'ى': 'ی', 'ئ': 'ی', 'ك': 'ک', 'ۀ': 'ه', 'ة': 'ه', 'أ': 'ا', 'إ': 'ا', 'آ': 'ا', 'ؤ': 'و',
    '۰': '0', '۱': '1', '۲': '2', '۳': '3', '۴': '4', '۵': '5', '۶': '6', '۷': '7', '۸': '8', '۹': '9',
    '٠': '0', '١': '1', '٢': '2', '٣': '3', '٤': '4', '٥': '5', '٦': '6', '٧': '7', '٨': '8', '٩': '9',
})


def nrm(text):
    """Search form of a text. The same function builds the index and reads the question."""
    t = str(text or '').lower().translate(_MAP)
    t = _DIAC.sub('', t)
    t = _SEP.sub(' ', t)
    t = _PUNCT.sub(' ', t.replace('_', ' '))
    return ' '.join(t.split())


# --------------------------------------------------------------------------- poets
POETS = {
    'khayyam': {'name': 'خیام', 'aliases': ['خیام', 'خیام نیشابوری', 'عمر خیام', 'khayyam', 'omar khayyam']},
    'hafez': {'name': 'حافظ', 'aliases': ['حافظ', 'حافظ شیرازی', 'hafez', 'hafiz']},
    'saadi': {'name': 'سعدی', 'aliases': ['سعدی', 'سعدی شیرازی', 'شیخ سعدی', 'saadi', 'sadi']},
    'moulavi': {'name': 'مولانا', 'aliases': ['مولانا', 'مولوی', 'رومی', 'جلال الدین بلخی', 'جلال الدین محمد بلخی',
                                              'جلال الدین رومی', 'جلال الدین محمد', 'mowlana', 'rumi', 'molana']},
    'bidel': {'name': 'بیدل', 'aliases': ['بیدل', 'بیدل دهلوی', 'میرزا بیدل', 'عبدالقادر بیدل', 'bedil', 'bidel']},
    'jami': {'name': 'جامی', 'aliases': ['جامی', 'عبدالرحمن جامی', 'نورالدین جامی', 'jami']},
    'naserkhosro': {'name': 'ناصرخسرو', 'aliases': ['ناصرخسرو', 'ناصر خسرو', 'ناصر خسرو قبادیانی', 'حکیم ناصرخسرو',
                                                    'nasir khusraw', 'naser khosrow']},
}
# Poets we do NOT have: the model must not recite them from memory.
MISSING_POETS = ['رابعه', 'رابعه بلخی', 'فردوسی', 'رودکی', 'نظامی', 'عطار', 'فرخی', 'منوچهری', 'انوری', 'عنصری', 'خاقانی',
                 'صائب', 'کلیم', 'عراقی', 'نیما', 'فروغ', 'شهریار', 'اقبال', 'استاد خلیلی', 'خلیل الله خلیلی',
                 'خوشحال خان', 'عبدالرحمن بابا', 'سنایی', 'وحشی', 'ابوسعید', 'باباطاهر', 'بابا طاهر', 'امیرخسرو',
                 'امیر خسرو', 'خسرو دهلوی', 'قاآنی', 'ملک الشعرا بهار', 'پروین', 'سهراب', 'اخوان', 'سیمین', 'شاملو']

# Books / collections, so «گلستان» alone is enough to pick Saadi's Golestan.
BOOKS = [
    ('گلستان', 'saadi', 'گلستان'), ('بوستان', 'saadi', 'بوستان'),
    ('مثنوی معنوی', 'moulavi', 'مثنوی معنوی'), ('مثنوی', 'moulavi', 'مثنوی معنوی'),
    ('دیوان شمس', 'moulavi', 'دیوان شمس'), ('فیه ما فیه', 'moulavi', 'فیه ما فیه'), ('مجالس سبعه', 'moulavi', 'مجالس سبعه'),
    ('هفت اورنگ', 'jami', 'هفت اورنگ'), ('بهارستان', 'jami', 'بهارستان'),
    ('سفرنامه', 'naserkhosro', 'سفرنامه'), ('وجه دین', 'naserkhosro', 'وجه دین'), ('زاد المسافرین', 'naserkhosro', 'زاد المسافرین'),
    ('خوان الاخوان', 'naserkhosro', 'خوان الاخوان'), ('جامع الحکمتین', 'naserkhosro', 'جامع الحکمتین'),
    ('نوروزنامه', 'khayyam', 'نوروزنامه'),
]
FORMS = [  # (word the user writes, form code stored in the database)
    ('رباعی', 'rubai'), ('رباعیات', 'rubai'), ('دوبیتی', 'rubai'), ('ترانه', 'rubai'),
    ('غزل', 'ghazal'), ('غزلیات', 'ghazal'), ('قطعه', 'qete'), ('قطعات', 'qete'),
    ('قصیده', 'qasideh'), ('قصاید', 'qasideh'), ('مثنوی', 'masnavi'),
]

_ASK = ('بخوان', 'بخون', 'بگو', 'بده', 'بیار', 'بفرست', 'معنی', 'تفسیر', 'شرح', 'ترجمه', 'توضیح', 'تعبیر', 'مفهوم', 'کیست', 'کی',
        'کدام', 'مال', 'از کیست', 'نقل', 'مثال', 'نمونه', 'پیشنهاد', 'بیت', 'مصراع', 'یادم', 'میدانی', 'میدونی', 'میشناسی',
        'دارم', 'دانی', 'بنویس برایم', 'چیست', 'یعنی', 'دربارهٔ', 'درباره', 'منظور')
_COMPOSE = ('بسرا', 'بساز', 'بنویس', 'خلق کن', 'بسازید', 'بنویسید', 'تصنیف', 'به سبک', 'در سبک', 'به شیوه', 'در وصف', 'مثل خیام بنویس',
            'شبیه', 'الهام', 'از خودت')
_STOP = set('''و در به از که را با این آن یک است بود هر تا چه چی کی هم یا بر برای من تو او ما شما ایشان ام ات اش مان تان شان
می نمی ای اگر اما ولی پس نیز باید شد شود کرد کنم کن کنید دارد دارم دارند نه هیچ همه خود بی تر ترین یکی
شعر اشعار بیت ابیات مصراع غزل غزلیات رباعی رباعیات قطعه قصیده مثنوی دوبیتی ترانه معنی تفسیر شرح ترجمه توضیح برایم برای
بخوان بخون بگو بده بیار بنویس درباره دربارهٔ کدام کیست مال شاعر دیوان کتاب نقل بفرما لطفا لطفاً ممنون سلام
poem poems ghazal rubai verse of the by from a an'''.split())


_FOLLOW = set(nrm(w) for w in ('بله', 'بلی', 'آره', 'اره', 'بگو', 'بنویس', 'بخوان', 'بخون', 'ادامه', 'کامل', 'کاملش', 'تمام', 'تمامش',
                              'همه', 'همش', 'بقیه', 'بقیهاش', 'معنی', 'تفسیر', 'شرح', 'ترجمه', 'توضیح', 'بیشتر', 'لطفا',
                              'میخوام', 'میخواهم', 'غزل', 'شعر', 'رباعی', 'اش', 'را', 'ش', 'هم', 'آن', 'این', 'بده', 'بیار',
                              'یکجا', 'یک', 'جا', 'و', 'خب', 'باشه', 'خوب', 'اوکی', 'ok', 'yes', 'please', 'بفرما', 'عیناً', 'عینا')) 

# --------------------------------------------------------------------------- database
_local = threading.local()


def _conn():
    c = getattr(_local, 'c', None)
    if c is None:
        if not os.path.exists(DB_PATH):
            return None
        c = sqlite3.connect('file:%s?mode=ro' % DB_PATH, uri=True, check_same_thread=False)
        c.row_factory = sqlite3.Row
        _local.c = c
    return c


def available():
    return os.path.exists(DB_PATH)


# --------------------------------------------------------------------------- understanding the question
def _find_poet(nmsg):
    """(poet_key, book) named in the message. Longest alias first so «ناصر خسرو» beats «خسرو»."""
    padded = ' %s ' % nmsg
    best, best_len = None, 0
    for key, p in POETS.items():
        for a in p['aliases']:
            na = nrm(a)
            if (' %s ' % na) in padded and len(na) > best_len:
                best, best_len = key, len(na)
    book = None
    for word, key, bk in BOOKS:
        if (' %s ' % nrm(word)) in padded:
            book = bk
            if best is None:
                best = key
            break
    return best, book


def _find_form(nmsg):
    toks = set(nmsg.split())
    for w, code in FORMS:
        if nrm(w) in toks:
            return code
    return None


def _missing_poet(nmsg):
    padded = ' %s ' % nmsg
    for n in MISSING_POETS:
        if (' %s ' % nrm(n)) in padded:
            return n
    return None


def _has(nmsg, words):
    padded = ' %s ' % nmsg
    return any((' %s ' % nrm(w)) in padded for w in words)


def _content_tokens(nmsg, drop):
    out = []
    for t in nmsg.split():
        if t in _STOP or t in drop or len(t) < 2 or t.isdigit():
            continue
        out.append(t)
    return out


# --------------------------------------------------------------------------- lookups
_POEM_COLS = 'p.id, p.poet_key, p.poet, p.book, p.section, p.title, p.url, p.status, p.form, p.n'


# Letters people (and keyboards) mix up. Used only for matching, never for the text we quote.
_FOLD = str.maketrans({'ث': 'س', 'ص': 'س', 'ذ': 'ز', 'ض': 'ز', 'ظ': 'ز', 'ط': 'ت', 'ق': 'غ', 'ح': 'ه', 'ع': 'ا'})
_ALT = {'س': 'صث', 'ص': 'سث', 'ث': 'سص', 'ز': 'ذضظ', 'ذ': 'زضظ', 'ض': 'زذظ', 'ظ': 'زذض', 'ت': 'ط', 'ط': 'ت',
        'غ': 'ق', 'ق': 'غ', 'ه': 'ح', 'ح': 'ه', 'ا': 'ع', 'ع': 'ا'}


def _fold(t):
    return t.translate(_FOLD)


def _lev(a, b, cap):
    """Edit distance, giving up above cap (words are short, so this is cheap)."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


@lru_cache(maxsize=300000)
def _near(a, b):
    if a == b:
        return True
    fa, fb = _fold(a), _fold(b)
    if fa == fb:
        return True
    n = min(len(fa), len(fb))
    if n < 3 or abs(len(fa) - len(fb)) > 2:
        return False
    cap = 1 if n <= 5 else 2
    # one edit cannot change both ends of a word, so most unrelated words are dropped without the slow comparison
    if cap == 1 and fa[0] != fb[0] and fa[-1] != fb[-1]:
        return False
    if cap == 2 and len(set(fa) & set(fb)) < n - 2:
        return False
    return _lev(fa, fb, cap) <= cap


def _cover(need, text):
    """Share of the question's words found in a couplet, allowing small spelling slips and glued/split words."""
    ct = nrm(text).split()
    cset, joined = set(ct), ''.join(ct)
    fjoined = _fold(joined)
    hit = 0
    for t in need:
        if t in cset or (len(t) >= 4 and (t in joined or _fold(t) in fjoined)) or any(_near(t, x) for x in ct):
            hit += 1
    return hit / len(need)


def _variants(t):
    out = []
    for i, ch in enumerate(t):
        for alt in _ALT.get(ch, ''):
            out.append(t[:i] + alt + t[i + 1:])
            if len(out) >= 6:
                return out
    return out


def _fts_query(tokens):
    toks = [t for t in tokens if re.fullmatch(r'[\w]+', t)][:10]
    terms = []
    for t in toks:
        terms.append(t)
        if len(t) >= 3:
            terms.extend(_variants(t)[:4])
    seen, uniq = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return ' OR '.join('"%s"' % t for t in uniq[:44])


def _search_couplets(c, tokens, poet, book, form, allow_prose=False, limit=6, min_cover=0.0):
    """Rank couplets by how many of the question's words they contain. Returns [(poem_id, [couplet idx, ...], score)]."""
    q = _fts_query(tokens)
    if not q:
        return []
    sql = ('SELECT cp.poem_id, cp.idx, cp.text, p.status, bm25(cf) AS r FROM cf JOIN couplets cp ON cp.id = cf.rowid '
           'JOIN poems p ON p.id = cp.poem_id WHERE cf MATCH ?')
    args = [q]
    if poet:
        sql += ' AND p.poet_key = ?'
        args.append(poet)
    if book:
        sql += ' AND p.book = ?'
        args.append(book)
    if form:
        sql += ' AND p.form = ?'
        args.append(form)
    if not allow_prose:
        sql += ' AND p.form != "prose"'
    sql += ' ORDER BY r LIMIT 300'
    need_list = list(dict.fromkeys(tokens))
    need = need_list
    scored = {}
    for row in c.execute(sql, args):
        cover = _cover(need_list, row['text'])
        # a pasted line must match almost entirely; a theme word only has to appear
        if len(need) >= 3 and cover < 0.6:
            continue
        key = row['poem_id']
        s = cover - row['r'] * 0.01 + {'authentic': 0.3, 'probable': 0.2, 'attributed': -0.1}.get(row['status'], 0)
        cov = cover
        e = scored.setdefault(key, [0.0, [], 0.0])
        e[1].append(row['idx'])
        e[0] = max(e[0], s)
        e[2] = max(e[2], cov)
    ranked = sorted(scored.items(), key=lambda kv: -kv[1][0])
    if not ranked:
        return []
    top_cov = ranked[0][1][2]
    top = ranked[0][1][0]
    keep = []
    for pid, v in ranked[:limit]:
        if top_cov >= 0.99 and len(need) >= 3 and v[2] < 0.8:
            continue                      # an exact line was found: drop the loose matches around it
        if top - v[0] > 0.6 or v[2] < min_cover:
            continue
        keep.append((pid, sorted(set(v[1])), v[0]))
    return keep


def _random_poems(c, poet, book, form, n=3, seed=0):
    where, args = ['1=1'], []
    if poet:
        where.append('p.poet_key = ?')
        args.append(poet)
    if book:
        where.append('p.book = ?')
        args.append(book)
    if form:
        where.append('p.form = ?')
        args.append(form)
    # Prefer the best-attested poems; for Khayyam that means the scholarly "authentic" and "probable" sets.
    for statuses in (('authentic', 'probable'), ('ok', 'authentic', 'probable')):
        rows = c.execute('SELECT p.id FROM poems p WHERE %s AND p.status IN (%s) AND p.n BETWEEN 1 AND 24 AND p.form != "prose"'
                         % (' AND '.join(where), ','.join('?' * len(statuses))), args + list(statuses)).fetchall()
        if rows:
            break
    ids = [r['id'] for r in rows]
    random.Random(seed).shuffle(ids)
    return ids[:n]


def _load_poem(c, pid, only=None, max_couplets=14):
    p = c.execute('SELECT %s FROM poems p WHERE p.id = ?' % _POEM_COLS, (pid,)).fetchone()
    if not p:
        return None
    rows = c.execute('SELECT idx, text FROM couplets WHERE poem_id = ? ORDER BY idx', (pid,)).fetchall()
    partial = False
    if len(rows) > max_couplets:
        partial = True
        keep = set()
        for i in (only or [0]):
            keep.update(range(max(0, i - 1), i + 2))
        rows = [r for r in rows if r['idx'] in keep][:max_couplets]
    return {'id': p['id'], 'poet_key': p['poet_key'], 'poet': p['poet'], 'book': p['book'], 'section': p['section'],
            'title': p['title'], 'url': p['url'], 'status': p['status'], 'form': p['form'],
            'couplets': [r['text'] for r in rows], 'partial': partial}


_STATUS_NOTE = {
    'authentic': 'از رباعیات اصیل خیام (بر پایهٔ پژوهش سیدعلی میرافضلی)',
    'probable': 'نسبت آن به خیام محتمل است، نه قطعی (پژوهش میرافضلی)',
    'attributed': 'نسبت این شعر به این شاعر قطعی نیست (منسوب)',
}
_STATUS_SHORT = {'authentic': 'رباعی اصیل', 'probable': 'نسبت محتمل', 'attributed': 'نسبت قطعی نیست'}


def _label(p):
    parts = [p['poet'], p['book'] if p['book'] != p['poet'] else '', p['title'] or '']
    return '، '.join(x for x in parts if x)


# --------------------------------------------------------------------------- public API
def build_context(message, history=None, kind='chat', whole=False, _depth=0):
    """None when the poetry collection is not needed; else {'prompt': str, 'poems': [..]}."""
    if not available():
        return None
    msg = str(message or '')[:2000]
    nmsg = nrm(msg)
    if not nmsg:
        return None
    # «بلی بگو» / «کامل بنویس» / «معنی کن»: no poem in the message itself, so use what the conversation was about.
    if history and _depth == 0 and len(nmsg.split()) <= 8 and all(t in _FOLLOW or t in _STOP for t in nmsg.split()):
        users = [m for m in history[-8:] if isinstance(m, dict) and m.get('role') == 'user']
        for m in reversed(users[-4:]):
            ctx = build_context(str(m.get('content', '')), None, 'chat', whole=True, _depth=1)
            if ctx and ctx.get('poems'):
                ctx['prompt'] += ('\nکاربر ادامهٔ گفتگوی پیشین را می‌خواهد؛ شعری را که پیش‌تر دربارهٔ آن حرف زدید، '
                                  'در صورت خواستن، با همین متن کامل بیاور.')
                return ctx
        return None
    if _has(nmsg, ('کامل', 'تمام', 'کاملش', 'تمامش', 'یکجا', 'همه اش', 'بقیه')):
        whole = True
    poet, book = _find_poet(nmsg)
    missing = _missing_poet(nmsg)
    form = _find_form(nmsg)
    poem_word = bool(set(nmsg.split()) & {'شعر', 'اشعار', 'بیت', 'ابیات', 'مصراع', 'غزل', 'غزلیات', 'رباعی', 'رباعیات',
                                          'دوبیتی', 'قطعه', 'قصیده', 'مثنوی', 'ترانه', 'poem', 'poetry', 'ghazal', 'rubai'})
    composing = _has(nmsg, _COMPOSE)
    asking = _has(nmsg, _ASK)

    if missing and not poet:
        if not (poem_word or asking):
            return None
        return {'poems': [], 'prompt': (
            'مجموعهٔ شعر ما شعر «%s» را ندارد. هیچ بیتی از او را از حفظ نقل نکن، زیرا ممکن است نادرست باشد. '
            'اگر کاربر شعر او را خواست، صادقانه بگو که متن تأییدشدهٔ آن نزد تو نیست و، اگر لازم بود، فقط دربارهٔ سبک و زندگی او '
            'توضیح کلی بده.' % missing)}

    # Writing a NEW poem (even "in the style of") never needs the collection.
    if composing and not (asking and poet and not _has(nmsg, ('به سبک', 'در سبک', 'به شیوه', 'شبیه', 'الهام'))):
        return None
    triggered = bool(kind == 'poetry' or poem_word or book or
                     (poet and _has(nmsg, ('بخوان', 'بخون', 'بگو', 'بده', 'بیار', 'نقل', 'مصراع', 'بیت'))))
    c = _conn()
    if c is None:
        return None
    drop = set()
    for key, p in POETS.items():
        for a in p['aliases']:
            drop.update(nrm(a).split())
    for w, _, _ in BOOKS:
        drop.update(nrm(w).split())
    tokens = _content_tokens(nmsg, drop)
    # A line of verse pasted with no cue words around it: only trusted when it matches a line almost word for word.
    strict = False
    if not triggered:
        if len(tokens) < 4:
            return None
        strict = True
    prose_ask = _has(nmsg, ('حکایت', 'نثر', 'داستان', 'حکمت', 'سفرنامه', 'باب'))

    # A follow-up like «معنی‌اش را بگو» has no words of its own: the conversation already holds the poem.
    if not tokens and not poet and not book:
        return None

    found = []                                  # [(poem_id, matched couplet idx list)]
    if len(tokens) >= 2 or (tokens and not poet):
        hist_poet = None
        if not poet and history and _depth == 0:
            for m in reversed([m for m in history[-8:] if isinstance(m, dict) and m.get('role') == 'user'][-4:]):
                hp, _b = _find_poet(nrm(str(m.get('content', ''))[:2000]))
                if hp:
                    hist_poet = hp
                    break
        kw = dict(allow_prose=bool(book) or prose_ask, min_cover=0.85 if strict else 0.0)
        hits = _search_couplets(c, tokens, hist_poet, book, form, **kw) if hist_poet else []
        if hits:
            poet = hist_poet                    # the user already said whose poem this is
        else:
            hits = _search_couplets(c, tokens, poet, book, form, **kw)
        for pid, idx, _s in hits:
            found.append((pid, idx))
    if strict and not found:
        return None
    mode = 'search' if found else ''
    if not found and (poet or book):
        for pid in _random_poems(c, poet, book, form, seed=zlib.crc32(nmsg.encode('utf-8'))):
            found.append((pid, None))
        mode = 'sample' if found else ''
    if not found and tokens and not poet:
        return {'poems': [], 'prompt': (
            'در مجموعهٔ شعر ما، متنی که با این گفته بخواند پیدا نشد. هیچ شعری را از حفظ نقل نکن و به کسی نسبت نده؛ '
            'اگر مطمئن نیستی، صادقانه بگو که نیافتی و بپرس منظور کدام شعر است.')}
    if not found:
        return None

    poems, budget = [], (9000 if whole else 3600)
    if whole:
        found = found[:2]
    for rank, (pid, idx) in enumerate(found[:4]):
        # the best match comes complete (ghazals run 6-15 couplets); long works only around the matched lines
        p = _load_poem(c, pid, idx, max_couplets=(80 if whole else 40 if (rank == 0 and mode == 'search') else 14))
        if not p:
            continue
        size = sum(len(x) for x in p['couplets'])
        if poems and budget - size < 0:
            break
        budget -= size
        poems.append(p)
    if not poems:
        return None

    lines = ['«مجموعهٔ تأییدشدهٔ شعر» (متن دقیق و بررسی‌شده). قواعد:',
             '- هر بیت یا مصراعی که برای کاربر می‌آوری باید کلمه‌به‌کلمه از همین متن‌ها باشد؛ چیزی را تغییر نده و نساز.',
             '- نام شاعر و کتاب را همان‌طور که این‌جا آمده بیاور؛ اما از وب‌سایت یا منبع دیجیتال نامی نبر.',
             '- اگر هیچ‌کدام به پرسش کاربر نمی‌خورد، بگو که شعر مناسبی در مجموعه نیافتی و از حفظ نقل نکن.',
             '- اگر کاربر شعر کامل خواست و متن کامل در اختیار توست (بدون عبارت «بخشی از شعر»)، همهٔ بیت‌ها را به همان ترتیب و بدون کم‌وکاست بنویس؛ هرگز نگو که نمی‌توانی آن را نقل کنی.',
             '- اگر کاربر شاعر مشخصی خواسته، فقط شعر همان شاعر را بیاور و از شاعران دیگر یاد نکن. اگر شاعری مشخص نشده و یک مصراع در آثار چند شاعر هست، به کوتاهی بگو.',
             '- اگر نسبت شعری به شاعر قطعی نیست (ذکر شده)، آن را همراه شعر و در یک جملهٔ کوتاه به کاربر بگو.']
    if mode == 'sample':
        lines.append('- کاربر شعر مشخصی نخواسته؛ یکی از این‌ها را (نه همه) انتخاب کن.')
    for i, p in enumerate(poems, 1):
        note = _STATUS_NOTE.get(p['status'])
        lines.append('\n[%d] %s%s%s' % (i, _label(p), ' (بخشی از شعر)' if p['partial'] else '', ('  ⟵ ' + note) if note else ''))
        lines.extend(p['couplets'])
    if poet and POETS[poet]['name'] and book is None and not any(x['poet_key'] == poet for x in poems):
        pass
    return {'poems': poems, 'poet': poet, 'prompt': '\n'.join(lines)}


def add_sources(reply, ctx):
    """Append a source line for the poems the reply really quotes (checked against the text, not trusted)."""
    if not ctx or not ctx.get('poems') or not reply:
        return reply
    nr = ' %s ' % nrm(reply)
    used = []
    for p in ctx['poems']:
        for cp in p['couplets']:
            half = [nrm(h) for h in cp.split('\n') if len(nrm(h)) >= 12]
            if half and any((' %s ' % h) in nr or h in nr for h in half):
                used.append(p)
                break
    if not used:
        return reply
    want = ctx.get('poet')
    if want:
        used = [p for p in used if p['poet_key'] == want] or used
    elif len({p['poet_key'] for p in used}) > 1:
        named = {k for k, v in POETS.items() if any((' %s ' % nrm(a)) in nr for a in v['aliases'])}
        if named:
            used = [p for p in used if p['poet_key'] in named] or used
        best = {}
        for p in used:                           # one poem per poet is enough for a source line
            best.setdefault(p['poet_key'], p)
        used = list(best.values())
    out = []
    for p in used:
        s = _label(p)
        if p['status'] in _STATUS_SHORT:
            s += ' — ' + _STATUS_SHORT[p['status']]
        out.append(s)
    return reply.rstrip() + '\n\n— ' + ' | '.join(out)