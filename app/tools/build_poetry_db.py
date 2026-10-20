#!/usr/bin/env python3
"""Turn the downloaded Ganjoor files (poetry_data/*.jsonl) into one read-only search file: poetry.db

    python tools/build_poetry_db.py poetry_data poetry.db

Nothing is rewritten: the verses are stored exactly as Ganjoor served them. Only a separate
"search form" (normalised spelling) is built for finding them.
"""
import glob
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from poetry_search import nrm  # the same normalisation is used for questions

ATTRIB = ('منتسب', 'منسوب', 'الحاقی', 'ملحقات', 'مشکوک', 'سرایش‌های نادرست')


def form_of(title, path):
    t = (title or '') + ' ' + ' '.join(path[1:])
    for word, code in (('غزل', 'ghazal'), ('رباعی', 'rubai'), ('ترانه', 'rubai'), ('دوبیتی', 'rubai'), ('قطعه', 'qete'),
                       ('قطعات', 'qete'), ('قصیده', 'qasideh'), ('قصاید', 'qasideh'), ('ترجیع', 'tarji'), ('مفرد', 'mofrad'),
                       ('مثنوی', 'masnavi'), ('بوستان', 'masnavi'), ('هفت اورنگ', 'masnavi')):
        if word in t:
            return code
    return 'other'


def is_prose(rec):
    book = rec['book']
    if any(w in book for w in ('نوروزنامه', 'فیه ما فیه', 'مجالس', 'سفرنامه', 'رسائل', 'رسالهٔ', 'خوان الاخوان', 'وجه دین',
                                'جامع الحکمتین', 'زاد المسافرین')):
        return True
    cps = rec['couplets']
    return bool(cps) and all(len(c) == 1 for c in cps) and sum(len(c[0]) for c in cps) / len(cps) > 90


def main():
    src, out = sys.argv[1], sys.argv[2]
    recs = []
    for f in sorted(glob.glob(os.path.join(src, '*.jsonl'))):
        with open(f, encoding='utf-8') as fh:
            for line in fh:
                if line.strip():
                    recs.append(json.loads(line))

    # Khayyam: scholarly grading from the Mirafzali edition inside the data itself.
    def whole(r):
        return nrm(' '.join(' '.join(c) for c in r['couplets']))

    def first_half(r):
        return nrm(r['couplets'][0][0]) if r['couplets'] else ''
    auth, prob = set(), set()
    for r in recs:
        if r['poet_slug'] == 'khayyam' and 'اصیل' in ' '.join(r['path']):
            auth.add(whole(r)); auth.add(first_half(r))
        elif r['poet_slug'] == 'khayyam' and 'محتمل' in ' '.join(r['path']):
            prob.add(whole(r)); prob.add(first_half(r))

    if os.path.exists(out):
        os.remove(out)
    db = sqlite3.connect(out)
    db.executescript('''
        CREATE TABLE poems(id INTEGER PRIMARY KEY, poet_key TEXT, poet TEXT, book TEXT, section TEXT, title TEXT,
                           url TEXT, status TEXT, form TEXT, n INTEGER);
        CREATE TABLE couplets(id INTEGER PRIMARY KEY, poem_id INTEGER, idx INTEGER, text TEXT);
        CREATE VIRTUAL TABLE cf USING fts5(t, content='', tokenize='unicode61 remove_diacritics 0');
        CREATE INDEX couplets_poem ON couplets(poem_id, idx);
        CREATE INDEX poems_sel ON poems(poet_key, book, form, status);
    ''')
    short = {'khayyam': 'خیام', 'hafez': 'حافظ', 'saadi': 'سعدی', 'moulavi': 'مولانا', 'bidel': 'بیدل', 'jami': 'جامی',
             'naserkhosro': 'ناصرخسرو'}
    stats = {}
    cid = 0
    for r in recs:
        path = r['path']
        status = 'ok'
        if any(w in ' '.join(path) for w in ATTRIB):
            status = 'attributed'
        if r['poet_slug'] == 'khayyam' and r['book'] != 'نوروزنامه':
            w, fh = whole(r), first_half(r)
            if 'اصیل' in ' '.join(path) or w in auth or fh in auth:
                status = 'authentic'
            elif 'محتمل' in ' '.join(path) or w in prob or fh in prob:
                status = 'probable'
            else:
                status = 'attributed'
        form = 'prose' if is_prose(r) else form_of(r['title'], path)
        poet = short.get(r['poet_slug'], r['poet'])
        section = path[2] if len(path) > 2 else ''
        db.execute('INSERT INTO poems VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (r['poem_id'], r['poet_slug'], poet, r['book'], section, r['title'], r['url'], status, form, len(r['couplets'])))
        for i, c in enumerate(r['couplets']):
            text = '\n'.join(c)
            cid += 1
            nn = nrm(text)
            db.execute('INSERT INTO couplets VALUES(?,?,?,?)', (cid, r['poem_id'], i, text))
            db.execute('INSERT INTO cf(rowid, t) VALUES(?,?)', (cid, nn))
        stats[(poet, status)] = stats.get((poet, status), 0) + 1
    db.commit()
    db.execute('INSERT INTO cf(cf) VALUES("optimize")')
    db.commit()
    db.execute('VACUUM')
    db.close()
    print('poems:', len(recs), ' couplets:', cid, ' size: %.1f MB' % (os.path.getsize(out) / 1e6))
    for (p, s), n in sorted(stats.items()):
        print('  %-10s %-11s %d' % (p, s, n))


if __name__ == '__main__':
    main()
