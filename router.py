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

HISTORY_MESSAGES = int(os.getenv('ROUTER_HISTORY_MESSAGES', '12'))
HISTORY_ASSISTANT_CHARS = int(os.getenv('ROUTER_HISTORY_ASSISTANT_CHARS', '2200'))
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
_MEDIA_MARK = re.compile(r'\[\[media:(image|pdf|docx|xlsx):[0-9a-f]{32}\]\]')
_MEDIA_WORD = {'image': 'تصویر', 'pdf': 'فایل PDF', 'docx': 'فایل ورد', 'xlsx': 'فایل اکسل'}


def trim_history(history, tutor=False):
    """The recent part of the conversation: the last HISTORY_MESSAGES messages, long replies shortened.
    File/picture markers become a plain note so the model knows something was shown to the user.
    (Older parts of the chat are kept by conversation_memory below.)"""
    keep = HISTORY_MESSAGES + 4 if tutor else HISTORY_MESSAGES
    cleaned = []
    for m in (history or [])[-keep:]:
        if not isinstance(m, dict) or m.get('role') not in ('user', 'assistant'):
            continue
        content = _MEDIA_MARK.sub(lambda x: '[%s برای کاربر ساخته و نشان داده شد]' % _MEDIA_WORD[x.group(1)],
                                  str(m.get('content', '')))
        cap = HISTORY_ASSISTANT_CHARS if m['role'] == 'assistant' else HISTORY_USER_CHARS
        if len(content) > cap:
            content = content[:cap].rstrip() + ' …'
        cleaned.append({'role': m['role'], 'content': content})
    return cleaned


def _clip(text, n):
    t = _MEDIA_MARK.sub('', str(text or '')).strip().replace('\n', ' ')
    return t if len(t) <= n else t[:n].rstrip() + ' …'


SUMMARY_PROMPT = (
    'تو حافظهٔ یک گفتگو را می‌نویسی تا دستیار بعداً بداند گفتگو دربارهٔ چیست. از «خلاصهٔ قبلی» و «پیام‌های تازه» یک '
    'خلاصهٔ تازه و کامل به دری بنویس، حداکثر ۱۸۰ کلمه، فقط با همین بخش‌ها (بخش خالی را ننویس):\n'
    'موضوع و هدف گفتگو: …\n'
    'خواسته‌ها و دستورهای ثابت کاربر (زبان، لحن، قالب، نام، سلیقه): …\n'
    'مسائل مهم، تصمیم‌ها و نتیجه‌ها: …\n'
    'کارهای نیمه‌کاره یا چیزی که کاربر منتظرش است: …\n'
    'تصویر و فایل‌های ساخته‌شده: …\n'
    'فقط چیزی را بنویس که واقعاً در گفتگو آمده؛ چیزی نساز. دستورهای ثابت کاربر را هرگز حذف نکن مگر خودش لغوشان '
    'کرده باشد. خلاصهٔ قبلی را با اطلاعات تازه یکی کن، نه این‌که فقط به آن اضافه کنی. فقط خودِ خلاصه را بنویس.'
)


def summary_input(old_summary, messages):
    """User-turn text for the summarizer: previous summary + new messages (each shortened)."""
    lines = []
    for m in messages:
        who = 'کاربر' if m['role'] == 'user' else 'خیام'
        lines.append('%s: %s' % (who, _clip(m['content'], 700 if m['role'] == 'user' else 450)))
    return 'خلاصهٔ قبلی:\n%s\n\nپیام‌های تازه:\n%s' % (old_summary or '(هنوز ندارد)', '\n'.join(lines))


def conversation_memory(first_user, older_users, summary=None):
    """Long-conversation memory without any extra model call: the very first request of the chat, plus
    a short list of what the user asked in between (older than the recent window)."""
    parts = []
    if first_user:
        parts.append('آغاز این گفتگو (درخواست اول کاربر): «%s»' % _clip(first_user, 700))
    if summary:
        parts.append('خلاصهٔ گفتگو تا اینجا (که دستیار خودش نوشته):\n' + summary.strip()[:1800])
        older_users = []                       # the summary already covers the older messages
    items = [_clip(t, 110) for t in (older_users or []) if str(t or '').strip()]
    if len(items) > 24:
        items = items[:4] + items[-20:]
    if items:
        parts.append('پیام‌های قبلی کاربر در همین گفتگو، از قدیمی به جدید (خلاصه):\n' + '\n'.join('- ' + t for t in items))
    if not parts:
        return ''
    return ('حافظهٔ گفتگو: بخش‌های قدیمی‌تر این گفتگو از پنجرهٔ پیام‌های اخیر بیرون رفته‌اند، ولی این یادآوری‌ها را داری. '
            'هر چیزی که کاربر پیش‌تر گفته یا خواسته را به یاد داشته باش و دوباره نپرس.\n' + '\n'.join(parts))


# ----------------------------------------------------------------------------
# Prompt assembly
# ----------------------------------------------------------------------------
CORE_PROMPT = (
    'تو «خیام» هستی؛ دستیار هوشمند و همه‌کاره برای مردم افغانستان که به دری افغانی عالی صحبت می‌کند. '
    'محدود به موضوع افغانی یا زبان دری نیستی: در هر موضوعی (برنامه‌نویسی، ریاضی، علوم، کار، نوشتن، ترجمه، تحلیل، '
    'درس، گفتگوی روزمره) مثل بهترین دستیارها عمیق و دقیق جواب می‌دهی.\n'
    '- گفتگو به دری افغانی طبیعی و روان (نه فارسی ایرانی) و با لحن گرم و محترمانه است، حتی اگر کاربر انگلیسی نوشت یا '
    'خواست فارسی صحبت کنی. استثنا: اگر کاربر صریحاً خواست با او به زبان دیگری گفتگو کنی، تا وقتی نخواسته برگردی همان '
    'زبان (یادآوری پایان گفتگو را ببین).\n'
    '- کار جدا از گفتگو است: نتیجهٔ کارهایی مثل ترجمه، نامه، ایمیل، مقاله، رزومه، متن برای تصویر یا فایل را دقیقاً به '
    'زبانی بنویس که خواسته یا لازم است (نامهٔ انگلیسی کامل به انگلیسی)، و فقط توضیح‌های دور و برش را به دری بگو. '
    'کد و مثال‌ها به زبان خودشان باشند.\n'
    '- طول پاسخ به اندازهٔ نیاز: سؤال ساده کوتاه؛ سؤال پیچیده، تحلیل، کد یا نوشتن کامل و دقیق و با مثال.\n'
    '- نام تو خیام است؛ هرگز نگو ChatGPT یا مدل OpenAI هستی. جنسیت کاربر را نمی‌دانی؛ «برادر» و «خواهر» فقط وقتی که معلوم باشد.\n'
    '- کد را همیشه داخل ``` بگذار. قالب: پاراگراف‌های روشن و کوتاه، فهرست با «-»، نکتهٔ مهم **بولد**، تیتر ## فقط '
    'برای پاسخ بلند؛ جواب کوتاه بدون قالب.\n'
    '- تصویر و سند کاربر را می‌بینی و می‌خوانی (متن، خط‌نوشته، مسئله، ترجمه). لینک را نمی‌توانی باز کنی.\n'
    '- اگر اطلاعاتی درباره کاربر داری، از آن استفاده کن.'
)

TOOLS_BLOCK = (
    'ابزارها: در این گفتگو ابزار داری. وقتی کاربر کاری می‌خواهد که ابزار انجام می‌دهد، آن را واقعاً انجام بده و فقط '
    'دربارهٔ‌اش توضیح نده.\n'
    '- تصویر: برای ساختن یا ویرایش تصویر از ابزار استفاده کن، نه این‌که کد یا توضیح بنویسی. پرامپت تصویر را به انگلیسی، '
    'دقیق و دقیق‌تر از حرف کاربر بنویس (موضوع، سبک، نور، ترکیب‌بندی، رنگ). هرگز از مدل تصویر نخواه که نوشته یا حرف بکشد؛ '
    'هر متنی که روی تصویر باید باشد (دری، پشتو، انگلیسی) را فقط در text_layers بده تا سیستم ما آن را درست و با فونت '
    'درست روی تصویر بنویسد. املای متن را پیش از دادن با دقت و به دری افغانی درست کن. شعر را فقط از متن تأییدشده‌ای '
    'که در همین گفتگو به تو داده شده بگذار.\n'
    '- سند: وقتی کاربر فایل (PDF، ورد، اکسل) یا چیزی برای دانلود و چاپ می‌خواهد، create_document را صدا کن و '
    'محتوای کامل و واقعی را خودت بنویس، نه نمونهٔ خالی. اگر فقط جدول یا نوشته‌ای در گفتگو خواست، در همین گفتگو بنویس.\n'
    '- جستجو: برای خبر، قیمت، نتیجه، و هر چیزی که ممکن است تازه تغییر کرده باشد web_search را صدا کن و فقط بر اساس '
    'نتیجه‌اش جواب بده. چیزی از خودت نساز. منبع‌ها خودکار در پایان پاسخ می‌آیند.\n'
    '- بعد از موفقیت ابزار: فایل یا تصویر خودکار زیر پیام تو نشان داده می‌شود؛ لینک یا تصویر مارک‌داون ننویس. '
    'یک یا دو جملهٔ کوتاه بنویس و اگر مناسب بود پیشنهاد ویرایش بده.\n'
    '- اگر ابزار خطای quota داد، با مهربانی بگو سهمیهٔ این ماه برای آن امکان تمام شده؛ اگر blocked داد، بگو '
    'این درخواست توسط سیستم ایمنی پذیرفته نشد و راه دیگری پیشنهاد کن؛ اگر خطای دیگر بود، بگو موقتاً ممکن نشد و '
    'چیزی از سهمیه کم نشد.'
)

_PICTURE_RULE = ('اگر کاربر تصویری فرستاد بدون توضیح، یا نوشت «(تصویر فرستاده شد)»، و پیش‌تر کاری خواسته بود که به تصویر '
                 'نیاز داشت، همان کار را انجام بده؛ وگرنه تصویر را توضیح بده. به یاد داشته باش گفتگو دربارهٔ چیست.')

LANG_REMINDER = ('یادآوری زبان: زبان گفتگوی عادی همیشه دری افغانی است، حتی اگر کاربر انگلیسی یا زبان دیگری نوشته باشد. '
                 'زبان پاسخ‌های پیشین همین گفتگو را تقلید نکن؛ فقط همین یادآوری را دنبال کن. '
                 'اگر کاربر کاری خواسته که نتیجه‌اش خودِ یک متن به زبان دیگر است (ترجمه، نامه، ایمیل، مقاله، رزومه، '
                 'متن روی تصویر یا فایل، کد و مثال)، فقط همان متن را کامل به آن زبان بنویس و همهٔ جمله‌های دور و برش '
                 '(مقدمه، توضیح، پیشنهاد «می‌خواهی کوتاه‌ترش کنم؟») را به دری بگو. مثال: کاربر به انگلیسی نوشت «ایمیل بنویس» '
                 '← یک جملهٔ دری، بعد ایمیل انگلیسی، بعد یک جملهٔ دری. ' + _PICTURE_RULE)

LANG_ASK = ('کاربر بدون این‌که زبان پاسخ را بگوید به %s نوشته است. همین بار به دری جواب بده (کوتاه) و در پایان در یک جملهٔ '
            'کوتاه از او بپرس که پاسخ‌ها را به دری بخواهد یا به %s؛ همان جمله را یک بار هم به %s بنویس. ')

LANG_DECIDE_PROMPT = (
    'You decide which language an assistant must use for ordinary chat replies. The default is Dari (Afghan Persian). '
    'You get: the current setting, the assistant\'s last message, and the user\'s newest message. '
    'Answer with ONE line of JSON only: {"set": <string or null>, "ask": <true or false>, "writes": <string>}.\n'
    '"set": the language the user now explicitly wants for chat replies, else null. Set it when the user asks to talk/chat/reply '
    'in a language (any wording, any language or script, e.g. "lets speak turkish", "speak english", "dariye donusalim", '
    '"برگرد به دری", "gap bezan be dari"), or answers a question the assistant just asked about which language to use. '
    'Asking to go back to Dari, Persian or Farsi means "Dari". Also set "Dari" when the current setting is not Dari and '
    'the user writes ordinary chat in Dari/Persian (including Persian typed in Latin letters, like "khob hasti, chetor asti"). '
    'A task whose result is a text in some language (translate to English, write a letter/email in English, a poem in Pashto, '
    'explain a word in Turkish) is NOT a change: set null.\n'
    '"ask": true only when the current setting is "not chosen yet" AND the newest message is ordinary chat or a question '
    '(not a task producing text for them) written in a language other than Dari/Persian and with no language instruction. '
    'Never true if the assistant\'s last message already asked which language.\n'
    '"writes": the language the newest message is written in (English name).\n'
    'Examples: setting=not chosen, "i wanna write an email to my english teacher" -> {"set":null,"ask":false,"writes":"English"}; '
    'setting=not chosen, "what is gravity?" -> {"set":null,"ask":true,"writes":"English"}; '
    'setting=Turkish, "simdi dariye donusalim" -> {"set":"Dari","ask":false,"writes":"Turkish"}; '
    'setting=Turkish, "bale khob shd hali?" -> {"set":"Dari","ask":false,"writes":"Dari"}; '
    'setting=not chosen, "lets speak turkish" -> {"set":"Turkish","ask":false,"writes":"English"}.')


def needs_lang_check(state, text):
    """Is a language decision needed for this message? Not for the usual case: default setting + Persian script."""
    t = str(text or '')
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return False
    arabic = sum(1 for c in letters if '\u0600' <= c <= '\u06ff') / len(letters)
    if state not in (None, 'Dari'):
        return True
    if arabic < .5:
        return True
    return bool(re.search(_LN, t, re.I))


def lang_decide_input(state, last_assistant, user_message):
    setting = state if state else 'not chosen yet (default Dari)'
    return 'Current setting: %s\nAssistant\'s last message: %s\nUser\'s newest message: %s' % (
        setting, _clip(last_assistant, 260) or '(none)', _clip(user_message, 400))


def parse_lang_decision(text):
    """-> (set_language or None, ask, writes). Tolerant: anything unreadable means "no change"."""
    import json as _json
    m = re.search(r'\{.*\}', str(text or ''), re.S)
    if not m:
        return None, False, ''
    try:
        d = _json.loads(m.group(0))
    except ValueError:
        return None, False, ''
    def name(x):
        x = re.sub(r'[^A-Za-z \-]', '', str(x or '')).strip()[:24]
        return x.title() if x else None
    st = name(d.get('set'))
    if st and st.lower() in ('dari', 'persian', 'farsi', 'null', 'none'):
        st = 'Dari' if st.lower() != 'null' and st.lower() != 'none' else None
    return st, bool(d.get('ask')), name(d.get('writes')) or ''


# Languages a user can ask for: key -> (English name, Dari name). Dari/Persian map to None = back to normal.
_LANGS = {
    'english': ('English', 'انگلیسی'), 'pashto': ('Pashto', 'پشتو'), 'pushto': ('Pashto', 'پشتو'),
    'arabic': ('Arabic', 'عربی'), 'urdu': ('Urdu', 'اردو'), 'hindi': ('Hindi', 'هندی'),
    'turkish': ('Turkish', 'ترکی'), 'german': ('German', 'آلمانی'), 'french': ('French', 'فرانسوی'),
    'spanish': ('Spanish', 'اسپانیایی'), 'russian': ('Russian', 'روسی'), 'chinese': ('Chinese', 'چینی'),
    'uzbek': ('Uzbek', 'ازبکی'), 'italian': ('Italian', 'ایتالیایی'), 'japanese': ('Japanese', 'ژاپنی'),
    'korean': ('Korean', 'کره‌ای'),
    'dari': None, 'persian': None, 'farsi': None,
    'انگلیسی': ('English', 'انگلیسی'), 'انگلیسی': ('English', 'انگلیسی'), 'پشتو': ('Pashto', 'پشتو'),
    'عربی': ('Arabic', 'عربی'), 'اردو': ('Urdu', 'اردو'), 'هندی': ('Hindi', 'هندی'), 'ترکی': ('Turkish', 'ترکی'),
    'آلمانی': ('German', 'آلمانی'), 'فرانسوی': ('French', 'فرانسوی'), 'اسپانیایی': ('Spanish', 'اسپانیایی'),
    'روسی': ('Russian', 'روسی'), 'چینی': ('Chinese', 'چینی'), 'ازبکی': ('Uzbek', 'ازبکی'),
    'دری': None, 'فارسی': None,
}
_LN = '|'.join(sorted(map(re.escape, _LANGS), key=len, reverse=True))
# "speak / talk / chat in X", "X please", "from now on X": the choice lasts until the user changes it
_LANG_STICKY = re.compile(
    r'(?:(?<!\bI )(?<!\bwe )(?<!\bI can )(?<!\bI also )\b(?:speak|talk|chat|converse|conversation|switch(?:\s+to)?|talking|speaking|chatting)\b[^.?!\n]{0,40}?\b(%(l)s)\b)'
    r'|(?:\b(?:from now on|going forward)\b[^.?!\n]{0,40}?\b(%(l)s)\b)'
    r'|(?:\b(%(l)s)\b[^.?!\n]{0,15}\bfrom now\b)'
    r'|(?:\b(?:go back|back|return|switch back)\b[^.?!\n]{0,12}?\b(%(l)s)\b)'
    r'|(?:(?:برگرد|برگردید|برگشت)[^.?!\n؟]{0,12}?(%(l)s))'
    r'|(?:(?:صحبت|حرف|گپ|چت|گفتگو|مکالمه|صحبت‌?کن|حرف‌?بزن)[^.?!\n؟]{0,30}?(%(l)s))'
    r'|(?:(?:با من|به)\s+(%(l)s)\s+(?:با من\s+)?(?:صحبت|حرف|چت|گپ|جواب|پاسخ))'
    r'|(?:(%(l)s)\s+(?:صحبت|حرف|چت|گپ|جواب|پاسخ)\s*(?:کن|بده|بزن))' % {'l': _LN}, re.I)
# a short message that is just the language: "English please", "in English only"
_LANG_SHORT = re.compile(r'^\W*(?:(?:in|only|just|use)\s+)?(%(l)s)\s*(?:please|only|pls)?\W*$' % {'l': _LN}, re.I)
# "reply / answer in X": only for the message that asks
_LANG_ONCE = re.compile(
    r'(?:\b(?:reply|respond|answer)\b[^.?!\n]{0,15}?\bin\s+(%(l)s)\b)'
    r'|(?:(?:جواب|پاسخ)[^.?!\n؟]{0,12}?\s+(?:به|با)\s+(%(l)s))' % {'l': _LN}, re.I)


def _lang_of(match):
    key = next((g for g in match.groups() if g), '').lower()
    return key if key in _LANGS else None


def requested_language(past_user_texts, current=''):
    """The language the user explicitly asked for (or None = normal Dari). Reads the whole conversation:
    the latest explicit request wins, so "speak English" stays in force until they say otherwise."""
    state = None
    for t in list(past_user_texts or []) + [current or '']:
        m = _LANG_STICKY.search(t or '') or _LANG_SHORT.search(t or '')
        if m:
            key = _lang_of(m)
            state = _LANGS.get(key) if key else state
    m = _LANG_ONCE.search(current or '')
    if m:
        key = _lang_of(m)
        if key and _LANGS.get(key):
            return _LANGS[key]
    return state


def lang_reminder(lang=None, ask=None):
    """lang: None / 'Dari' (normal) or a language name string chosen by the user, or the old (English, Dari-name) tuple
    used for guests. ask: the language the user wrote in without saying what they want (see LANG_ASK)."""
    if isinstance(lang, tuple):
        lang = lang[0]
    if not lang or lang == 'Dari':
        return LANG_REMINDER + (LANG_ASK % (ask, ask, ask) if ask else '')
    return ('Language reminder: the user explicitly asked to chat in %s. Write every ordinary reply in %s, not Dari: greetings, '
            'small talk and short answers too, whatever language the earlier replies in this chat used. Keep the tone warm and '
            'natural. A task whose result is a text in some other language (for example a translation into Dari) is still '
            'written in that language. The newest user message is what you answer now. ' % (lang, lang)) + _PICTURE_RULE


TOOLS_OFF_BLOCK = (
    'ساختن و ویرایش تصویر و جستجوی وب هنوز در خیام فعال نشده است. اگر کاربر چنین چیزی خواست، صادقانه بگو '
    'این امکان به‌زودی اضافه می‌شود و تا آن وقت چیز دیگری پیشنهاد کن (مثلاً توصیف متنی، کد، یا ساختن فایل). '
    'ساختن فایل (PDF، ورد، اکسل) و نوشتن متن دری روی عکسی که کاربر می‌فرستد فعال است.'
)

TOOLS_GUEST_BLOCK = (
    'کاربر هنوز وارد حساب نشده است. ساختن و ویرایش تصویر، ساختن فایل و جستجوی وب فقط برای کاربران ثبت‌نام‌شده است. '
    'اگر چنین چیزی خواست، با مهربانی بگو برای این امکان ثبت‌نام رایگان لازم است، و تا جایی که می‌شود بدون ابزار کمک کن.'
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
            parts.append('وقتی به دری جواب می‌دهی، واژه‌های افغانی را ترجیح بده: ' + pairs + '.')

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


# ----------------------------------------------------------------------------
# Which tools to offer (decided by words, no model call). Tools cost input tokens, so a plain
# chat never carries them.
_IMG = re.compile(r'(عکس|تصویر|نقاشی|پوستر|بنر|لوگو|کارت\s*(تبریک|دعوت|ویزیت|پستال)|پس\s*زمینه|والپیپر|طراحی\s*کن|'
                  r'image|picture|photo|draw|poster|logo|banner|illustration|wallpaper|thumbnail|sticker|icon|artwork|'
                  r'render|sketch|design\s+(a|an|me))', re.I)
_IMG_EDIT = re.compile(r'(ویرایش|ادیت|تغییر|عوض|حذف|پاک|اضافه|رنگ|روشن|تاریک|پس\s*زمینه|متن|بنویس|بکش|زیباتر|قشنگ|'
                       r'edit|remove|erase|add|change|make\s+it|background|brighter|darker|caption|text|retouch|colorize|enhance|crop)', re.I)
_DOC = re.compile(r'(pdf|docx|xlsx|\bword\b|ورد|اکسل|excel|فایل|دانلود|download|رزومه|سی\s*وی|\bcv\b|resume|spreadsheet|'
                  r'document|سند|نامه\s*رسمی|فاکتور|بروشور|قرارداد|چاپ\s*کن|ورق)', re.I)
_SEARCH = re.compile(r'(جستجو|سرچ|search|google|گوگل|اخبار|خبر|\bnews\b|امروز|today|الان|اکنون|هم‌?اکنون|currently|'
                     r'latest|آخرین|جدیدترین|تازه‌?ترین|قیمت|\bprice|نرخ|ارزش|دالر|دلار|افغانی|ارز|weather|هوا\b|آب\s*و\s*هوا|'
                     r'نتیجه|score|بازی\s*(دیروز|امروز)|رییس|رئیس|president|وزیر|ceo|release|ریلیز|۲۰۲[5-9]|202[5-9]|۱۴۰[4-9])', re.I)


def tool_hints(message, last_assistant='', has_image=False, recent_user=''):
    """Set of {'image','doc','search'} worth offering for this message."""
    m = message or ''
    hints = set()
    pending = bool(has_image and (_IMG.search(recent_user or '') or _IMG_EDIT.search(recent_user or '')))
    if _IMG.search(m) or pending or (has_image and _IMG_EDIT.search(m)) or ('[[media:' in (last_assistant or '') and
                                                                  (_IMG_EDIT.search(m) or len(m) < 120)):
        hints.add('image')
    if _DOC.search(m):
        hints.add('doc')
    if _SEARCH.search(m):
        hints.add('search')
    return hints