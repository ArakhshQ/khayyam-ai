#!/usr/bin/env python3
"""Download the complete works of chosen poets from Ganjoor's public API.

Run it once on your own computer (Python 3.8+, no extra packages):

    python fetch_ganjoor.py --list                 # check which poets it finds
    python fetch_ganjoor.py                        # download the default poets
    python fetch_ganjoor.py --poets "خیام" "سعدی"   # only some poets
    python fetch_ganjoor.py --limit 20             # quick test: 20 poems per poet

Result: a folder `poetry_data/` with one `<poet>.jsonl` file per poet (one poem per
line, text exactly as served by Ganjoor, nothing rewritten) and a `_manifest.json`.
The script can be stopped and started again at any time; it continues where it left off.
Please be gentle with the server: it uses a few parallel requests and pauses between them.
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

API = os.environ.get('GANJOOR_API', 'https://api.ganjoor.net').rstrip('/')

# Persian name fragments used to find the poets in Ganjoor's list.
# Ganjoor addresses we expect (checked first, so a similar-sounding name cannot win).
EXPECTED_URL = {'خیام': '/khayyam', 'حافظ': '/hafez', 'سعدی': '/saadi', 'مولوی': '/moulavi',
                'بیدل': '/bidel', 'جامی': '/jami', 'ناصرخسرو': '/nasser', 'رابعه': '/rabeeh'}
DEFAULT_POETS = ['خیام', 'حافظ', 'سعدی', 'مولوی', 'بیدل', 'جامی', 'ناصرخسرو', 'رابعه']

_print_lock = threading.Lock()


def log(*a):
    with _print_lock:
        print(*a, flush=True)


def get_json(path, retries=6):
    url = API + path
    delay = 1.5
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'khayyam-ai-poetry-fetch/1.0'})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            wait = delay
            if e.code in (429, 503) and e.headers.get('Retry-After', '').isdigit():
                wait = max(wait, int(e.headers['Retry-After']))
            if attempt == retries - 1:
                raise
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
            time.sleep(delay)
        delay = min(delay * 2, 30)
    return None


def find_poets(names):
    poets = get_json('/api/ganjoor/poets') or []
    found, seen = [], set()
    for want in names:
        hits = [p for p in poets if want in (p.get('name') or '')]
        exact = [p for p in hits if (p.get('fullUrl') or '') == EXPECTED_URL.get(want)]
        if exact:
            hits = exact
        else:
            hits.sort(key=lambda p: len(p.get('name') or ''))   # otherwise the shortest name
            if len(hits) > 1:
                log(f'  note: several poets match "{want}": ' + ' | '.join(f"{p.get('name')} {p.get('fullUrl')}" for p in hits[:5]))
        for p in hits[:1]:
            if p['id'] not in seen:
                seen.add(p['id'])
                found.append(p)
        if not hits:
            log(f'  !! no poet matched "{want}"')
    return found


def slug_of(poet):
    s = (poet.get('fullUrl') or '').strip('/') or f"poet{poet['id']}"
    return ''.join(c if c.isalnum() or c in '-_' else '_' for c in s)


def walk_category(cat_id, trail, out, limit, seen_cats):
    """Collect (poem_id, poem_url, trail) for every poem below a category."""
    if limit and len(out) >= limit or cat_id in seen_cats:
        return
    seen_cats.add(cat_id)
    data = get_json(f'/api/ganjoor/cat/{cat_id}?poems=true')
    if not data:
        return
    cat = data.get('cat') or {}
    here = trail + [cat.get('title') or '']
    for p in cat.get('poems') or []:
        if p.get('id') is None:
            continue
        out.append((p['id'], (cat.get('fullUrl') or '') + '/' + (p.get('urlSlug') or ''), here))
        if limit and len(out) >= limit:
            return
    for child in cat.get('children') or []:
        if child.get('id') is not None:
            walk_category(child['id'], here, out, limit, seen_cats)
            if limit and len(out) >= limit:
                return


def fetch_poem(poem_id, poem_url):
    q = 'recitations=false&images=false&songs=false&comments=false'
    data = get_json(f'/api/ganjoor/poem/{poem_id}?{q}')
    if not data and poem_url:
        data = get_json('/api/ganjoor/poem?url=' + urllib.parse.quote(poem_url) + '&' + q)
    return data


def couplets_of(verses):
    """Group verses into couplets with Ganjoor's own coupletIndex. Text is untouched."""
    groups, order = {}, []
    for v in sorted(verses or [], key=lambda v: v.get('vOrder', 0)):
        text = (v.get('text') or '').strip()
        if not text:
            continue
        key = v.get('coupletIndex')
        if key is None:
            key = ('o', v.get('vOrder'))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(text)
    return [groups[k] for k in order]


def record(poet, trail, data):
    cat = (data.get('category') or {}).get('cat') or {}
    cat_path = [t for t in trail if t]
    return {
        'poet': poet.get('name'),
        'poet_slug': slug_of(poet),
        'poet_id': poet['id'],
        'book': (cat_path[1] if len(cat_path) > 1 else (cat.get('title') or '')),
        'path': cat_path,
        'poem_id': data.get('id'),
        'title': data.get('title'),
        'full_title': data.get('fullTitle'),
        'url': data.get('fullUrl'),
        'source': data.get('sourceName'),
        'text': data.get('plainText'),
        'couplets': couplets_of(data.get('verses')),
    }


def run_poet(poet, outdir, limit, workers, pause):
    slug = slug_of(poet)
    path = os.path.join(outdir, slug + '.jsonl')
    done = set()
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    done.add(json.loads(line)['poem_id'])
                except Exception:
                    pass
    log(f"\n== {poet['name']} ({slug}) — listing poems ...")
    root = poet.get('rootCatId')
    if root is None:
        info = get_json(f"/api/ganjoor/poet/{poet['id']}") or {}
        root = (info.get('cat') or {}).get('id')
    jobs = []
    walk_category(root, [], jobs, limit, set())
    todo = [j for j in jobs if j[0] not in done]
    log(f"   {len(jobs)} poems found, {len(done)} already saved, {len(todo)} to download")

    lock = threading.Lock()
    count = {'ok': 0, 'fail': 0}
    start = time.time()

    def work(job):
        pid, url, trail = job
        time.sleep(pause)
        data = fetch_poem(pid, url)
        if not data or not (data.get('plainText') or data.get('verses')):
            return pid, None
        return pid, record(poet, trail, data)

    with open(path, 'a', encoding='utf-8') as out, ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, j) for j in todo]
        for i, fu in enumerate(as_completed(futs), 1):
            try:
                pid, rec = fu.result()
            except Exception as e:  # network trouble: counted, retried on the next run
                count['fail'] += 1
                continue
            with lock:
                if rec is None:
                    count['fail'] += 1
                else:
                    out.write(json.dumps(rec, ensure_ascii=False) + '\n')
                    out.flush()
                    count['ok'] += 1
            if i % 100 == 0 or i == len(futs):
                rate = i / max(1, time.time() - start)
                log(f"   {i}/{len(todo)}  ({rate:.1f} poems/s, failed so far: {count['fail']})")
    total = len(done) + count['ok']
    return {'poet': poet['name'], 'slug': slug, 'file': slug + '.jsonl', 'poems': total, 'failed_this_run': count['fail']}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--poets', nargs='*', default=DEFAULT_POETS, help='Persian name fragments (default: the eight poets)')
    ap.add_argument('--out', default='poetry_data', help='output folder')
    ap.add_argument('--list', action='store_true', help='only show which poets match, then stop')
    ap.add_argument('--limit', type=int, default=0, help='max poems per poet (for a quick test)')
    ap.add_argument('--workers', type=int, default=4, help='parallel requests (keep small)')
    ap.add_argument('--pause', type=float, default=0.15, help='seconds each worker waits before a request')
    ap.add_argument('--yes', action='store_true', help='do not ask for confirmation')
    a = ap.parse_args()

    poets = find_poets(a.poets)
    log('Matched poets:')
    for p in poets:
        log(f"  id={p['id']:<5} {p.get('name')}   {p.get('fullUrl')}")
    if a.list or not poets:
        return
    if not a.yes and sys.stdin.isatty():
        if input('Download these? [y/N] ').strip().lower() not in ('y', 'yes'):
            return
    os.makedirs(a.out, exist_ok=True)
    manifest = []
    for p in poets:
        manifest.append(run_poet(p, a.out, a.limit, a.workers, a.pause))
        with open(os.path.join(a.out, '_manifest.json'), 'w', encoding='utf-8') as f:
            json.dump({'source': API, 'fetched_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'poets': manifest}, f, ensure_ascii=False, indent=2)
    log('\nDone.')
    for m in manifest:
        log(f"  {m['poet']}: {m['poems']} poems" + (f"  (missed {m['failed_this_run']}, run again to retry)" if m['failed_this_run'] else ''))


if __name__ == '__main__':
    main()
