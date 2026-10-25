"""The chat's tools: pictures (create / edit / text on top), web search, documents.

Each tool: check the user's monthly allowance -> do the work -> refund the allowance if it failed.
The model only ever sees short JSON results; files go to the user through [[media:ID]] markers
that the server appends to the answer (so a model can never fake or mangle a link).
"""
import json
import os
import re
import threading

import dari_text
import gemini
import media
import metering

MAX_LAYERS = 6
# A picture takes 10-40 s and holds a web-server thread the whole time. Cap how many run at once per
# worker so a burst of picture requests can never starve ordinary chat.
_SLOTS = threading.BoundedSemaphore(int(os.getenv('TOOL_CONCURRENCY', '3')))
_SLOT_KINDS = ('image', 'image_hd', 'search')


def specs(hints, has_attached_image, has_picture_in_chat=False):
    """OpenAI 'tools' list for this turn. Tools that need Google are left out while no Google key is set,
    so the model never promises something that cannot happen."""
    out = []
    g = gemini.available()
    layers_schema = {
        'type': 'array', 'maxItems': MAX_LAYERS,
        'description': 'Text to draw on the picture with Khayyam\'s own Dari-capable renderer. Always use this '
                       'for any words on the picture, never put words in the image prompt.',
        'items': {'type': 'object', 'properties': {
            'text': {'type': 'string', 'description': 'Exact text, correct spelling. Use \\n for line breaks.'},
            'preset': {'type': 'string', 'enum': list(dari_text.PRESETS), 'description': 'Where and how big.'},
            'color': {'type': 'string', 'description': "'auto' (default, picks readable ink) or a hex like #ffffff"},
            'font': {'type': 'string', 'enum': ['vazir', 'amiri'], 'description': 'amiri = classical, for poetry'},
            'band': {'type': 'number', 'description': '0-0.9 darkness of the plate behind the text (default by preset)'},
        }, 'required': ['text']}}
    if 'image' in hints and g:
        out.append({'type': 'function', 'function': {
            'name': 'generate_image',
            'description': 'Create a new picture from a description (poster, illustration, photo, logo, card...).',
            'parameters': {'type': 'object', 'properties': {
                'prompt': {'type': 'string', 'description': 'Detailed English description: subject, style, lighting, '
                                                           'composition, colors. No words/letters to be drawn.'},
                'aspect_ratio': {'type': 'string', 'enum': list(gemini.ASPECTS), 'description': 'Default 1:1'},
                'quality': {'type': 'string', 'enum': ['standard', 'high'], 'description': 'high = 2K, only when asked'},
                'text_layers': layers_schema}, 'required': ['prompt']}}})
        out.append({'type': 'function', 'function': {
            'name': 'edit_image',
            'description': 'Change a picture (the one the user attached, or one made earlier in this chat): '
                           'background, colors, add or remove objects, style, enhancement.',
            'parameters': {'type': 'object', 'properties': {
                'instruction': {'type': 'string', 'description': 'What to change, in English, precise. '
                                                                 'Say what must stay unchanged.'},
                'source': {'type': 'string', 'description': "'attached' (picture in the user's message), 'last' "
                                                            "(most recent picture of this chat) or a media id"},
                'text_layers': layers_schema}, 'required': ['instruction', 'source']}}})
    if 'image' in hints and (g or has_attached_image or has_picture_in_chat):
        out.append({'type': 'function', 'function': {
            'name': 'add_text_to_image',
            'description': 'Put (or change) text on a picture without redrawing it. Free and instant. Use it to '
                           'fix wording on a picture made earlier: send ALL the layers wanted in the end.',
            'parameters': {'type': 'object', 'properties': {
                'source': {'type': 'string', 'description': "'attached', 'last' or a media id"},
                'text_layers': layers_schema}, 'required': ['source', 'text_layers']}}})
    if 'search' in hints and g:
        out.append({'type': 'function', 'function': {
            'name': 'web_search',
            'description': 'Search the web for current information (news, prices, results, recent facts).',
            'parameters': {'type': 'object', 'properties': {
                'query': {'type': 'string', 'description': 'Short search query, in the best language for the topic'}},
                'required': ['query']}}})
    if 'doc' in hints:
        out.append({'type': 'function', 'function': {
            'name': 'create_document',
            'description': 'Create a downloadable file. PDF or DOCX use spec.blocks; XLSX uses spec.sheets.',
            'parameters': {'type': 'object', 'properties': {
                'format': {'type': 'string', 'enum': ['pdf', 'docx', 'xlsx']},
                'filename': {'type': 'string', 'description': 'Short file name without extension'},
                'spec': {'type': 'object', 'description': (
                    'PDF/DOCX: {title, subtitle?, blocks:[{type:"heading",level:1-3,text} | {type:"paragraph",text} | '
                    '{type:"list",ordered?:bool,items:[]} | {type:"table",headers:[],rows:[[]]} | {type:"poem",lines:[],poet?} | '
                    '{type:"quote",text,author?} | {type:"divider"} | {type:"pagebreak"}]}. Text supports **bold**. '
                    'XLSX: {sheets:[{name,headers:[],rows:[[]],charts?:[{type:"bar"|"line"|"pie",title,x_col:1,y_cols:[2]}]}]}; '
                    'numbers as numbers; formulas like =SUM(B2:B9) allowed. Write complete real content.')}},
                'required': ['format', 'spec']}}})
    return out


# ------------------------------------------------------------------------------------------ helpers
def _layers(raw):
    out = []
    for lay in (raw if isinstance(raw, list) else [])[:MAX_LAYERS]:
        if not isinstance(lay, dict) or not str(lay.get('text') or '').strip():
            continue
        item = {'text': str(lay['text'])[:500]}
        if lay.get('preset') in dari_text.PRESETS:
            item['preset'] = lay['preset']
        if isinstance(lay.get('color'), str) and (lay['color'] == 'auto' or re.fullmatch(r'#[0-9a-fA-F]{3,6}', lay['color'])):
            item['color'] = lay['color']
        if lay.get('font') in ('vazir', 'amiri'):
            item['font'] = lay['font']
        try:
            if lay.get('band') is not None:
                item['band'] = min(.9, max(0, float(lay['band'])))
        except (TypeError, ValueError):
            pass
        out.append(item)
    return out


class Env:
    """What a tool needs to know about this request."""
    def __init__(self, db, uid, plan, conv_id=None, attached=None, logger=None):
        self.db, self.uid, self.plan, self.conv_id = db, uid, plan, conv_id
        self.attached = attached              # (mime, bytes) or None
        self.owner = metering.owner_key(uid)
        self.log = logger
        self.media_ids = []                   # shown to the user, in order
        self.created = []                     # every file stored this turn (incl. hidden clean bases)
        self.sources = []                     # web sources to append
        self.cost_micro = None                # set by a tool when the real cost is known


def _quota_error(reason, kind):
    names = {'image': 'images', 'image_hd': 'HD images', 'search': 'web searches', 'doc': 'documents'}
    return {'ok': False, 'error': 'quota',
            'message': 'The monthly allowance for %s on this plan is used up (%s). Tell the user kindly and '
                       'mention they can upgrade their plan or wait for next month.' % (names.get(kind, kind),
                                                                                       'usage limit' if reason in ('count', 'hd') else 'budget')}


def _resolve_source(env, source):
    """-> (mime, bytes, media_row|None) or None. Order: the attachment, a named file, the latest picture of the chat."""
    source = (source or 'last').strip()
    if source == 'attached' and env.attached:
        return env.attached[0], env.attached[1], None
    if media.valid_id(source):
        row = media.get(env.db, source)
        if row and row['owner_key'] == env.owner and row['kind'] == 'image':
            return row['mime'], row['data'], row
    for r in media.recent(env.db, env.owner, env.conv_id, 8):
        if r['kind'] == 'image':
            row = media.get(env.db, r['id'])
            if row:
                return row['mime'], row['data'], row
    if env.attached:
        return env.attached[0], env.attached[1], None
    return None


def _save_image(env, raw, mime, name, meta, hidden=False):
    ext = 'png' if mime == 'image/png' else 'jpg'
    mid = media.save(env.db, env.owner, 'image', mime, media.safe_name(name, ext), raw,
                     conversation_id=env.conv_id, meta=meta, hidden=hidden)
    env.created.append(mid)
    return mid


def _finish_image(env, raw, mime, prompt, layers, label):
    """Optionally draw the text layers; store the picture(s); return the visible media id."""
    meta = {'prompt': prompt[:600], 'label': label}
    if layers:
        base_id = _save_image(env, raw, mime, 'base', {'role': 'base'}, hidden=True)
        out, out_mime = dari_text.overlay_text(raw, layers)
        meta.update(base=base_id, layers=layers)
        mid = _save_image(env, out, out_mime, 'khayyam-image', meta)
    else:
        mid = _save_image(env, raw, mime, 'khayyam-image', meta)
    env.media_ids.append(mid)
    return mid


def _guarded(env, kind, detail, work):
    """Reserve allowance -> work() -> refund if it raised."""
    slot = kind in _SLOT_KINDS
    if slot and not _SLOTS.acquire(timeout=float(os.getenv('TOOL_WAIT_SECONDS', '25'))):
        return {'ok': False, 'error': 'busy', 'message': 'The server is busy making other pictures right now. '
                                                         'Tell the user to try again in a minute. Nothing was charged.'}
    try:
        return _guarded_inner(env, kind, detail, work)
    finally:
        if slot:
            _SLOTS.release()


def _guarded_inner(env, kind, detail, work):
    rid, why = metering.tool_reserve(env.db, env.uid, kind, env.plan, detail)
    if not rid:
        return _quota_error(why, kind)
    env.cost_micro = None
    try:
        result = work()
    except gemini.GeminiError as exc:
        metering.tool_finalize(env.db, rid, failed=not exc.billed)
        if env.log:
            env.log.warning('tool %s failed: %s', kind, exc)
        if exc.blocked:
            return {'ok': False, 'error': 'blocked',
                    'message': 'The safety system did not accept this request. Tell the user and suggest a different idea.'}
        return {'ok': False, 'error': 'failed', 'message': 'Temporarily not possible. Nothing was charged.'}
    except Exception:
        metering.tool_finalize(env.db, rid, failed=True)
        if env.log:
            env.log.exception('tool %s crashed', kind)
        return {'ok': False, 'error': 'failed', 'message': 'Temporarily not possible. Nothing was charged.'}
    metering.tool_finalize(env.db, rid, cost_micro=env.cost_micro)
    return result


# ------------------------------------------------------------------------------------------ tools
def t_generate_image(env, a):
    prompt = str(a.get('prompt') or '').strip()
    if not prompt:
        return {'ok': False, 'error': 'failed', 'message': 'empty prompt'}
    if not gemini.available():
        return {'ok': False, 'error': 'failed', 'message': 'Image creation is not set up on this server.'}
    layers = _layers(a.get('text_layers'))
    full = prompt + '\n\n' + (dari_text.composition_hint(layers) if layers else
                              'Do not draw any letters or words in the picture unless the description needs a sign.')

    # HD (2K) only when asked AND the plan still has HD pictures this month; otherwise a normal picture.
    hd = a.get('quality') == 'high' and metering.hd_left(env.db, env.uid, env.plan)

    def work():
        mime, raw = gemini.generate_image(full, (), a.get('aspect_ratio') or '1:1', '2K' if hd else '1K')
        mid = _finish_image(env, raw, mime, prompt, layers, 'generated')
        res = {'ok': True, 'media_id': mid, 'text_drawn_by_khayyam': bool(layers),
               'note': 'The picture is shown to the user automatically below your message.'}
        if a.get('quality') == 'high' and not hd:
            res['note'] += (' It was made in standard quality because HD pictures are not part of this '
                            "plan (or this month's HD pictures are used up). Mention it briefly, only if relevant.")
        return res
    return _guarded(env, 'image_hd' if hd else 'image', prompt[:100], work)


def t_edit_image(env, a):
    src = _resolve_source(env, a.get('source'))
    if not src:
        return {'ok': False, 'error': 'failed', 'message': 'No picture to edit was found. Ask the user to attach one.'}
    if not gemini.available():
        return {'ok': False, 'error': 'failed', 'message': 'Image editing is not set up on this server.'}
    mime, raw, row = src
    instruction = str(a.get('instruction') or '').strip()
    layers = _layers(a.get('text_layers'))
    keep = []
    if row and row['meta'].get('base') and row['meta'].get('layers'):
        base = media.get(env.db, row['meta']['base'])       # edit the clean picture, then redraw the same text
        if base and base['owner_key'] == env.owner:
            mime, raw, keep = base['mime'], base['data'], row['meta']['layers']
    final_layers = layers or keep
    full = instruction + '\n\nKeep everything not mentioned unchanged. ' + (
        dari_text.composition_hint(final_layers) if final_layers else 'Do not add any new letters or words.')

    def work():
        out_mime, out = gemini.generate_image(full, [(mime, raw)], _same_aspect(raw), '1K')
        mid = _finish_image(env, out, out_mime, instruction, final_layers, 'edited')
        return {'ok': True, 'media_id': mid, 'text_kept_or_redrawn': bool(final_layers),
                'note': 'The edited picture is shown to the user automatically.'}
    return _guarded(env, 'image', instruction[:100], work)


def _same_aspect(raw):
    """Closest supported aspect ratio to the source picture, so an edit keeps its shape."""
    try:
        w, h = dari_text.image_size(raw)
    except Exception:
        return '1:1'
    r = w / max(1, h)
    best = min(gemini.ASPECTS, key=lambda s: abs(int(s.split(':')[0]) / int(s.split(':')[1]) - r))
    return best


def t_add_text(env, a):
    layers = _layers(a.get('text_layers'))
    if not layers:
        return {'ok': False, 'error': 'failed', 'message': 'No text given.'}
    src = _resolve_source(env, a.get('source'))
    if not src:
        return {'ok': False, 'error': 'failed', 'message': 'No picture was found. Ask the user to attach one.'}
    mime, raw, row = src
    if row and row['meta'].get('base'):
        base = media.get(env.db, row['meta']['base'])
        if base and base['owner_key'] == env.owner:
            mime, raw = base['mime'], base['data']          # always redraw on the clean picture: no doubled text

    def work():
        mid = _finish_image(env, raw, mime, 'text overlay', layers, 'text')
        return {'ok': True, 'media_id': mid, 'note': 'The picture with the text is shown automatically.'}
    return _guarded(env, 'overlay', 'text', work)


def t_web_search(env, a):
    q = str(a.get('query') or '').strip()
    if not q:
        return {'ok': False, 'error': 'failed', 'message': 'empty query'}
    if not gemini.available():
        return {'ok': False, 'error': 'failed', 'message': 'Web search is not set up on this server.'}

    def work():
        res = gemini.web_search(q)
        env.cost_micro = metering.search_cost_micro(len(res.get('queries') or []))
        for s in res['sources']:
            if re.match(r'^https?://', s['url']) and s['url'] not in [x['url'] for x in env.sources]:
                env.sources.append(s)
        return {'ok': True, 'results': res['text'], 'sources': [s['title'] for s in res['sources']],
                'note': 'Answer from this text only. Source links are added automatically at the end.'}
    return _guarded(env, 'search', q[:100], work)


def t_create_document(env, a):
    fmt = str(a.get('format') or 'pdf').lower()
    if fmt not in ('pdf', 'docx', 'xlsx'):
        return {'ok': False, 'error': 'failed', 'message': 'format must be pdf, docx or xlsx'}
    spec = a.get('spec') if isinstance(a.get('spec'), dict) else {}

    def work():
        data, mime, ext = dari_text.build_document(fmt, spec)
        name = media.safe_name(a.get('filename') or spec.get('title') or 'khayyam', ext)
        mid = media.save(env.db, env.owner, ext, mime, name, data, conversation_id=env.conv_id,
                         meta={'title': str(spec.get('title') or '')[:120]})
        env.media_ids.append(mid)
        env.created.append(mid)
        return {'ok': True, 'media_id': mid, 'file': name,
                'note': 'The file is shown to the user automatically with a download button.'}
    return _guarded(env, 'doc', fmt, work)


EXECUTORS = {'generate_image': t_generate_image, 'edit_image': t_edit_image, 'add_text_to_image': t_add_text,
             'web_search': t_web_search, 'create_document': t_create_document}

STATUS = {'generate_image': 'در حال ساختن تصویر', 'edit_image': 'در حال ویرایش تصویر',
          'add_text_to_image': 'در حال نوشتن متن روی تصویر', 'web_search': 'در حال جستجو در وب',
          'create_document': 'در حال ساختن فایل'}


def run(env, name, raw_args):
    """Execute one tool call. Never raises; returns the dict the model receives."""
    fn = EXECUTORS.get(name)
    if not fn:
        return {'ok': False, 'error': 'failed', 'message': 'unknown tool'}
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
        if not isinstance(args, dict):
            args = {}
    except ValueError:
        return {'ok': False, 'error': 'failed', 'message': 'invalid arguments'}
    return fn(env, args)


def context_note(env):
    """Short list of this chat's earlier pictures/files, so 'make it bluer' can name the right one."""
    rows = media.recent(env.db, env.owner, env.conv_id, 5)
    lines = []
    for r in rows:
        if r['kind'] == 'image':
            texts = ' | '.join(l.get('text', '')[:40] for l in (r['meta'].get('layers') or []))
            lines.append('- image id=%s prompt="%s"%s' % (r['id'], str(r['meta'].get('prompt', ''))[:80],
                                                          (' text="%s"' % texts) if texts else ''))
        else:
            lines.append('- file id=%s name=%s' % (r['id'], r['name']))
    note = ''
    if lines:
        note += 'Earlier in this chat (newest first):\n' + '\n'.join(lines) + '\n'
    if env.attached:
        note += "The user's current message includes an attached picture (source='attached').\n"
    return note.strip()