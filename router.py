"""Khayyam request router: decides HOW to answer before any paid call is made.

What it does, in order, per message (all local, no model call, microseconds):

1. classify()      -> intent level: canned | simple | standard | complex  (+ kind, output cap)
2. canned_reply()  -> pure greetings / thanks / goodbyes are answered locally for free
3. build_chat_prompt() -> a compact core prompt + ONLY the knowledge relevant to this message
4. trim_history()  -> a bounded conversation history (long replies are shortened)
5. order_options() -> which plan tier (model) to try first for this intent

Everything tunable lives in the ROUTING / LIMITS dictionaries below.
"""
import os
import random
import re

# ----------------------------------------------------------------------------
# Tunables
# ----------------------------------------------------------------------------
# Preference order of model classes per intent. Classes: nano, mini, full, oss.
# A plan only has the classes listed in its PLAN_CONFIG; missing ones are skipped,
# and anything not listed here is tried afterwards (so a user never gets "no model"
# just because the preferred class is exhausted).
ROUTING = {
    'simple':   ['mini', 'nano', 'full', 'oss'],
    'standard': ['mini', 'nano', 'full', 'oss'],
    'complex':  ['full', 'mini', 'nano', 'oss'],
}

# Max answer tokens per intent (hard cap sent to the provider).
LIMITS = {'simple': 450, 'standard': 900, 'complex': 1600}

CANNED_ENABLED = os.getenv('ROUTER_CANNED', 'true').lower() == 'true'

HISTORY_MESSAGES = int(os.getenv('ROUTER_HISTORY_MESSAGES', '6'))
HISTORY_ASSISTANT_CHARS = int(os.getenv('ROUTER_HISTORY_ASSISTANT_CHARS', '1200'))
HISTORY_USER_CHARS = int(os.getenv('ROUTER_HISTORY_USER_CHARS', '2000'))

# ----------------------------------------------------------------------------
# Text normalisation
# ----------------------------------------------------------------------------
_DIACRITICS = re.compile('[ً-ٰٟـ‌‍‎‏]')
_PUNCT = re.compile(r'[^\w\s]', re.UNICODE)


def normalize(text):
    t = str(text or '').lower()
    t = t.replace('ي', 'ی').replace('ك', 'ک').replace('ۀ', 'ه').replace('أ', 'ا').replace('إ', 'ا')
    t = _DIACRITICS.sub('', t)
    t = _PUNCT.sub(' ', t)
    return ' '.join(t.split())


def estimate_tokens(text):
    """Cheap, network-free token estimate (deliberately a little pessimistic).
    ASCII ~4 chars/token, Arabic-script ~2 chars/token."""
    s = str(text or '')
    ascii_chars = sum(1 for c in s if ord(c) < 128)
    other = len(s) - ascii_chars
    return int(ascii_chars / 4 + other / 2.0) + 1


# ----------------------------------------------------------------------------
# Canned replies (free, instant)
# ----------------------------------------------------------------------------
_CANNED = {
    'greet': (
        {'سلام', 'سلام علیکم', 'سلام و علیکم', 'السلام علیکم', 'السلام علیکم و رحمه الله',
         'سلام خیام', 'سلام علیکم خیام', 'سلام خیام جان', 'هلو', 'های', 'سلامتی',
         'hi', 'hello', 'hey', 'salam', 'salaam', 'hi khayyam', 'hello khayyam', 'hey khayyam'},
        ['سلام دوست عزیز! خوش آمدید. چطور می‌توانم کمک‌تان کنم؟',
         'سلام و خوش آمدید! بگویید، امروز چه چیزی در ذهن‌تان است؟',
         'سلام! خوش استید؟ چه کمکی از دست من برمی‌آید؟'],
    ),
    'howareyou': (
        {'چطور استی', 'چطور استید', 'چطوری', 'چطورید', 'چطور هستید', 'چطور هستی', 'خوبی', 'خوب استی',
         'خوب استید', 'خوب هستید', 'خوبید', 'احوال شما', 'حال شما چطور است', 'حالت چطور است',
         'سلام چطور استی', 'سلام چطور استید', 'سلام چطوری', 'سلام خوبی', 'سلام خوب استید',
         'how are you', 'hi how are you', 'hello how are you'},
        ['الحمدلله خوب استم، تشکر که پرسیدید! شما چطور استید؟ امروز چه کمکی می‌توانم بکنم؟',
         'خدا را شکر، خوب استم. شما چطور استید؟ بگویید چه خدمتی از من برمی‌آید.'],
    ),
    'thanks': (
        {'تشکر', 'تشکر زیاد', 'تشکر از شما', 'خیلی تشکر', 'متشکرم', 'ممنون', 'ممنونم', 'خیلی ممنون',
         'مننون', 'سپاس', 'سپاسگزارم', 'تشکر خیام', 'thanks', 'thank you', 'thx', 'tashakor', 'merci'},
        ['خواهش می‌کنم! هر وقت سوالی داشتید، من اینجا استم.',
         'قابل ندارد! اگر کار دیگری بود، بگویید.'],
    ),
    'bye': (
        {'خداحافظ', 'خدا حافظ', 'خدا نگهدار', 'فعلا', 'تا بعد', 'به امید دیدار', 'bye', 'goodbye', 'see you'},
        ['به امید دیدار! خدا نگهدارتان. هر وقت خواستید، برگردید.',
         'خدا نگهدار! موفق و سلامت باشید.'],
    ),
    'morning': (
        {'صبح بخیر', 'صبح شما بخیر', 'صبح خیر', 'good morning'},
        ['صبح شما هم بخیر! روز خوبی داشته باشید. چطور می‌توانم کمک‌تان کنم؟'],
    ),
    'evening': (
        {'عصر بخیر', 'عصر شما بخیر', 'شب بخیر', 'شب شما بخیر', 'good evening', 'good night'},
        ['شب و عصر شما هم بخیر! چطور می‌توانم کمک‌تان کنم؟'],
    ),
}


def canned_reply(message):
    """Return a reply string for a pure greeting/thanks/bye, else None."""
    if not CANNED_ENABLED:
        return None
    n = normalize(message)
    if not n or len(n) > 40:
        return None
    for phrases, replies in _CANNED.values():
        if n in phrases:
            return random.choice(replies)
    return None


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------
# Single words are matched as whole tokens (so "تثبیت" is not mistaken for poetry's "بیت");
# multi-word phrases are matched as substrings of the normalised text.
_POETRY_TOK = {'شعر', 'اشعار', 'غزل', 'رباعی', 'مصرع', 'بیت', 'قافیه', 'دوبیتی', 'poem', 'poetry', 'ghazal'}
_CODE_TOK = {'کد', 'کود', 'python', 'پایتون', 'javascript', 'جاوااسکریپت', 'html', 'css', 'sql', 'java', 'cpp',
             'bug', 'error', 'function', 'اسکریپت', 'الگوریتم', 'algorithm', 'کامپایل', 'compile', 'api', 'json'}
_CODE_PHRASE = ('برنامه بنویس', 'برنامه نویسی', 'خطا در', 'c++')
_MATH_TOK = {'معادله', 'انتگرال', 'مشتق', 'لیمیت', 'ماتریس', 'solve', 'equation', 'integral', 'derivative',
             'prove', 'calculate', 'matrix'}
_MATH_PHRASE = ('ثابت کن', 'حل کن', 'حساب کن', 'محاسبه کن')
_LONG_TOK = {'مقاله', 'انشا', 'تحلیل', 'مقایسه', 'خلاصه', 'ترجمه', 'مفصل', 'گزارش', 'essay', 'analyze',
             'analysis', 'compare', 'summarize', 'summary', 'translate'}
_WHY_TOK = {'چرا', 'چگونه', 'چطور', 'why', 'how', 'توضیح', 'explain', 'فرق', 'تفاوت', 'difference'}
_LONG_PHRASE = ('به تفصیل', 'کامل توضیح', 'گام به گام', 'قدم به قدم', 'step by step', 'in detail', 'write a')


def _tok(n, words):
    return any(w in words for w in n.split())


def _phrase(n, phrases):
    return any(p in n for p in phrases)


_MATH_EXPR = re.compile(r'[0-9۰-۹]\s*[\+\-\*/\^=×÷]\s*[0-9۰-۹a-zA-Z]|[a-z]\s*[\^=]\s*[0-9]|\\frac|\\sqrt|√|∫|∑')
_MEMORY_TRIGGERS = ('یادت باشد', 'یادت باشه', 'ذخیره کن', 'به یاد داشته باش', 'فراموش نکن', 'همیشه بدان',
                    'بدان که', 'حفظ کن', 'remember', 'save this', 'note that', 'keep in mind',
                    'always know', "dont forget", "don't forget")


def has_memory_trigger(message):
    low = str(message or '').lower()
    return any(t in low for t in _MEMORY_TRIGGERS)


def classify(message, history=None, has_image=False, has_doc=False, feature='chat'):
    """Return {'level','kind','max_output','effort'}.

    level  canned|simple|standard|complex
    kind   poetry|code|math|longform|document|image|chat   (selects prompt blocks)
    effort optional reasoning effort for models that support it ('low' for math/code)
    """
    raw = str(message or '')
    n = normalize(raw)
    out = {'level': 'standard', 'kind': 'chat', 'effort': None}

    if feature == 'chat' and not has_image and not has_doc and canned_reply_possible(n):
        out['level'] = 'canned'
        return _finish(out)

    if has_doc:
        out.update(level='complex', kind='document')
    elif _tok(n, _POETRY_TOK):
        out.update(level='complex', kind='poetry')
    elif '```' in raw or _tok(n, _CODE_TOK) or _phrase(n, _CODE_PHRASE):
        out.update(level='complex', kind='code', effort='low')
    elif _MATH_EXPR.search(raw) or _tok(n, _MATH_TOK) or _phrase(n, _MATH_PHRASE):
        out.update(level='complex', kind='math', effort='low')
    elif _tok(n, _LONG_TOK) or _phrase(n, _LONG_PHRASE):
        out.update(level='complex', kind='longform')
    elif has_image:
        out.update(level='standard', kind='image')
    elif len(raw) > 600:
        out.update(level='complex', kind='longform')
    elif _tok(n, _WHY_TOK):
        out['level'] = 'standard'          # explanations deserve room to breathe
    elif len(n.split()) <= 9 and len(raw) <= 70 and len(history or []) <= 4:
        out['level'] = 'simple'
    return _finish(out)


def canned_reply_possible(normalized):
    if not CANNED_ENABLED or not normalized or len(normalized) > 40:
        return False
    return any(normalized in phrases for phrases, _ in _CANNED.values())


def _finish(out):
    out['max_output'] = LIMITS.get(out['level'], LIMITS['standard'])
    if out['kind'] == 'poetry':
        out['max_output'] = min(out['max_output'], 1000)
    return out


# ----------------------------------------------------------------------------
# Model ordering
# ----------------------------------------------------------------------------
def model_class(model):
    m = str(model)
    if m.startswith('openai/gpt-oss'):
        return 'oss'
    if m.endswith('-nano') or '-nano' in m:
        return 'nano'
    if m.endswith('-mini') or '-mini' in m:
        return 'mini'
    return 'full'


def order_options(config, level):
    """config = PLAN_CONFIG[plan] style list. Returns options sorted by preference for
    `level`, each carrying its ORIGINAL tier number so token windows stay consistent."""
    pref = ROUTING.get(level) or ROUTING['standard']
    rank = {c: i for i, c in enumerate(pref)}
    indexed = []
    for index, spec in enumerate(config):
        indexed.append((rank.get(model_class(spec['model']), 99), index, spec))
    indexed.sort(key=lambda t: (t[0], t[1]))
    return [{'model': spec['model'], 'limit': spec['limit'], 'reset': spec['reset'],
             'tier': index + 1} for _, index, spec in indexed]


# ----------------------------------------------------------------------------
# History
# ----------------------------------------------------------------------------
def trim_history(history, tutor=False):
    """Keep the most recent messages; shorten long replies (the user rarely needs the
    model to re-read its own essay). Lessons keep a little more context than chat."""
    keep = HISTORY_MESSAGES + 4 if tutor else HISTORY_MESSAGES
    cleaned = []
    for m in (history or [])[-keep:]:
        if not isinstance(m, dict) or m.get('role') not in ('user', 'assistant'):
            continue
        content = str(m.get('content', ''))
        cap = HISTORY_ASSISTANT_CHARS if m['role'] == 'assistant' else HISTORY_USER_CHARS
        if len(content) > cap:
            content = content[:cap].rstrip() + ' …'
        cleaned.append({'role': m['role'], 'content': content})
    return cleaned


# ----------------------------------------------------------------------------
# Prompt assembly
# ----------------------------------------------------------------------------
CORE_PROMPT = (
    'تو «خیام» هستی، دستیار هوشمند افغانی. همیشه به دری افغانی طبیعی و روان صحبت کن '
    '(نه فارسی ایرانی)، با لحن گرم و محترمانه.\n'
    '- کوتاه و مفید جواب بده. به سوال ساده، یک یا دو جمله کافی است؛ فقط وقتی لازم است توضیح بیشتر بده.\n'
    '- نام تو خیام است؛ هرگز نگو ChatGPT یا مدل OpenAI هستی.\n'
    '- جنسیت کاربر را نمی‌دانی؛ از «برادر» و «خواهر» فقط وقتی استفاده کن که معلوم باشد.\n'
    '- نمادهای علمی (x, H2O, km) و کد مجاز است؛ کد را همیشه داخل ``` بگذار.\n'
    '- قالب: پاراگراف‌های کوتاه (حداکثر ۳ جمله)، برای فهرست «-»، نکته‌های مهم **بولد**؛ '
    'تیتر ## فقط برای پاسخ‌های بلند. جواب‌های کوتاه بدون قالب‌بندی.\n'
    '- لینک را نمی‌توانی باز کنی. تصویر و فایل را به دری توضیح بده.\n'
    '- اگر اطلاعاتی درباره کاربر داری، از آن استفاده کن.'
)

POETRY_BLOCK = (
    'حالت شعر. اگر کاربر شعر تازه می‌خواهد: هر مصرع را روی یک خط جداگانه بنویس، بین دو بیت یک خط خالی بگذار، '
    'قافیه را در تمام شعر حفظ کن و فقط شعر را بنویس. اگر به سبک شاعری خواسته شده، آن را سرودهٔ تازه به سبک او بدان '
    'و به نام خود او نیاور. '
    'اگر کاربر شعر یا بیتی از شاعری شناخته‌شده می‌خواهد یا معنی و شرح آن را می‌پرسد: متن را فقط وقتی نقل کن که '
    'کلمه‌به‌کلمه از درستی‌اش مطمئن هستی، و هرگز واژه‌ای را تغییر نده یا از خودت بیت نساز. اگر مطمئن نیستی، '
    'صادقانه بگو متن دقیق را به یاد نداری و به‌جای آن مفهوم و فضای شعر را توضیح بده. نام شاعر را فقط وقتی بگو '
    'که مطمئنی؛ رباعی‌های بسیاری به‌نادرست به خیام نسبت داده شده‌اند.'
)

MEMORY_BLOCK = (
    'کاربر خواسته چیزی را به یاد بسپاری. بگو: «بسیار خوب، این را در حافظه‌ام ذخیره کردم و در '
    'گفتگوهای بعدی به یاد خواهم داشت.» هرگز نگو که نمی‌توانی چیزی را به یاد بسپاری.'
)


def _words(text):
    return {w for w in normalize(text).split() if len(w) >= 3}


def _relevant(items, message, key_fn, limit):
    msg_words = _words(message)
    scored = []
    for item in items:
        score = len(msg_words & _words(key_fn(item)))
        if score:
            scored.append((score, item))
    scored.sort(key=lambda t: -t[0])
    return [item for _, item in scored[:limit]]


def build_chat_prompt(knowledge, examples, user_memories, message, kind='chat', level='standard'):
    parts = [CORE_PROMPT]

    # Afghan vocabulary: always present, in one compact line (this is what makes the
    # answer sound Afghan rather than Iranian).
    dialect = knowledge.get('dari_dialect', [])[:60]
    if dialect:
        pairs = '، '.join(f'{d["correct"]} (نه {d["wrong"]})' for d in dialect
                          if d.get('correct') and d.get('wrong'))
        if pairs:
            parts.append('واژه‌های افغانی را ترجیح بده: ' + pairs + '.')

    if kind == 'poetry':
        parts.append(POETRY_BLOCK)

    # Cultural notes only when the message touches them.
    customs = knowledge.get('cultural_customs', [])
    if customs and level != 'simple':
        hits = _relevant(customs, message, lambda c: str(c.get('topic', '')) + ' ' + str(c.get('content', ''))[:200], 3)
        if hits:
            parts.append('دانش فرهنگی مرتبط:\n' + '\n'.join(f'- {c["topic"]}: {c["content"]}' for c in hits))

    # Style examples: relevant ones, otherwise the single shortest as a tone anchor.
    ex_list = [e for e in examples.get('conversation_examples', []) if e.get('user') and e.get('assistant')]
    if ex_list and level != 'simple':
        hits = _relevant(ex_list, message, lambda e: e['user'], 2)
        if not hits:
            hits = [min(ex_list, key=lambda e: len(e['assistant']))]
        parts.append('نمونه لحن:\n' + '\n\n'.join(f'کاربر: {e["user"]}\nخیام: {e["assistant"]}' for e in hits))

    if has_memory_trigger(message):
        parts.append(MEMORY_BLOCK)

    if user_memories:
        notes = '\n'.join(f'- {m}' for m in user_memories[:8])
        parts.append('یادداشت‌های ذخیره‌شده کاربر (به زبان خود او):\n' + notes)

    return '\n\n'.join(parts)


# ----------------------------------------------------------------------------
# Memory extraction without a model call
# ----------------------------------------------------------------------------
_TRIGGER_STRIP = re.compile(
    r'(یادت\s*باشد|یادت\s*باشه|به\s*یاد\s*داشته\s*باش|فراموش\s*نکن|همیشه\s*بدان|بدان\s*که|ذخیره\s*کن|حفظ\s*کن|'
    r'remember(\s+that)?|save\s+this|note\s+that|keep\s+in\s+mind|always\s+know|don\'?t\s*forget)',
    re.IGNORECASE)


def extract_memory_text(message):
    """Turn "یادت باشه اسم من احمد است" into "اسم من احمد است". No model call, no tokens."""
    raw = str(message or '').strip()
    if not has_memory_trigger(raw):
        return None
    cleaned = _TRIGGER_STRIP.sub(' ', raw)
    cleaned = re.sub(r'^[\s:،,\-—!؟?.]+|[\s:،,\-—]+$', '', cleaned)
    cleaned = re.sub(r'^که\s+', '', cleaned)
    cleaned = ' '.join(cleaned.split())
    if len(cleaned) < 4:
        return None
    return cleaned[:300]