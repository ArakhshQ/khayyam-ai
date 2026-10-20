"""Khayyam's own Dari/Arabic-script renderer.

Image models cannot spell Dari. So a picture is created WITHOUT letters and the text is drawn here,
with real fonts and a real shaping engine (MuPDF/HarfBuzz): letters join, vowel marks sit right,
right-to-left order is correct, English and digits inside Dari lines stay in the right place.

Also builds the documents (PDF, DOCX, XLSX) the chat can hand to the user, from one JSON-like spec.
Everything here is pure functions on bytes/dicts: no network, no database.
"""
import html as _html
import io
import os
import re

import pymupdf

pymupdf.TOOLS.mupdf_display_errors(False)

_HERE = os.path.dirname(os.path.abspath(__file__))
_NEEDED = ('vazirmatn-400.ttf', 'vazirmatn-700.ttf', 'amiri-400.ttf', 'amiri-700.ttf')


def _build_fonts(target):
    """The site already ships Vazirmatn and Amiri as web fonts (static/fonts/*.woff2). The renderer needs
    plain TTF files, so they are converted once, on first use, into a cache folder. No manual copying."""
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer
    os.makedirs(target, exist_ok=True)
    src = os.path.join(_HERE, 'static', 'fonts')
    for w in (400, 700):
        f = TTFont(os.path.join(src, 'vazirmatn-var.woff2'))
        f.flavor = None
        g = instancer.instantiateVariableFont(f, {'wght': w})
        g.save(os.path.join(target, 'vazirmatn-%d.ttf' % w))
    for w in (400, 700):
        f = TTFont(os.path.join(src, 'amiri-arabic-%d-normal.woff2' % w))
        f.flavor = None
        f.save(os.path.join(target, 'amiri-%d.ttf' % w))


def _find_font_dir():
    own = os.path.join(_HERE, 'assets', 'fonts')          # preferred: TTFs shipped with the code
    if all(os.path.exists(os.path.join(own, n)) for n in _NEEDED):
        return own
    import tempfile
    cache = os.path.join(tempfile.gettempdir(), 'khayyam_fonts_v1')
    if not all(os.path.exists(os.path.join(cache, n)) for n in _NEEDED):
        _build_fonts(cache)
    return cache


FONT_DIR = _find_font_dir()
ACCENT = '#0b6b4f'          # emerald, same as the site
GOLD = '#b8893b'

_ARABIC = re.compile(r'[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]')
_LETTER = re.compile(r'[^\W\d_]', re.U)
_FA_DIGITS = str.maketrans('0123456789', '۰۱۲۳۴۵۶۷۸۹')

BASE_CSS = """
@font-face {font-family: vazir; src: url(vazirmatn-400.ttf);}
@font-face {font-family: vazir; font-weight: bold; src: url(vazirmatn-700.ttf);}
@font-face {font-family: amiri; src: url(amiri-400.ttf);}
@font-face {font-family: amiri; font-weight: bold; src: url(amiri-700.ttf);}
body {font-family: vazir;}
"""

_archive = None


def _arch():
    global _archive
    if _archive is None:
        _archive = pymupdf.Archive(FONT_DIR)
    return _archive


_fonts = {}


def _font(name, bold):
    key = (name, bool(bold))
    if key not in _fonts:
        file = '%s-%d.ttf' % ('vazirmatn' if name == 'vazir' else 'amiri', 700 if bold else 400)
        _fonts[key] = pymupdf.Font(fontfile=os.path.join(FONT_DIR, file))
    return _fonts[key]


def is_rtl(text):
    letters = _LETTER.findall(text or '')
    if not letters:
        return False
    return sum(1 for c in letters if _ARABIC.match(c)) / len(letters) >= 0.3


def esc(text):
    return _html.escape(str(text or ''), quote=False)


def _inline(text):
    """Escape, then allow **bold** and *italic* only."""
    t = esc(text)
    t = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
    t = re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])', r'<i>\1</i>', t)
    return t.replace('\n', '<br>')


def _color(value, default):
    v = str(value or '').strip()
    return v if re.fullmatch(r'#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}', v) else default


def _rgb(hex_color):
    h = hex_color.lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


# =============================================================================================
# TEXT ON PICTURES
# =============================================================================================
# box = (x, y, w, h) as fractions of the picture; size = font size as a fraction of the short side.
PRESETS = {
    'title_bottom': {'box': (.06, .70, .88, .24), 'size': .085, 'font': 'vazir', 'bold': True, 'align': 'center', 'band': .45,
                     'hint': 'the lower third of the picture'},
    'title_center': {'box': (.08, .33, .84, .34), 'size': .10, 'font': 'vazir', 'bold': True, 'align': 'center', 'band': 0,
                     'hint': 'the middle band of the picture'},
    'title_top': {'box': (.06, .05, .88, .24), 'size': .085, 'font': 'vazir', 'bold': True, 'align': 'center', 'band': .0,
                  'hint': 'the upper third of the picture'},
    'poem': {'box': (.10, .22, .80, .56), 'size': .055, 'font': 'amiri', 'bold': False, 'align': 'center', 'band': .30,
             'hint': 'the central area of the picture'},
    'caption': {'box': (.04, .86, .92, .11), 'size': .042, 'font': 'vazir', 'bold': False, 'align': 'center', 'band': .55,
                'hint': 'the bottom edge of the picture'},
}
DEFAULT_PRESET = 'title_bottom'


def composition_hint(layers):
    """Sentence for the image prompt: keep those areas calm and never draw letters."""
    areas = []
    for lay in layers or []:
        p = PRESETS.get(lay.get('preset') or DEFAULT_PRESET) or PRESETS[DEFAULT_PRESET]
        if lay.get('box') is None and p['hint'] not in areas:
            areas.append(p['hint'])
    base = ('Do not draw any letters, words, numbers, captions, logos or signs anywhere in the picture; '
            'all text will be added afterwards.')
    if areas:
        base += ' Keep ' + ' and '.join(areas) + ' simple and uncluttered, with calm tones, so text can be placed there.'
    return base


def _mean_luma(page, rect):
    """Average brightness (0-255) of an area of the page, from a tiny render."""
    try:
        pm = page.get_pixmap(clip=rect, dpi=12)
        s, n = pm.samples, pm.n
        if not s:
            return 128
        step = n * 3
        vals = [(s[i] * 299 + s[i + 1] * 587 + s[i + 2] * 114) / 1000 for i in range(0, len(s) - 2, step)]
        return sum(vals) / max(1, len(vals))
    except Exception:
        return 128


def _layer_defaults(lay):
    p = PRESETS.get(lay.get('preset') or DEFAULT_PRESET) or PRESETS[DEFAULT_PRESET]
    out = dict(p)
    for k in ('font', 'bold', 'align', 'size', 'band'):
        if lay.get(k) is not None:
            out[k] = lay[k]
    if lay.get('box'):
        out['box'] = tuple(lay['box'])
    out['font'] = out['font'] if out['font'] in ('vazir', 'amiri') else 'vazir'
    out['align'] = out['align'] if out['align'] in ('center', 'right', 'left') else 'center'
    out['size'] = min(.3, max(.015, float(out['size'])))
    out['band'] = min(.9, max(0, float(out['band'] or 0)))
    out['color'] = lay.get('color') or 'auto'
    return out


def _clamp_box(box):
    x, y, w, h = [float(v) for v in box]
    x = min(max(x, 0), .95); y = min(max(y, 0), .95)
    return x, y, min(max(w, .05), 1 - x), min(max(h, .03), 1 - y)


def image_size(image_bytes):
    d = pymupdf.open(stream=image_bytes, filetype=_sniff(image_bytes))
    try:
        r = d[0].rect
        return int(r.width), int(r.height)
    finally:
        d.close()


def _sniff(b):
    if b[:3] == b'\xff\xd8\xff':
        return 'jpeg'
    if b[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if b[:4] == b'RIFF' and b[8:12] == b'WEBP':
        return 'webp'
    return 'png'


def overlay_text(image_bytes, layers, fmt='jpeg'):
    """Draw Dari/Arabic-script (or any) text layers on a picture. Returns (bytes, mime).

    Each layer: {text, preset|box, font, size, color('auto'|'#rrggbb'), bold, align, band, shadow}
    'auto' picks white or dark ink from the real brightness behind the text and adds a soft
    band when the picture is busy, so the words are always readable.
    """
    kind = _sniff(image_bytes)
    src = pymupdf.open(stream=image_bytes, filetype=kind)
    img_page = src[0]
    # Bring the picture into a fresh page whose size is exactly the picture, 1 point = 1 pixel.
    probe = pymupdf.Pixmap(image_bytes) if kind != 'webp' else None
    W, H = (probe.width, probe.height) if probe else (int(img_page.rect.width), int(img_page.rect.height))
    src.close()
    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)
    page.insert_image(page.rect, stream=image_bytes)
    short = min(W, H)

    for lay in layers or []:
        text = str(lay.get('text') or '').strip()
        if not text:
            continue
        d = _layer_defaults(lay)
        x, y, w, h = _clamp_box(d['box'])
        rect = pymupdf.Rect(x * W, y * H, (x + w) * W, (y + h) * H)
        luma = _mean_luma(page, rect)
        color = d['color']
        if color == 'auto' or not re.fullmatch(r'#[0-9a-fA-F]{3,6}', color):
            ink = '#ffffff' if luma < 150 else '#14231c'
        else:
            ink = _color(color, '#ffffff')
        band = d['band']
        if band and (luma > 70 or color == 'auto'):
            fill = (0, 0, 0) if ink.lower() in ('#fff', '#ffffff') else (1, 1, 1)
            page.draw_rect(rect, color=None, fill=fill, fill_opacity=band, radius=0.06)
        pad = rect.width * 0.035
        inner = pymupdf.Rect(rect.x0 + pad, rect.y0 + pad * .6, rect.x1 - pad, rect.y1 - pad * .6)
        paras = [p for p in re.split(r'\n', text)]
        size_pt = d['size'] * short
        try:                                   # make the longest line fit the width when it reasonably can
            fnt = _font(d['font'], d['bold'])
            widest = .82 * max(fnt.text_length(re.sub(r'[\u064B-\u065F\u0670]', '', p), fontsize=size_pt) for p in paras)
            avail = (rect.width - 2 * rect.width * 0.035) * 0.97
            if widest > avail and len(paras) <= 6:
                size_pt = max(size_pt * avail / widest, d['size'] * short * .62)
        except Exception:
            pass
        rtl = is_rtl(text)
        body = ''.join('<p style="margin:0">%s</p>' % (_inline(p) if p.strip() else '&nbsp;') for p in paras)
        css = BASE_CSS + 'body{font-family:%s;font-size:%.1fpt;line-height:1.55;%s direction:%s;text-align:%s;color:%s}' % (
            d['font'], size_pt, 'font-weight:bold;' if d['bold'] else '',
            'rtl' if rtl else 'ltr', d['align'], ink)
        # First on a scratch page: how much room is left, so the block can be centred vertically.
        scratch = pymupdf.open(); sp = scratch.new_page(width=W, height=H)
        spare, scale = sp.insert_htmlbox(inner, body, css=css, archive=_arch(), scale_low=0)
        scratch.close()
        if spare is None or spare < 0:
            spare = 0
        box = pymupdf.Rect(inner.x0, inner.y0 + spare / 2, inner.x1, inner.y1 - spare / 2)
        if lay.get('shadow', ink.lower() in ('#fff', '#ffffff') and not band):
            sh = pymupdf.Rect(box.x0 + short * .004, box.y0 + short * .004, box.x1 + short * .004, box.y1 + short * .004)
            shadow_css = css.replace('color:' + ink, 'color:#000000')
            page.insert_htmlbox(sh, body, css=shadow_css, archive=_arch(), scale_low=0, opacity=.55)
        page.insert_htmlbox(box, body, css=css, archive=_arch(), scale_low=0)

    pix = page.get_pixmap(dpi=72)
    doc.close()
    if fmt == 'png':
        return pix.tobytes('png'), 'image/png'
    return pix.tobytes('jpeg', jpg_quality=93), 'image/jpeg'


# =============================================================================================
# DOCUMENTS
# =============================================================================================
MAX_BLOCKS = 400
MAX_CELLS = 6000


def _clean_spec(spec):
    spec = spec if isinstance(spec, dict) else {}
    blocks = spec.get('blocks') if isinstance(spec.get('blocks'), list) else []
    out = []
    cells = 0
    for b in blocks[:MAX_BLOCKS]:
        if not isinstance(b, dict):
            continue
        t = str(b.get('type') or 'paragraph')
        if t == 'table':
            hdr = [str(c)[:300] for c in (b.get('headers') or [])][:30]
            rows = []
            for r in (b.get('rows') or [])[:500]:
                if isinstance(r, list):
                    rows.append([str(c)[:600] for c in r][:30]); cells += len(r)
                if cells > MAX_CELLS:
                    break
            out.append({'type': 'table', 'headers': hdr, 'rows': rows, 'caption': str(b.get('caption') or '')[:200]})
        elif t in ('list', 'numbered'):
            out.append({'type': 'list', 'ordered': t == 'numbered' or bool(b.get('ordered')),
                        'items': [str(i)[:1500] for i in (b.get('items') or [])][:200]})
        elif t == 'heading':
            out.append({'type': 'heading', 'level': min(3, max(1, int(b.get('level') or 1))), 'text': str(b.get('text') or '')[:400]})
        elif t == 'poem':
            lines = b.get('lines')
            if not isinstance(lines, list):
                lines = str(b.get('text') or '').split('\n')
            out.append({'type': 'poem', 'lines': [str(l)[:400] for l in lines][:200], 'poet': str(b.get('poet') or '')[:120]})
        elif t == 'quote':
            out.append({'type': 'quote', 'text': str(b.get('text') or '')[:3000], 'author': str(b.get('author') or '')[:120]})
        elif t in ('divider', 'pagebreak'):
            out.append({'type': t})
        else:
            out.append({'type': 'paragraph', 'text': str(b.get('text') or '')[:6000]})
    return {'title': str(spec.get('title') or '')[:200], 'subtitle': str(spec.get('subtitle') or '')[:300],
            'author': str(spec.get('author') or '')[:120], 'blocks': out,
            'rtl': spec.get('rtl'), 'accent': _color(spec.get('accent'), ACCENT)}


def _doc_rtl(spec):
    if spec.get('rtl') in (True, False):
        return bool(spec['rtl'])
    sample = ' '.join([spec['title'], spec['subtitle']] + [
        b.get('text', '') or ' '.join(b.get('items', []) or b.get('lines', []) or b.get('headers', []))
        for b in spec['blocks'][:20]])
    return is_rtl(sample)


def _digits(s, rtl):
    return s.translate(_FA_DIGITS) if rtl else s


def _dir(text, doc_rtl):
    """Per-block direction: a Latin paragraph inside a Dari document reads left to right."""
    r = is_rtl(text) if re.search(r'[^\W\d_]', text or '', re.U) else doc_rtl
    return ' style="direction:%s;text-align:%s"' % (('rtl', 'right') if r else ('ltr', 'left'))


def _pdf_html(spec, rtl):
    ac = spec['accent']
    parts = []
    if spec['title']:
        parts.append('<h1 class="doc-title">%s</h1>' % _inline(spec['title']))
    if spec['subtitle']:
        parts.append('<p class="doc-sub">%s</p>' % _inline(spec['subtitle']))
    if spec['title'] or spec['subtitle']:
        parts.append('<div class="rule"></div>')
    for b in spec['blocks']:
        t = b['type']
        if t == 'heading':
            parts.append('<h%d%s>%s</h%d>' % (b['level'] + 1, _dir(b['text'], rtl), _inline(b['text']), b['level'] + 1))
        elif t == 'paragraph':
            for para in re.split(r'\n{2,}', b['text']):
                if para.strip():
                    parts.append('<p%s>%s</p>' % (_dir(para, rtl), _inline(para.strip())))
        elif t == 'list':
            tag = 'ol' if b['ordered'] else 'ul'
            parts.append('<%s>%s</%s>' % (tag, ''.join('<li%s>%s</li>' % (_dir(i, rtl), _inline(i)) for i in b['items']), tag))
        elif t == 'table':
            cols = max([len(b['headers'])] + [len(r) for r in b['rows']] or [1])
            order = (lambda cells: cells[::-1]) if rtl else (lambda cells: cells)   # first column on the right in Dari
            head = ''.join('<th%s>%s</th>' % (_dir(c, rtl), _inline(c)) for c in order(b['headers'] + [''] * (cols - len(b['headers'])))) if b['headers'] else ''
            body = ''.join('<tr>%s</tr>' % ''.join('<td%s>%s</td>' % (_dir(c, rtl), _inline(c)) for c in order(r + [''] * (cols - len(r)))) for r in b['rows'])
            cap = '<p class="cap">%s</p>' % _inline(b['caption']) if b['caption'] else ''
            parts.append('%s<table>%s%s</table>' % (cap, ('<tr>%s</tr>' % head) if head else '', body))
        elif t == 'poem':
            lines = '<br>'.join(_inline(l) for l in b['lines'])
            poet = '<p class="poet">%s</p>' % _inline(b['poet']) if b['poet'] else ''
            parts.append('<div class="poem">%s</div>%s' % (lines, poet))
        elif t == 'quote':
            who = '<p class="qa">%s</p>' % _inline(b['author']) if b['author'] else ''
            parts.append('<blockquote>%s</blockquote>%s' % (_inline(b['text']), who))
        elif t == 'divider':
            parts.append('<div class="rule"></div>')
    d = 'rtl' if rtl else 'ltr'
    al = 'right' if rtl else 'left'
    css = BASE_CSS + f"""
body {{font-size: 11.5pt; line-height: 1.7; color: #1e2a25; direction: {d}; text-align: {al};}}
h1.doc-title {{font-size: 26pt; color: {ac}; margin: 0 0 2pt 0; line-height: 1.35;}}
p.doc-sub {{font-size: 13pt; color: #5d6b64; margin: 0 0 6pt 0;}}
div.rule {{border-bottom: 1.5pt solid {GOLD}; margin: 6pt 0 12pt 0; height: 0;}}
h2 {{font-size: 18pt; color: {ac}; margin: 16pt 0 4pt 0; line-height: 1.4;}}
h3 {{font-size: 14.5pt; color: #25443a; margin: 12pt 0 3pt 0; line-height: 1.4;}}
h4 {{font-size: 12.5pt; color: #25443a; margin: 10pt 0 2pt 0;}}
p {{margin: 0 0 7pt 0;}}
li {{margin: 0 0 3pt 0;}}
table {{width: 100%; border-collapse: collapse; margin: 6pt 0 10pt 0;}}
th {{background-color: {ac}; color: #ffffff; padding: 5pt 6pt; border: 0.7pt solid {ac}; font-weight: bold; text-align: {al};}}
td {{padding: 4.5pt 6pt; border: 0.7pt solid #b9c4be; text-align: {al};}}
p.cap {{color: #5d6b64; font-size: 10pt; margin: 8pt 0 2pt 0;}}
div.poem {{font-family: amiri; font-size: 16pt; line-height: 2.0; text-align: center; margin: 10pt 20pt 2pt 20pt; direction: rtl; color: #1b3d31;}}
p.poet {{text-align: center; color: {GOLD}; font-size: 10.5pt; margin: 0 0 10pt 0;}}
blockquote {{margin: 8pt 16pt 2pt 16pt; padding: 0 10pt; color: #33423b; font-style: italic;}}
p.qa {{color: #5d6b64; font-size: 10pt; margin: 0 16pt 10pt 16pt;}}
"""
    return ''.join(parts), css


def build_pdf(spec):
    spec = _clean_spec(spec)
    rtl = _doc_rtl(spec)
    html_body, css = _pdf_html(spec, rtl)
    story = pymupdf.Story(html=html_body, user_css=css, archive=_arch())
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    media = pymupdf.paper_rect('a4')
    where = media + (54, 60, -54, -64)
    more, pages = 1, 0
    while more and pages < 120:
        dev = writer.begin_page(media)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        pages += 1
    writer.close()
    out = pymupdf.open(stream=buf.getvalue(), filetype='pdf')
    total = out.page_count
    for i, pg in enumerate(out, 1):          # page number at the foot of every page
        num = _digits('%d / %d' % (i, total), rtl) if total > 1 else ''
        if num:
            r = pymupdf.Rect(0, pg.rect.height - 40, pg.rect.width, pg.rect.height - 22)
            pg.insert_htmlbox(r, '<p style="margin:0;text-align:center;direction:ltr">%s</p>' % num,
                              css=BASE_CSS + 'body{font-size:9pt;color:#7a8780}', archive=_arch())
    if spec['title']:
        out.set_metadata({'title': spec['title'][:200], 'author': spec['author'] or 'Khayyam', 'producer': 'Khayyam AI'})
    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return data


# -------------------------------------------------------------------------------- DOCX
def build_docx(spec):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor, Cm

    spec = _clean_spec(spec)
    rtl = _doc_rtl(spec)
    ac = RGBColor(*[int(c * 255) for c in _rgb(spec['accent'])])
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.2)
    sec.top_margin = sec.bottom_margin = Cm(2.2)
    if rtl:
        sectpr = sec._sectPr
        el = OxmlElement('w:bidi'); sectpr.append(el)

    def style_font(style, size, bold=False, color=None):
        style.font.name = 'Arial'; style.font.size = Pt(size); style.font.bold = bold
        rpr = style.element.get_or_add_rPr()
        rf = rpr.find(qn('w:rFonts'))
        if rf is None:
            rf = OxmlElement('w:rFonts'); rpr.append(rf)
        for a in ('w:ascii', 'w:hAnsi', 'w:cs', 'w:eastAsia'):
            rf.set(qn(a), 'Arial')
        if color is not None:
            style.font.color.rgb = color
        szcs = rpr.find(qn('w:szCs'))
        if szcs is None:
            szcs = OxmlElement('w:szCs'); rpr.append(szcs)
        szcs.set(qn('w:val'), str(int(size * 2)))
        if bold:
            if rpr.find(qn('w:bCs')) is None:
                rpr.append(OxmlElement('w:bCs'))

    style_font(doc.styles['Normal'], 11.5)
    for name, size in (('Heading 1', 20), ('Heading 2', 16), ('Heading 3', 13.5), ('Title', 26)):
        style_font(doc.styles[name], size, True, ac)

    def para_rtl(p, align=None):
        ppr = p._p.get_or_add_pPr()
        if rtl:
            ppr.append(OxmlElement('w:bidi'))
        if align == 'center':
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        else:
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT if rtl else WD_ALIGN_PARAGRAPH.LEFT

    def add_runs(p, text, size=None, color=None, bold_all=False, font=None):
        tokens = re.split(r'(\*\*.+?\*\*)', str(text))
        for tk in tokens:
            if not tk:
                continue
            b = tk.startswith('**') and tk.endswith('**') and len(tk) > 4
            run = p.add_run(tk[2:-2] if b else tk)
            run.bold = bool(b or bold_all)
            if size:
                run.font.size = Pt(size)
            if color is not None:
                run.font.color.rgb = color
            rpr = run._r.get_or_add_rPr()
            if rtl:
                rpr.append(OxmlElement('w:rtl'))
                if run.bold:
                    rpr.append(OxmlElement('w:bCs'))
            if font:
                run.font.name = font
                rf = rpr.find(qn('w:rFonts'))
                for a in ('w:cs',):
                    rf.set(qn(a), font)

    if spec['title']:
        p = doc.add_paragraph(style='Title'); para_rtl(p); add_runs(p, spec['title'], 26, ac, True)
    if spec['subtitle']:
        p = doc.add_paragraph(); para_rtl(p); add_runs(p, spec['subtitle'], 13, RGBColor(0x5d, 0x6b, 0x64))
    for b in spec['blocks']:
        t = b['type']
        if t == 'heading':
            p = doc.add_paragraph(style='Heading %d' % b['level']); para_rtl(p); add_runs(p, b['text'], None, ac, True)
        elif t == 'paragraph':
            for para in re.split(r'\n{2,}', b['text']):
                if para.strip():
                    p = doc.add_paragraph(); para_rtl(p); add_runs(p, para.strip())
        elif t == 'list':
            for n, item in enumerate(b['items'], 1):
                p = doc.add_paragraph(); para_rtl(p)
                p.paragraph_format.space_after = Pt(3)
                marker = _digits('%d. ' % n, rtl) if b['ordered'] else '•  '
                add_runs(p, marker + item)
        elif t == 'table':
            cols = max([len(b['headers'])] + [len(r) for r in b['rows']] or [1])
            tbl = doc.add_table(rows=0, cols=cols)
            tbl.style = 'Table Grid'
            if rtl:
                tblpr = tbl._tbl.tblPr; tblpr.append(OxmlElement('w:bidiVisual'))
            if b['headers']:
                cells = tbl.add_row().cells
                for i in range(cols):
                    cp = cells[i].paragraphs[0]; para_rtl(cp)
                    add_runs(cp, (b['headers'] + [''] * cols)[i], 11, RGBColor(255, 255, 255), True)
                    tcpr = cells[i]._tc.get_or_add_tcPr(); shd = OxmlElement('w:shd')
                    shd.set(qn('w:val'), 'clear'); shd.set(qn('w:color'), 'auto'); shd.set(qn('w:fill'), spec['accent'].lstrip('#')[:6].ljust(6, '0'))
                    tcpr.append(shd)
            for r in b['rows']:
                cells = tbl.add_row().cells
                for i in range(cols):
                    cp = cells[i].paragraphs[0]; para_rtl(cp); add_runs(cp, (r + [''] * cols)[i], 11)
            doc.add_paragraph()
        elif t == 'poem':
            for line in b['lines']:
                p = doc.add_paragraph(); para_rtl(p, 'center'); p.paragraph_format.space_after = Pt(2)
                if rtl:
                    p._p.get_or_add_pPr()
                add_runs(p, line, 15, RGBColor(0x1b, 0x3d, 0x31))
            if b['poet']:
                p = doc.add_paragraph(); para_rtl(p, 'center'); add_runs(p, b['poet'], 10.5, RGBColor(0xb8, 0x89, 0x3b))
        elif t == 'quote':
            p = doc.add_paragraph(); para_rtl(p); p.paragraph_format.left_indent = Cm(1); p.paragraph_format.right_indent = Cm(1)
            add_runs(p, b['text'], 11.5, RGBColor(0x33, 0x42, 0x3b))
            for r in p.runs:
                r.italic = True
            if b['author']:
                p = doc.add_paragraph(); para_rtl(p); add_runs(p, b['author'], 10, RGBColor(0x5d, 0x6b, 0x64))
        elif t == 'pagebreak':
            doc.add_page_break()
    doc.core_properties.title = spec['title'][:200]
    doc.core_properties.author = spec['author'] or 'Khayyam'
    buf = io.BytesIO(); doc.save(buf)
    return buf.getvalue()


# -------------------------------------------------------------------------------- XLSX
_SAFE_FORMULA = re.compile(r'^=\s*(?:(?:SUM|AVERAGE|MIN|MAX|COUNT|COUNTA|ROUND|ABS|IF|AND|OR|PRODUCT|MEDIAN)\(|[A-Z]{1,3}\d{1,5}|\d)[A-Z0-9_+\-*/^().,:<>=\s"%$]*$')


class _Txt(str):
    """A string that must stay literal text in the sheet (never a formula)."""


def _cell(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return v
    s = str(v)[:2000]
    if s.startswith('=') and _SAFE_FORMULA.match(s) and 'HYPERLINK' not in s.upper():
        return s
    if s[:1] in ('=', '+', '-', '@') and not re.fullmatch(r'[-+]?\d+([.,]\d+)?%?', s):
        return _Txt(s)
    m = re.fullmatch(r'[-+]?\d+(\.\d+)?', s.strip())
    if m:
        try:
            f = float(s)
            return int(f) if f.is_integer() and '.' not in s else f
        except ValueError:
            pass
    return s


def build_xlsx(spec):
    """spec: {title, rtl, sheets:[{name, headers[], rows[][], widths[], charts:[{type,title,x_col,y_cols}]}]}"""
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    spec = spec if isinstance(spec, dict) else {}
    sheets = spec.get('sheets') if isinstance(spec.get('sheets'), list) and spec.get('sheets') else [
        {'name': 'Sheet1', 'headers': spec.get('headers') or [], 'rows': spec.get('rows') or []}]
    sample = ' '.join(str(c) for s in sheets[:3] for c in (s.get('headers') or [])[:10])
    rtl = spec['rtl'] if spec.get('rtl') in (True, False) else is_rtl(sample)
    ac = _color(spec.get('accent'), ACCENT).lstrip('#')
    ac = ac if len(ac) == 6 else ACCENT.lstrip('#')
    wb = Workbook()
    wb.remove(wb.active)
    thin = Side(style='thin', color='B9C4BE')
    cells_total = 0
    used = set()
    for si, sh in enumerate(sheets[:8]):
        name = re.sub(r'[\[\]\*\?/\\:]', ' ', str(sh.get('name') or 'Sheet%d' % (si + 1)))[:31].strip() or 'Sheet%d' % (si + 1)
        while name.lower() in used:
            name = (name[:28] + '_%d' % si)
        used.add(name.lower())
        ws = wb.create_sheet(name)
        ws.sheet_view.rightToLeft = bool(rtl)
        headers = [str(h)[:200] for h in (sh.get('headers') or [])][:60]
        rows = [r for r in (sh.get('rows') or []) if isinstance(r, list)][:2000]
        r0 = 1
        if headers:
            ws.append(headers)
            for c in ws[1]:
                c.font = Font(bold=True, color='FFFFFF', name='Arial')
                c.fill = PatternFill('solid', fgColor=ac)
                c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True, readingOrder=2 if rtl else 1)
            ws.freeze_panes = 'A2'
            r0 = 2
        for r in rows:
            cells_total += len(r)
            if cells_total > MAX_CELLS * 2:
                break
            vals = [_cell(v) for v in r[:60]]
            ws.append(vals)
            for c, v in zip(ws[ws.max_row], vals):
                if isinstance(v, _Txt):
                    c.data_type = 's'
        ncols = max([len(headers)] + [len(r) for r in rows] or [1])
        for row in ws.iter_rows(min_row=r0):
            for c in row:
                c.border = Border(left=thin, right=thin, top=thin, bottom=thin)
                c.alignment = Alignment(horizontal='right' if rtl else 'left', vertical='top', wrap_text=True,
                                        readingOrder=2 if rtl else 1)
                c.font = Font(name='Arial')
        widths = sh.get('widths') if isinstance(sh.get('widths'), list) else []
        for i in range(1, ncols + 1):
            if i - 1 < len(widths) and isinstance(widths[i - 1], (int, float)):
                w = widths[i - 1]
            else:
                w = max([len(str(headers[i - 1])) if i - 1 < len(headers) else 0] +
                        [len(str(r[i - 1])) for r in rows[:200] if i - 1 < len(r)] or [8]) + 3
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(9, w))
        n = len(rows)
        for ch in (sh.get('charts') or [])[:3]:
            if not (isinstance(ch, dict) and headers and n):
                continue
            kind = str(ch.get('type') or 'bar')
            try:
                x_col = int(ch.get('x_col') or 1)
                y_cols = [int(c) for c in (ch.get('y_cols') or [2])][:6]
            except (TypeError, ValueError):
                continue
            if not (1 <= x_col <= ncols) or any(not (1 <= c <= ncols) for c in y_cols):
                continue
            chart = {'line': LineChart, 'pie': PieChart}.get(kind, BarChart)()
            chart.title = str(ch.get('title') or '')[:80] or None
            chart.height, chart.width = 8.5, 17
            for yc in y_cols:
                chart.add_data(Reference(ws, min_col=yc, min_row=1, max_row=1 + n), titles_from_data=True)
                if kind == 'pie':
                    break
            chart.set_categories(Reference(ws, min_col=x_col, min_row=2, max_row=1 + n))
            ws.add_chart(chart, '%s%d' % (get_column_letter(ncols + 2), 2))
    if not wb.sheetnames:
        wb.create_sheet('Sheet1')
    wb.properties.title = str(spec.get('title') or '')[:200]
    wb.properties.creator = 'Khayyam'
    buf = io.BytesIO(); wb.save(buf)
    return buf.getvalue()


MIME = {'pdf': 'application/pdf',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}


def build_document(fmt, spec):
    """Returns (bytes, mime, extension)."""
    fmt = (fmt or 'pdf').lower()
    if fmt == 'pdf':
        return build_pdf(spec), MIME['pdf'], 'pdf'
    if fmt in ('docx', 'word'):
        return build_docx(spec), MIME['docx'], 'docx'
    if fmt in ('xlsx', 'excel'):
        return build_xlsx(spec), MIME['xlsx'], 'xlsx'
    raise ValueError('unsupported format')