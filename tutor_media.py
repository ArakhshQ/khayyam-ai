"""Pictures and document pages that a student brings to the tutor class.

* A photo is re-encoded as a modest JPEG. A PDF is turned into page pictures (the first MAX_PAGES pages).
* Every picture can have "regions": named rectangles (a heading, a paragraph, a drawn object, a handwritten answer ...).
  The teacher points at and circles regions by id, so it never has to guess pixel positions.
  - PDF pages with a text layer get their regions straight from the text blocks (exact, free).
  - Photos, scans and drawings are described by a vision model (see LOOK_SYSTEM), which reads positions off a grid drawn on the picture.
Boxes are always [left, top, width, height] in thousandths (0-1000) of the picture, so they do not depend on screen size.
"""
import json
import re

import pymupdf

import dari_text

MAX_UPLOAD = 12 * 1024 * 1024
MAX_PAGES = 20
MAX_MATERIALS = 30
MAX_REGIONS = 18
PAGE_WIDTH = 1100

LOOK_SYSTEM = r"""You are the eyes of a teacher. A student shows a picture (a photo, a textbook page, a worksheet, handwriting, a diagram, a drawing). The picture has a thin yellow grid drawn on it: lines every 10% and numbers 0-1000 along the top and left edges (thousandths of the width and of the height). Read positions from that grid.

Return one JSON object only:
{"kind": "photo|textbook|worksheet|handwriting|diagram|math|map|other",
 "summary": "2-3 plain sentences in Afghan Dari: what the picture is and what in it matters for teaching",
 "text": "all readable text in reading order, copied exactly, in its own language, at most 1800 characters; empty if there is none",
 "regions": [ {"id": "r1", "label": "1-4 words in Dari that name this part", "what": "one short Dari sentence about it", "text": "the exact words or numbers in it when it is text, else empty", "box": [left, top, width, height]} ]}

box = thousandths of the picture: left and top of the part, then its width and height. Make every box tight around the part, never the whole picture unless the part is the whole picture.
Give 6-14 regions that cover what a teacher would point at: each object, label, heading, paragraph or line of text, formula, table row, part of a diagram, each exercise, and each handwritten line or student answer. Ids are r1, r2, r3 ... in reading order (for right-to-left text, from the right).
Never invent text you cannot read; write «؟» for an unreadable word. Afghan words: «ابتدایی» for basic or elementary, never «پایه»."""

FIND_NOTE = ('\n\nThe teacher also needs to point at: "%s". Put it in the JSON as "found": {"id": "%s", "label": "...", "what": "...", "text": "...", "box": [l, t, w, h]}. '
             'If it is not visible, set "found": null.')


# ───────── what kind of file is it ─────────
def kind_of(data):
    if not data:
        return None
    if data[:5] == b'%PDF-':
        return 'pdf'
    if data[:3] == b'\xff\xd8\xff' or data[:8] == b'\x89PNG\r\n\x1a\n' or (data[:4] == b'RIFF' and data[8:12] == b'WEBP'):
        return 'image'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image'
    return None


def prepare_image(data, max_side=1600):
    """-> (jpeg bytes, width, height)"""
    jpeg, _mime = dari_text.shrink_image(data, max_side)
    w, h = dari_text.image_size(jpeg)
    return jpeg, w, h


# ───────── PDF pages ─────────
def _norm_box(r, pw, ph):
    x0, y0, x1, y1 = max(0, r[0]), max(0, r[1]), min(pw, r[2]), min(ph, r[3])
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    return [int(x0 / pw * 1000), int(y0 / ph * 1000), max(8, int((x1 - x0) / pw * 1000)), max(8, int((y1 - y0) / ph * 1000))]


def _label(text, n=4):
    words = re.sub(r'\s+', ' ', text or '').strip().split(' ')
    return ' '.join(words[:n])[:40] or '؟'


def page_regions(page):
    """Regions from the text blocks and pictures of one PDF page. [] when the page has no text layer."""
    if page.rotation:
        return []
    rect = page.rect
    pw, ph = float(rect.width), float(rect.height)
    if pw < 1 or ph < 1:
        return []
    out = []
    try:
        blocks = page.get_text('blocks')
    except Exception:
        return []
    for b in blocks:
        x0, y0, x1, y1, txt, _no, typ = b[:7]
        box = _norm_box((x0, y0, x1, y1), pw, ph)
        if not box:
            continue
        txt = re.sub(r'\s+', ' ', txt or '').strip()
        if typ == 1:
            out.append({'label': 'تصویر', 'kind': 'figure', 'text': '', 'box': box})
        elif len(txt) >= 3:
            out.append({'label': _label(txt), 'kind': 'text', 'text': txt[:300], 'box': box})
    if len(out) > MAX_REGIONS:                            # keep the biggest ones
        out = sorted(out, key=lambda r: -(r['box'][2] * r['box'][3]))[:MAX_REGIONS]
    out.sort(key=lambda r: (round(r['box'][1] / 40), r['box'][0]))
    for i, r in enumerate(out):
        r['id'] = 'r%d' % (i + 1)
    return out


def render_pdf(data, max_pages=MAX_PAGES, width=PAGE_WIDTH):
    """-> {'pages': total, 'items': [{'jpeg', 'w', 'h', 'page', 'text', 'regions'}]}.  Raises ValueError for unreadable files."""
    try:
        doc = pymupdf.open(stream=data, filetype='pdf')
    except Exception as e:
        raise ValueError('unreadable pdf') from e
    try:
        if doc.needs_pass or doc.page_count < 1:
            raise ValueError('pdf needs a password or is empty')
        total = doc.page_count
        items = []
        for i in range(min(total, max_pages)):
            page = doc[i]
            k = width / max(1.0, float(page.rect.width))
            if page.rect.height * k > 1700:
                k = 1700.0 / max(1.0, float(page.rect.height))
            pix = page.get_pixmap(matrix=pymupdf.Matrix(k, k), alpha=False)
            regions = page_regions(page)
            try:
                text = re.sub(r'[ \t]+', ' ', page.get_text('text') or '').strip()[:2500]
            except Exception:
                text = ''
            items.append({'jpeg': pix.tobytes('jpeg', jpg_quality=84), 'w': pix.width, 'h': pix.height, 'page': i + 1,
                          'text': text, 'regions': regions})
        return {'pages': total, 'items': items}
    finally:
        doc.close()


# ───────── the grid the vision model reads positions from ─────────
def grid_overlay(jpeg):
    """The picture with a thin yellow grid (every 10%) and 0-1000 numbers on the top and left edges."""
    w, h = dari_text.image_size(jpeg)
    doc = pymupdf.open()
    try:
        page = doc.new_page(width=w, height=h)
        page.insert_image(page.rect, stream=jpeg)
        fs = max(9.0, min(w, h) / 62.0)
        col = (1.0, 0.86, 0.0)
        for i in range(1, 10):
            x, y = w * i / 10.0, h * i / 10.0
            page.draw_line((x, 0), (x, h), color=col, width=max(.6, w / 1400.0), stroke_opacity=.55)
            page.draw_line((0, y), (w, y), color=col, width=max(.6, w / 1400.0), stroke_opacity=.55)
            page.insert_text((x + 2, fs + 1), str(i * 100), fontsize=fs, color=col)
            page.insert_text((2, y - 2), str(i * 100), fontsize=fs, color=col)
        pix = page.get_pixmap(dpi=72)
        return pix.tobytes('jpeg', jpg_quality=88)
    finally:
        doc.close()


# ───────── regions ─────────
def clean_box(v):
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return None
    try:
        x, y, w, h = [float(t) for t in v]
    except (TypeError, ValueError):
        return None
    if any(t != t for t in (x, y, w, h)):
        return None
    x, y = min(992, max(0, x)), min(992, max(0, y))
    w, h = min(1000 - x, max(8, w)), min(1000 - y, max(8, h))
    if w < 4 or h < 4:
        return None
    return [int(x), int(y), int(w), int(h)]


def _s(v, n):
    return re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f<>]', '', v).strip()[:n] if isinstance(v, str) else ''


def clean_regions(raw, taken=()):
    """List of region dicts from a model -> clean list with unique ids r1, r2 ... (ids in `taken` are skipped)."""
    out, used = [], set(taken)
    for r in (raw if isinstance(raw, list) else [])[:MAX_REGIONS + 4]:
        if not isinstance(r, dict):
            continue
        box = clean_box(r.get('box'))
        if not box:
            continue
        rid = r.get('id') if isinstance(r.get('id'), str) and re.match(r'^r\d{1,3}$', r.get('id')) and r.get('id') not in used else None
        if not rid:
            n = 1
            while 'r%d' % n in used:
                n += 1
            rid = 'r%d' % n
        used.add(rid)
        out.append({'id': rid, 'label': _s(r.get('label'), 40) or '؟', 'what': _s(r.get('what'), 120),
                    'text': _s(r.get('text'), 300), 'box': box, 'kind': _s(r.get('kind'), 12) or ''})
        if len(out) >= MAX_REGIONS:
            break
    return out


def parse_look(reply):
    """Model reply (JSON text) -> dict(kind, summary, text, regions, found) or None."""
    if not isinstance(reply, str):
        return None
    m = re.search(r'\{.*\}', reply, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    regions = clean_regions(d.get('regions'))
    found = None
    if isinstance(d.get('found'), dict):
        f = clean_regions([d['found']], taken={r['id'] for r in regions})
        found = f[0] if f else None
    return {'kind': _s(d.get('kind'), 14) or 'other', 'summary': _s(d.get('summary'), 500), 'text': _s(d.get('text'), 1800),
            'regions': regions, 'found': found}


def regions_for_prompt(meta, limit=16):
    """Compact text for the live teacher: what is in the picture and which region ids it can point at."""
    lines = []
    if meta.get('summary'):
        lines.append('what it shows: ' + meta['summary'])
    regs = meta.get('regions') or []
    if regs:
        lines.append('regions you can point at (id: label - what; text):')
        for r in regs[:MAX_REGIONS]:
            t = (' ; "%s"' % r['text'][:90]) if r.get('text') else ''
            lines.append('  %s: %s%s%s' % (r['id'], r.get('label', ''), (' - ' + r['what']) if r.get('what') else '', t))
    if meta.get('text'):
        lines.append('text in it: ' + meta['text'][:700])
    return '\n'.join(lines)
