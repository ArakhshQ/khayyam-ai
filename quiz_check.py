"""Checking a generated quiz before a student sees it.

A model writes the quiz, and models make arithmetic mistakes: the marked answer can be wrong, or no option can be right.
Two layers, cheapest first:
  1. MATH, exactly and for free: a question can carry "check", the expression whose value is the answer (for example
     "(5x+6)-(2x+1)"). It is computed with sympy and compared with every option. Exactly one option must be equal, and it
     must be the marked one. Otherwise the question is dropped.
  2. EVERYTHING ELSE: an independent solver (a second model call, done by the caller) answers the questions it could not
     check without seeing the key; a question it disputes is dropped.

Safety: the expressions come from a model, so they are parsed only after a strict character whitelist (digits, a few
one-letter variables, operators, brackets). No names, no underscores, no dots outside numbers: nothing that can run code.
Huge powers are refused so a crafted expression cannot hang the server.
"""
import random
import re

try:
    import sympy
    from sympy.parsing.sympy_parser import (parse_expr, standard_transformations,
                                            implicit_multiplication_application, convert_xor)
    _TRANSFORMS = standard_transformations + (implicit_multiplication_application, convert_xor)
except Exception:          # sympy missing: the math layer is skipped, nothing else breaks
    sympy = None

_DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
_ALLOWED = re.compile(r'^[0-9abcnxyz\s+\-*/^().√]+$')
_VARS = 'abcnxyz'


def to_expr(s):
    """A short maths expression or a number -> a sympy expression, or None when it is not plain maths."""
    if sympy is None or not isinstance(s, str):
        return None
    t = s.translate(_DIGITS).strip().lower()
    for a, b in (('×', '*'), ('·', '*'), ('÷', '/'), ('−', '-'), ('–', '-'), ('٫', '.'), ('²', '^2'), ('³', '^3'), ('‏', ''), ('‎', ''),
                 ('⁦', ''), ('⁩', '')):
        t = t.replace(a, b)
    if '=' in t:                         # "x = 3" -> 3; a real equation is not a value
        lhs, rhs = t.split('=', 1)
        if not re.fullmatch(r'\s*[a-z]\s*', lhs) or '=' in rhs:
            return None
        t = rhs
    t = t.strip().rstrip('.')
    if not t or len(t) > 80 or not _ALLOWED.match(t):
        return None
    if t.count('^') + t.count('**') > 2 or re.search(r'(\^|\*\*)\s*\(?\s*-?\d{3,}', t):
        return None
    t = re.sub(r'√\s*(\d+(?:\.\d+)?|[a-z])', r'sqrt(\1)', t).replace('√', 'sqrt')
    loc = {v: sympy.Symbol(v) for v in _VARS}
    loc['sqrt'] = sympy.sqrt
    try:
        e = parse_expr(t, local_dict=loc, transformations=_TRANSFORMS, evaluate=False)      # nothing is computed yet
    except Exception:
        return None
    if not isinstance(e, sympy.Expr):
        return None
    for node in sympy.preorder_traversal(e):         # a power of a power, or a huge power, could take forever to compute
        if isinstance(node, sympy.Pow):
            ex = node.exp
            if ex.has(sympy.Pow) or (ex.is_number and abs(complex(ex.evalf())) > 64):
                return None
    return e


def same(a, b):
    """Are two expressions equal? Checked at several random points (fast, no symbolic simplify that could hang)."""
    try:
        syms = sorted((a.free_symbols | b.free_symbols), key=str)
        rnd = random.Random(7)
        for _ in range(5):
            sub = {s: sympy.Rational(rnd.randint(-97, 97), rnd.randint(7, 31)) for s in syms}
            va, vb = complex(a.evalf(subs=sub)), complex(b.evalf(subs=sub))
            if abs(va - vb) > 1e-7 * max(1.0, abs(va), abs(vb)):
                return False
        return True
    except Exception:
        return False


def check_question(q):
    """-> 'ok' (the key is proved right), 'bad' (wrong key, no right option, or two right options), 'unknown' (not plain maths)."""
    c = to_expr(q.get('check') or '')
    if c is None:
        return 'unknown'
    vals = [to_expr(o) for o in q.get('options') or []]
    if sum(v is not None for v in vals) < 3:            # the options are words: maths cannot decide
        return 'unknown'
    hits = [i for i, v in enumerate(vals) if v is not None and same(v, c)]
    if len(hits) == 1 and hits[0] == q.get('answer'):
        return 'ok'
    return 'bad'


SOLVER_SYSTEM = ("You check a school quiz. Solve every question yourself, carefully, step by step in your head, WITHOUT trusting anyone. "
                 'Return one JSON object only: {"answers": [{"i": 0, "pick": 2}, ...]} where "i" is the question number given and "pick" is the index 0-3 '
                 "of the ONE correct option, or -1 if no option is correct or more than one is correct or the question is unclear.")


def solver_prompt(questions):
    lines = []
    for i, q in questions:
        lines.append('%d) %s\n   options: %s' % (i, q['q'], ' | '.join('%d: %s' % (k, o) for k, o in enumerate(q['options']))))
    return 'Questions:\n' + '\n'.join(lines)


def solver_picks(data):
    """The solver's JSON -> {question index: pick}."""
    out = {}
    if isinstance(data, dict) and isinstance(data.get('answers'), list):
        for a in data['answers']:
            try:
                out[int(a.get('i'))] = int(a.get('pick'))
            except (TypeError, ValueError, AttributeError):
                continue
    return out
