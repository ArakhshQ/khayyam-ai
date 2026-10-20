"""Google Gemini over plain HTTPS (Interactions API): picture creation/editing and web search.

No SDK: one small client, so a change at Google is a change in one file. Responses are read
defensively (the shape of a picture block is searched for, not assumed), and every failure is
turned into a GeminiError the caller can refund and explain to the user.
"""
import base64
import os

import httpx

ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/interactions'
IMAGE_MODEL = os.getenv('GEMINI_IMAGE_MODEL', 'gemini-nano-banana-2.1')
SEARCH_MODEL = os.getenv('GEMINI_SEARCH_MODEL', 'gemini-3.8-flash')
ASPECTS = ('1:1', '3:2', '2:3', '3:4', '4:3', '4:5', '5:4', '9:16', '16:9', '21:9')
SIZES = ('1K', '2K')            # 4K is deliberately not offered (3x the price)
MAX_IMAGE_BYTES = 12 * 1024 * 1024


class GeminiError(Exception):
    def __init__(self, message, blocked=False, billed=False):
        super().__init__(message)
        self.blocked = blocked      # refused by the safety system (not a fault)
        self.billed = billed        # the call may still have been billed


def available():
    return bool(os.getenv('GEMINI_API_KEY'))


def _post(body, timeout):
    key = os.getenv('GEMINI_API_KEY')
    if not key:
        raise GeminiError('GEMINI_API_KEY is not set')
    try:
        r = httpx.post(ENDPOINT, json=body, headers={'x-goog-api-key': key, 'Content-Type': 'application/json'},
                       timeout=httpx.Timeout(timeout, connect=10))
    except httpx.TimeoutException as exc:
        raise GeminiError('timeout', billed=True) from exc
    except httpx.HTTPError as exc:
        raise GeminiError('network: %s' % type(exc).__name__) from exc
    if r.status_code >= 400:
        try:
            msg = (r.json().get('error') or {}).get('message') or ''
        except ValueError:
            msg = ''
        blocked = r.status_code == 400 and any(w in msg.lower() for w in ('safety', 'blocked', 'prohibited', 'policy'))
        raise GeminiError('http %d %s' % (r.status_code, msg[:200]), blocked=blocked)
    try:
        return r.json()
    except ValueError as exc:
        raise GeminiError('bad json') from exc


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def extract_images(data):
    """All (mime, bytes) pictures found in a response, in order."""
    out = []
    for n in _walk(data):
        blob, mime = None, None
        if n.get('type') == 'image' and isinstance(n.get('data'), str):
            blob, mime = n['data'], n.get('mime_type') or n.get('mimeType')
        elif isinstance(n.get('inline_data') or n.get('inlineData'), dict):
            d = n.get('inline_data') or n.get('inlineData')
            if isinstance(d.get('data'), str) and str(d.get('mime_type') or d.get('mimeType') or '').startswith('image/'):
                blob, mime = d['data'], d.get('mime_type') or d.get('mimeType')
        if blob:
            try:
                raw = base64.b64decode(blob, validate=False)
            except Exception:
                continue
            if raw and len(raw) <= MAX_IMAGE_BYTES:
                out.append((mime or _sniff(raw), raw))
    return out


def _sniff(b):
    return 'image/png' if b[:4] == b'\x89PNG' else 'image/jpeg'


def extract_text(data):
    parts = []
    for step in (data.get('steps') or []) if isinstance(data, dict) else []:
        if isinstance(step, dict) and step.get('type') == 'model_output':
            for blk in step.get('content') or []:
                if isinstance(blk, dict) and blk.get('type') == 'text' and blk.get('text'):
                    parts.append(blk['text'])
    return '\n'.join(parts).strip()


def generate_image(prompt, source_images=(), aspect='1:1', size='1K'):
    """Create or edit a picture. source_images: [(mime, bytes)] (edit / reference). Returns (mime, bytes)."""
    aspect = aspect if aspect in ASPECTS else '1:1'
    size = size if size in SIZES else '1K'
    parts = [{'type': 'text', 'text': prompt[:4000]}]
    for mime, raw in list(source_images)[:4]:
        parts.append({'type': 'image', 'mime_type': mime or 'image/jpeg', 'data': base64.b64encode(raw).decode()})
    body = {'model': IMAGE_MODEL, 'input': parts,
            'response_format': {'type': 'image', 'mime_type': 'image/jpeg', 'aspect_ratio': aspect, 'image_size': size}}
    data = _post(body, timeout=110)
    imgs = extract_images(data)
    if not imgs:
        said = extract_text(data)
        raise GeminiError('no image returned: %s' % said[:200], blocked=True)
    return imgs[-1]


def web_search(query):
    """Grounded answer from Google Search. Returns {'text', 'sources': [{title,url}], 'queries': []}."""
    body = {'model': SEARCH_MODEL,
            'input': ('Search the web and answer factually and briefly (under 220 words). Give the key facts, '
                      'numbers and dates, and say which date the information is from when it matters. '
                      'Answer in the same language as the question.\n\nQuestion: ' + query[:600]),
            'tools': [{'type': 'google_search'}]}
    data = _post(body, timeout=45)
    text = extract_text(data)
    sources, seen, queries = [], set(), []
    for n in _walk(data):
        if n.get('type') == 'url_citation' and isinstance(n.get('url'), str):
            u = n['url']
            if u.startswith('http') and u not in seen:
                seen.add(u); sources.append({'title': str(n.get('title') or u)[:120], 'url': u})
        if n.get('type') == 'google_search_call':
            q = (n.get('arguments') or {}).get('queries')
            if isinstance(q, list):
                queries += [str(x) for x in q]
    if not text:
        raise GeminiError('empty search answer')
    return {'text': text[:4000], 'sources': sources[:6], 'queries': queries[:4]}