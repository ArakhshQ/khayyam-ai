"""Khayyam chalkboard: server-side cleaning and checking of lesson beats.

Everything the model writes for the board passes through `clean_beat` before it is stored or sent
to a student's browser. It does three jobs:

1. Safety: only known operations and known keys survive, strings and numbers are clamped, ids are
   renamed per beat, and references to things that are not on the board are dropped.
2. Shape: a beat is {say, clear, ask, do:[ops]} with sensible size limits.
3. Truth: equations written on the board (or spoken) are recomputed here. A wrong sum never reaches
   a student; the caller asks the model to fix it, or drops the beat.
"""
import ast
import json
import re
from fractions import Fraction

COLORS = ('white', 'yellow', 'pink', 'blue', 'green', 'orange')
ICONS = ('apple', 'book', 'sun', 'moon', 'star', 'tree', 'house', 'car', 'ball', 'basketball', 'drop',
         'cloud', 'flower', 'fish', 'bird', 'heart', 'pencil', 'cup', 'coin', 'bread', 'clock', 'person',
         'bulb', 'mountain', 'check', 'cross', 'arrowr', 'ruler', 'triangle', 'square', 'circle', 'magnet', 'seed')
ZONES = ('title', 'main', 'a', 'b', 'left', 'right', 'first', 'second', 'bottom', 'full', 'center')
SIZES = ('xl', 'l', 'm', 's', 'xs')
ANIM_KINDS = ('move', 'fall', 'swing', 'orbit', 'wave', 'grow', 'fill', 'count', 'spin')
MARK_STYLES = ('underline', 'box', 'circle', 'tick', 'cross', 'arrow')

MAX_SAY = 520
MAX_OPS = 12
MAX_BEAT_JSON = 7000

# Keys each operation may carry (besides the shared ones).
SHARED = ('op', 'id', 'color', 'zone', 'box', 'at', 'width')
KEYS = {
    'text': ('t', 'size', 'align', 'bold', 'font'),
    'math': ('m', 'size', 'align'),
    'line': ('from', 'to'),
    'arrow': ('from', 'to', 'both'),
    'rect': ('x', 'y', 'w', 'h', 'fill'),
    'circle': ('cx', 'cy', 'r', 'fill'),
    'ellipse': ('cx', 'cy', 'rx', 'ry', 'r', 'fill'),
    'poly': ('pts', 'close', 'fill'),
    'path': ('d',),
    'icon': ('name', 'scale'),
    'pie': ('parts', 'fill', 'fillColor'),
    'bars': ('rows', 'fillColor'),
    'numberline': ('min', 'max', 'step', 'marks'),
    'graph': ('fn', 'xmin', 'xmax', 'ymin', 'ymax', 'grid', 'points'),
    'table': ('rows', 'header'),
    'dots': ('n', 'cols'),
    'mark': ('target', 'style'),
    'point': ('target',),
    'erase': ('target',),
    'wait': ('s',),
    'anim': ('kind', 'icon', 'shape', 'dur', 'loop', 'size', 'from', 'to', 'bodies', 'x', 'from_y', 'to_y',
             'angle', 'period', 'fontSize', 'amp', 'cycles', 'hop', 'len', 'spin', 'turns', 'fade', 'digits'),
}
ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{0,23}$')
FN_RE = re.compile(r'^[0-9a-z+\-*/^().,πxX²³ ]{1,80}$')
PATH_RE = re.compile(r'^[MmLlHhVvCcQqZz0-9eE.,\s+\-]{1,1200}$')
CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\u202a-\u202e\u2066-\u2069]')
TAG_RE = re.compile(r'<[^>]*>')


def _str(v, n):
    if not isinstance(v, str):
        return ''
    v = CTRL_RE.sub('', TAG_RE.sub('', v)).strip()
    return v[:n]


def _num(v, lo, hi, default=None):
    if isinstance(v, bool):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x or x in (float('inf'), float('-inf')):
        return default
    x = min(hi, max(lo, x))
    return int(x) if x == int(x) else round(x, 3)


def _pair(v, lo, hi):
    if isinstance(v, (list, tuple)) and len(v) >= 2:
        a, b = _num(v[0], lo, hi), _num(v[1], lo, hi)
        if a is not None and b is not None:
            return [a, b]
    return None


def _box(v):
    if isinstance(v, (list, tuple)) and len(v) == 4:
        out = [_num(x, 0, 100) for x in v]
        if None not in out and out[2] > 1 and out[3] > 1:
            return out
    return None


# ───────── arithmetic checking ─────────
_FA = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩٫٬', '01234567890123456789.,')


def _normalize_math(s):
    s = s.translate(_FA)
    s = re.sub(r'(?<=\d),(?=\d{3}(?!\d))', '', s)
    s = s.replace(',', ';')
    s = re.sub(r'\\(?:d?frac)\s*\{([^{}]*)\}\s*\{([^{}]*)\}', r'((\1)/(\2))', s)
    s = s.replace('\\times', '*').replace('\\cdot', '*').replace('\\div', '/').replace('\\left', '').replace('\\right', '')
    s = s.replace('×', '*').replace('·', '*').replace('÷', '/').replace('−', '-').replace('–', '-').replace('٪', '%')
    s = s.replace('{', '(').replace('}', ')')
    return s


def _safe_eval(expr):
    """Exact arithmetic on + - * / ^ and parentheses. Returns a Fraction or None."""
    try:
        tree = ast.parse(expr.replace('^', '**'), mode='eval')
    except (SyntaxError, ValueError):
        return None

    def ev(n, depth=0):
        if depth > 20:
            raise ValueError
        if isinstance(n, ast.Expression):
            return ev(n.body, depth + 1)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return Fraction(str(n.value))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            v = ev(n.operand, depth + 1)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left, depth + 1), ev(n.right, depth + 1)
            if isinstance(n.op, ast.Add):
                return a + b
            if isinstance(n.op, ast.Sub):
                return a - b
            if isinstance(n.op, ast.Mult):
                return a * b
            if isinstance(n.op, ast.Div):
                if b == 0:
                    raise ValueError
                return a / b
            if isinstance(n.op, ast.Pow):
                if b.denominator != 1 or abs(b) > 8 or abs(a) > 10 ** 6:
                    raise ValueError
                return a ** int(b)
        raise ValueError

    try:
        return ev(tree)
    except (ValueError, ZeroDivisionError, OverflowError, RecursionError, TypeError):
        return None


_EQ_RE = re.compile(r'(?<![\w.])((?:[\d(][\d\s+\-*/().^]*)(?:=\s*[\d(][\d\s+\-*/().^]*)+)(?![\w.])')


def check_equations(text):
    """Find chains like '12 + 7 = 19' in free text and return a list of wrong ones (strings)."""
    bad = []
    s = _normalize_math(text or '')
    for m in _EQ_RE.finditer(s):
        chain = m.group(1)
        sides = [p.strip() for p in chain.split('=')]
        if len(sides) < 2 or any(not p for p in sides):
            continue
        if len(chain) > 120:
            continue
        vals = []
        for p in sides:
            if not re.search(r'\d', p):
                vals = []
                break
            vals.append(_safe_eval(p))
        if not vals or any(v is None for v in vals):
            continue
        # a plain list like "1 = 1" is fine; only compare when at least one side has an operator
        if not any(re.search(r'[+\-*/^]', p.lstrip('-')) for p in sides):
            continue
        first = vals[0]
        for v in vals[1:]:
            if abs(v - first) > Fraction(1, 10 ** 6) * max(1, abs(first)):
                bad.append(re.sub(r'\s+', ' ', chain).strip())
                break
    return bad


def equation_problems(beat):
    texts = [beat.get('say', '')]
    for op in beat.get('do', []):
        if op.get('op') == 'text':
            texts.append(op.get('t', ''))
        elif op.get('op') == 'math':
            texts.append(op.get('m', ''))
        elif op.get('op') == 'table':
            for row in op.get('rows') or []:
                texts.append(' '.join(str(c) for c in row))
    out = []
    for t in texts:
        out.extend(check_equations(t))
    return out


# ───────── cleaning ─────────
def _by_local_name(name, known):
    """The model often points at 'p1' from a later beat; ids are stored as 'k<beat>_p1'. Use the newest match."""
    best, best_i = None, -1
    for k in known:
        m = re.match(r'^k(\d+)_(.+)$', k)
        if m and m.group(2) == name and int(m.group(1)) > best_i:
            best, best_i = k, int(m.group(1))
    return best


def _clean_op(raw, mapping, known):
    if not isinstance(raw, dict):
        return None
    op = _str(raw.get('op'), 14).lower()
    if op == 'pause':
        op = 'wait'
    if op not in KEYS:
        return None
    out = {'op': op}

    # id (renamed per beat so a reused name can never collide)
    oid = raw.get('id')
    if isinstance(oid, str) and ID_RE.match(oid) and op not in ('erase', 'wait', 'point'):
        new = mapping['prefix'] + oid
        mapping['own'][oid] = new
        out['id'] = new

    col = raw.get('color')
    if col in COLORS:
        out['color'] = col
    if op in ('pie', 'bars') and raw.get('fillColor') in COLORS:
        out['fillColor'] = raw['fillColor']
    z = raw.get('zone')
    if z in ZONES:
        out['zone'] = z
    b = _box(raw.get('box'))
    if b:
        out['box'] = b
    at = _pair(raw.get('at'), 0, 100)
    if at:
        out['at'] = at
    w = _num(raw.get('width'), 1.5, 8)
    if w is not None and op in ('line', 'arrow', 'rect', 'circle', 'ellipse', 'poly', 'path', 'icon'):
        out['width'] = w

    g = lambda k: raw.get(k)
    if op == 'text':
        t = _str(g('t') or g('text'), 240)
        if not t:
            return None
        out['t'] = t
        if g('size') in SIZES:
            out['size'] = g('size')
        elif isinstance(g('size'), (int, float)) and not isinstance(g('size'), bool):
            out['size'] = _num(g('size'), 14, 90)
        if g('align') in ('center', 'left', 'right'):
            out['align'] = g('align')
        if g('font') == 'display':
            out['font'] = 'display'
        if g('bold') is True:
            out['bold'] = True
    elif op == 'math':
        m = _str(g('m'), 140)
        if not m or '<' in m and '>' in m and False:
            return None
        out['m'] = m
        if g('size') in SIZES:
            out['size'] = g('size')
        if g('align') in ('center', 'left', 'right'):
            out['align'] = g('align')
    elif op in ('line', 'arrow'):
        a, c = _pair(g('from'), -3, 103), _pair(g('to'), -3, 103)
        if not a or not c:
            return None
        out['from'], out['to'] = a, c
        if op == 'arrow' and g('both') is True:
            out['both'] = True
    elif op == 'rect':
        for k, lo, hi in (('x', 0, 100), ('y', 0, 100), ('w', 1, 200), ('h', 1, 200)):
            v = _num(g(k), lo, hi)
            if v is not None:
                out[k] = v
        if g('fill') in (True, 1):
            out['fill'] = True
    elif op in ('circle', 'ellipse'):
        for k, lo, hi in (('cx', 0, 100), ('cy', 0, 100), ('r', 1, 150), ('rx', 1, 150), ('ry', 1, 150)):
            v = _num(g(k), lo, hi)
            if v is not None:
                out[k] = v
        if g('fill') in (True, 1):
            out['fill'] = True
    elif op == 'poly':
        pts = [p for p in (_pair(q, -3, 103) for q in (g('pts') if isinstance(g('pts'), list) else [])[:40]) if p]
        if len(pts) < 2:
            return None
        out['pts'] = pts
        if g('close') is False:
            out['close'] = False
        if g('fill') in (True, 1):
            out['fill'] = True
    elif op == 'path':
        d = g('d')
        if not isinstance(d, str) or not PATH_RE.match(d.strip()):
            return None
        out['d'] = d.strip()
    elif op == 'icon':
        n = g('name')
        if n not in ICONS:
            return None
        out['name'] = n
        s = _num(g('scale'), .2, 1.5)
        if s is not None:
            out['scale'] = s
    elif op == 'pie':
        parts = int(_num(g('parts'), 1, 24, 4))
        out['parts'] = parts
        out['fill'] = int(_num(g('fill'), 0, parts, 0))
    elif op == 'bars':
        rows = []
        for r in (g('rows') if isinstance(g('rows'), list) else [])[:8]:
            if isinstance(r, dict):
                p = int(_num(r.get('parts'), 1, 24, 4))
                rows.append({'parts': p, 'fill': int(_num(r.get('fill'), 0, p, 0))})
        if not rows:
            return None
        out['rows'] = rows
    elif op == 'numberline':
        mn, mx = _num(g('min'), -10000, 10000, 0), _num(g('max'), -10000, 10000, 10)
        if mx <= mn:
            return None
        st = _num(g('step'), 1e-6, 10000, 1)
        if (mx - mn) / st > 60:
            st = (mx - mn) / 20
        out.update({'min': mn, 'max': mx, 'step': st})
        marks = []
        for mk in (g('marks') if isinstance(g('marks'), list) else [])[:12]:
            if isinstance(mk, dict) and _num(mk.get('at'), -10000, 10000) is not None:
                item = {'at': _num(mk.get('at'), -10000, 10000)}
                lab = _str(mk.get('label'), 30)
                if lab:
                    item['label'] = lab
                if mk.get('color') in COLORS:
                    item['color'] = mk['color']
                marks.append(item)
        if marks:
            out['marks'] = marks
    elif op == 'graph':
        fn = g('fn')
        if fn is not None:
            if not isinstance(fn, str) or not FN_RE.match(fn):
                return None
            out['fn'] = fn.strip()
        xmin, xmax = _num(g('xmin'), -1000, 1000, -5), _num(g('xmax'), -1000, 1000, 5)
        ymin, ymax = _num(g('ymin'), -1000, 1000, -5), _num(g('ymax'), -1000, 1000, 5)
        if xmax <= xmin or ymax <= ymin:
            return None
        out.update({'xmin': xmin, 'xmax': xmax, 'ymin': ymin, 'ymax': ymax})
        if g('grid') is False:
            out['grid'] = False
        pts = [p for p in (_pair(q, -1000, 1000) for q in (g('points') if isinstance(g('points'), list) else [])[:30]) if p]
        if pts:
            out['points'] = pts
        if 'fn' not in out and not pts:
            return None
    elif op == 'table':
        rows = []
        for r in (g('rows') if isinstance(g('rows'), list) else [])[:8]:
            row = r if isinstance(r, list) else [r]
            rows.append([_str(str(c) if isinstance(c, (int, float)) and not isinstance(c, bool) else c, 40) for c in row[:6]])
        if not rows:
            return None
        out['rows'] = rows
    elif op == 'dots':
        out['n'] = int(_num(g('n'), 1, 40, 6))
        c = _num(g('cols'), 1, 12)
        if c is not None:
            out['cols'] = int(c)
    elif op in ('mark', 'point', 'erase'):
        tg = g('target')
        if op == 'erase' and (tg in (None, 'all')):
            out['target'] = 'all'
        else:
            lst = tg if isinstance(tg, list) else [tg]
            res = []
            for t in lst[:8]:
                if not isinstance(t, str):
                    continue
                t = mapping['own'].get(t) or (t if t in known else _by_local_name(t, known))
                if t:
                    res.append(t)
            if not res:
                if op != 'erase' and 'at' in out:
                    pass
                else:
                    return None
            else:
                out['target'] = res[0] if (len(res) == 1 and op != 'erase') else res
        if op == 'mark':
            out['style'] = g('style') if g('style') in MARK_STYLES else 'underline'
    elif op == 'wait':
        out['s'] = _num(g('s'), .2, 6, 1)
    elif op == 'anim':
        k = g('kind')
        if k not in ANIM_KINDS:
            return None
        out['kind'] = k
        if g('icon') in ICONS:
            out['icon'] = g('icon')
        if g('shape') in ('cup', 'bar', 'pie'):
            out['shape'] = g('shape')
        for key, lo, hi in (('dur', .5, 20), ('size', 6, 50), ('x', 0, 100),
                            ('from_y', 0, 90), ('to_y', 0, 90), ('angle', 0, 90), ('period', .5, 30),
                            ('fontSize', 20, 200), ('amp', 0, 60), ('cycles', .5, 8), ('hop', 0, 50), ('len', 0, 100),
                            ('spin', -20, 20), ('turns', -20, 20)):
            v = _num(g(key), lo, hi)
            if v is not None:
                out[key] = v
        if k == 'move':
            for key in ('from', 'to'):
                v = _pair(g(key), 0, 100)
                if v:
                    out[key] = v
        elif k in ('count', 'fill'):
            for key in ('from', 'to'):
                v = _num(g(key), -1e6, 1e6)
                if v is not None:
                    out[key] = v
        if g('loop') is False:
            out['loop'] = False
        bodies = []
        for bd in (g('bodies') if isinstance(g('bodies'), list) else [])[:4]:
            if isinstance(bd, dict):
                item = {'r': _num(bd.get('r'), 8, 48, 30), 'period': _num(bd.get('period'), 1, 30, 4),
                        'size': _num(bd.get('size'), 4, 30, 12)}
                if bd.get('icon') in ICONS:
                    item['icon'] = bd['icon']
                bodies.append(item)
        if bodies:
            out['bodies'] = bodies
    return out


def clean_beat(raw, prefix='b', known_ids=()):
    """Returns (beat or None, ids_defined_by_this_beat).  `known_ids` = ids still valid from earlier beats."""
    if not isinstance(raw, dict):
        return None, set()
    say = _str(raw.get('say'), MAX_SAY)
    if len(say) < 2:
        return None, set()
    mapping = {'prefix': prefix, 'own': {}}
    ops = []
    for r in (raw.get('do') if isinstance(raw.get('do'), list) else [])[:MAX_OPS]:
        # point/mark/erase may refer to items defined earlier in the same beat, so clean in order
        c = _clean_op(r, mapping, known_ids)
        if c:
            ops.append(c)
    beat = {'say': say, 'do': ops}
    if raw.get('clear') is True:
        beat['clear'] = True
    if raw.get('ask') is True:
        beat['ask'] = True
    if len(json.dumps(beat, ensure_ascii=False)) > MAX_BEAT_JSON:
        beat['do'] = ops[:6]
    return beat, set(mapping['own'].values())


def live_ids(beats):
    """Ids that are still on the board after the last clear (or erase-all) in `beats`."""
    ids = set()
    for b in beats:
        if b.get('clear'):
            ids = set()
        for op in b.get('do', []):
            if op.get('op') == 'erase':
                tg = op.get('target')
                if tg == 'all':
                    ids = set()
                else:
                    for t in (tg if isinstance(tg, list) else [tg]):
                        ids.discard(t)
            elif op.get('id'):
                ids.add(op['id'])
    return ids


def describe_op(op):
    k = op.get('op')
    d = _describe(op)
    return ('%s = %s' % (op['id'], d)) if op.get('id') and d else d


def _describe(op):
    k = op.get('op')
    if k == 'text':
        return 'نوشته: ' + op.get('t', '')
    if k == 'math':
        return 'فرمول: ' + op.get('m', '')
    if k == 'pie':
        return 'دایره %s/%s' % (op.get('fill'), op.get('parts'))
    if k == 'bars':
        return 'نوار کسر'
    if k == 'graph':
        return 'نمودار ' + (op.get('fn') or '')
    if k == 'numberline':
        return 'خط عددی %s تا %s' % (op.get('min'), op.get('max'))
    if k == 'table':
        return 'جدول %d ردیف' % len(op.get('rows') or [])
    if k == 'icon':
        return 'نقش ' + op.get('name', '')
    if k == 'anim':
        return 'انیمیشن ' + op.get('kind', '')
    return k or ''


def board_digest(beats, limit=14):
    """A short description of what is on the board now, for the model."""
    on = []
    for b in beats:
        if b.get('clear'):
            on = []
        for op in b.get('do', []):
            if op.get('op') in ('erase', 'wait', 'point', 'mark'):
                if op.get('op') == 'erase' and op.get('target') == 'all':
                    on = []
                continue
            on.append(describe_op(op))
    on = [x for x in on if x]
    return on[-limit:]
