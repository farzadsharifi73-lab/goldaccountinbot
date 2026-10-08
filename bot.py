"""Private Telegram bookkeeping reader for approved gold-gallery groups.

Pilot: local SQLite is not persistent on Render Free. Never treat pilot numbers as audited.
"""
import csv
import hmac
import io
import logging
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

LOG = logging.getLogger('goldbot')
logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
TEHRAN = ZoneInfo('Asia/Tehran')
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
OWNER_ID = int(os.environ.get('OWNER_TELEGRAM_ID', '1508795686'))
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '').strip()
DB_PATH = Path(os.environ.get('DB_PATH', '/tmp/goldbot.sqlite3'))
ALLOWED_GROUP_IDS = {int(i) for i in re.split(r'[\s,;]+', os.environ.get('ALLOWED_GROUP_IDS', '').strip()) if i}
API = f'https://api.telegram.org/bot{TOKEN}' if TOKEN else ''
app = FastAPI(title="Private Gold Gallery Telegram Bot", docs_url=None, redoc_url=None, openapi_url=None)

CHAR_MAP = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩', '01234567890123456789')
CATEGORY_KEYWORDS = (
    ('فروش', ('#فروش', '#فروش_آبشده', '#فروش_ساخته', '#فروش_بانک_طلا')),
    ('خرید', ('#خرید', '#خرید_آبشده', '#خرید_ساخته', '#خرید_بانک_طلا')),
    ('دریافت', ('#دریافت', '#وصول')),
    ('پرداخت', ('#پرداخت',)),
    ('موجودی', ('#موجودی',)),
)


def normalize(s):
    return (s or '').translate(CHAR_MAP).replace('ي','ی').replace('ك','ک').replace('\u200c','_')


def decimal_str(value):
    if value is None:
        return ''
    v = normalize(value).replace('٬', ',').replace('٫', '.')
    # Slash notation is ambiguous in Persian bookkeeping (decimal vs grouping).
    if re.search(r'\d\s*/\s*\d', v):
        return ''
    v = re.sub(r'(?<=\d)[,،](?=\d{3}(?:\D|$))', '', v)
    v = re.sub(r'\s+', '', v)
    m = re.search(r'[-+]?\d+(?:\.\d+)?', v)
    if not m:
        return ''
    try:
        return str(Decimal(m.group()).normalize())
    except InvalidOperation:
        return ''


def field(text, labels):
    for line in text.replace('؛','\n').replace(';','\n').splitlines():
        line = line.strip(' \t-•')
        for label in labels:
            match = re.match(r'^' + re.escape(label) + r'(?:\s*[:：=\-]\s*|\s+)(.+)$', line, re.I)
            if match:
                return match.group(1).strip()
    return ''


def party_from_text(text):
    value = field(text, ('مشتری', 'طرف حساب', 'طرف_حساب', 'طرف معامله', 'نام مشتری', 'نام', 'شخص', 'خریدار', 'فروشنده معامله'))
    if value:
        return value
    for line in text.replace('؛','\n').replace(';','\n').splitlines():
        m = re.match(r'^\s*(?:به|از)\s+([^\n:]+?)\s*$', line)
        if m and not re.search(r'\b(?:ریال|تومان)\b', m.group(1)):
            return m.group(1).strip()
    return ''


def categorize(text):
    hashtags = re.findall(r'(?<!\w)#[\w\u0600-\u06FF]+', text)
    for tag in hashtags:
        for cat, triggers in CATEGORY_KEYWORDS:
            if any(tag == t or tag.startswith(t + '_') for t in triggers):
                return cat, tag
    return 'سایر', (hashtags[0] if hashtags else '')


def parse_money(text):
    src = field(text, ('مبلغ', 'مبلغ کل', 'جمع', 'مبلغ معامله', 'مبلغ فروش', 'مبلغ خرید'))
    if not src:
        return '', '', 'مبلغ معامله مشخص نیست'
    val = decimal_str(src)
    if not val:
        return '', '', 'مبلغ معامله قابل خواندن نیست'
    unit = 'ریال' if 'ریال' in src else ('تومان' if ('تومان' in src or 'تومن' in src) else '')
    if not unit:
        # No silent money conversion or assumption for audited totals.
        return '', '', f'واحد مبلغ مشخص نیست ({src})'
    num = Decimal(val)
    if unit == 'ریال':
        num = num / Decimal('10')
    return format(num, 'f'), 'تومان', ''


def parse_weight(text):
    src = field(text, ('وزن فلز', 'وزن خالص طلا', 'وزن', 'وزن طلا'))
    if not src:
        return '', '', 'وزن مشخص نیست'
    val = decimal_str(src)
    if not val:
        return '', '', 'وزن قابل خواندن نیست'
    if 'سوت' in src:
        return format(Decimal(val)/1000, 'f'), 'گرم', ''
    if 'گرم' in src:
        return format(Decimal(val), 'f'), 'گرم', ''
    return '', '', f'واحد وزن مشخص نیست ({src})'


def parse_transaction(text):
    normal = normalize(text)
    category, hashtag = categorize(normal)
    money, money_unit, money_issue = parse_money(normal)
    weight, weight_unit, weight_issue = parse_weight(normal)
    party = party_from_text(normal)
    quote = field(normal, ('مظنه', 'مزنه', 'قیمت مظنه', 'نرخ مظنه'))
    agent = field(normal, ('فروشنده', 'ثبت کننده', 'ثبت‌کننده', 'کاربر'))
    problems = []
    if category == 'سایر':
        problems.append('هشتگ نوع عملیات شناخته نشده')
    if category in {'فروش','خرید'}:
        if not party: problems.append('نام طرف حساب خالی است')
        if money_issue: problems.append(money_issue)
        if weight_issue: problems.append(weight_issue)
    elif category in {'دریافت','پرداخت'} and money_issue:
        problems.append(money_issue)
    # Inventory often intentionally contains only counts/weights; do not require amount.
    return {'category': category, 'hashtag': hashtag, 'party': party,
            'amount_toman': money, 'amount_unit': money_unit,
            'weight_g': weight, 'weight_unit': weight_unit,
            'quote_raw': quote, 'agent': agent, 'problems': '; '.join(problems)}


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout = 20000')
    con.execute('''CREATE TABLE IF NOT EXISTS seen_groups (
        chat_id INTEGER PRIMARY KEY, name TEXT NOT NULL, last_seen TEXT NOT NULL)''')
    con.execute('''CREATE TABLE IF NOT EXISTS transactions (
        chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        chat_name TEXT NOT NULL, sender TEXT NOT NULL, message_date TEXT NOT NULL,
        day_local TEXT NOT NULL, text_raw TEXT NOT NULL,
        category TEXT NOT NULL, hashtag TEXT, party TEXT,
        amount_toman TEXT, amount_unit TEXT, weight_g TEXT, weight_unit TEXT,
        quote_raw TEXT, agent TEXT, problems TEXT,
        PRIMARY KEY(chat_id, message_id))''')
    con.commit()
    return con


@contextmanager
def open_db():
    connection = db()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def tg(method, payload=None, files=None):
    if not TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is not configured')
    resp = requests.post(f'{API}/{method}', data=payload, files=files, timeout=22)
    resp.raise_for_status()
    result = resp.json()
    if not result.get('ok'):
        LOG.warning('Telegram API method %s returned an error', method)
    return result


def send(chat_id, msg):
    # Telegram text limit 4096 characters, avoid cutting through huge summaries.
    for i in range(0, len(msg), 3500):
        tg('sendMessage', {'chat_id': chat_id, 'text': msg[i:i+3500], 'disable_web_page_preview': 'true'})


def send_csv(chat_id, rows):
    data = io.StringIO()
    columns = ['chat_name', 'chat_id', 'message_id', 'day_local', 'message_date', 'sender', 'category',
               'hashtag', 'party', 'amount_toman', 'amount_unit', 'weight_g', 'weight_unit',
               'quote_raw', 'agent', 'problems', 'text_raw']
    writer = csv.DictWriter(data, fieldnames=columns, extrasaction='ignore')
    writer.writeheader()
    def excel_safe(value):
        value = '' if value is None else str(value)
        # CSV import into Excel must not execute formulas copied from group messages.
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value
    writer.writerows([{k: excel_safe(v) for k, v in dict(row).items()} for row in rows])
    binary = io.BytesIO(data.getvalue().encode('utf-8-sig'))
    binary.name = 'gold_transactions.csv'
    tg('sendDocument', {'chat_id': chat_id, 'caption': 'خروجی خام قابل بررسی؛ ردیف‌های مبهم نیازمند کنترل دستی‌اند.'},
       {'document': ('gold_transactions.csv', binary, 'text/csv')})


def process_group(msg):
    chat = msg.get('chat') or {}
    cid = int(chat.get('id', 0))
    if not cid:
        return
    chat_name = chat.get('title') or str(cid)
    with open_db() as con:
        con.execute('INSERT INTO seen_groups VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen',
                    (cid, chat_name, datetime.now(timezone.utc).isoformat()))
        if cid not in ALLOWED_GROUP_IDS:
            return
        txt = msg.get('text') or msg.get('caption') or ''
        if not re.search(r'(?<!\w)#[\w\u0600-\u06FF]+', txt):
            # A message edited to remove the hashtag is no longer reportable.
            con.execute('DELETE FROM transactions WHERE chat_id=? AND message_id=?',
                        (cid, int(msg['message_id'])))
            return
        info = parse_transaction(txt)
        sender_id = (msg.get('from') or {}).get('id')
        sender_name = ((msg.get('from') or {}).get('first_name') or '') + ' ' + ((msg.get('from') or {}).get('last_name') or '')
        sender = sender_name.strip() or str(sender_id or 'نامشخص')
        event_date = datetime.fromtimestamp(int(msg.get('date', datetime.now(timezone.utc).timestamp())), timezone.utc)
        con.execute('''INSERT INTO transactions
        (chat_id, message_id, chat_name, sender, message_date, day_local, text_raw,
         category, hashtag, party, amount_toman, amount_unit, weight_g, weight_unit,
         quote_raw, agent, problems)
         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(chat_id,message_id) DO UPDATE SET
         sender=excluded.sender, text_raw=excluded.text_raw,
         category=excluded.category,hashtag=excluded.hashtag,party=excluded.party,
         amount_toman=excluded.amount_toman,amount_unit=excluded.amount_unit,
         weight_g=excluded.weight_g,weight_unit=excluded.weight_unit,
         quote_raw=excluded.quote_raw,agent=excluded.agent,problems=excluded.problems''',
         (cid, int(msg['message_id']), chat_name, sender, event_date.isoformat(),
          event_date.astimezone(TEHRAN).date().isoformat(), txt,
          info['category'], info['hashtag'], info['party'], info['amount_toman'], info['amount_unit'],
          info['weight_g'], info['weight_unit'], info['quote_raw'], info['agent'], info['problems']))
    # Never post replies or reports in a group.


def rows_for(day=None, month=None, category=None):
    sql = 'SELECT * FROM transactions WHERE chat_id IN ({})'.format(','.join('?'*len(ALLOWED_GROUP_IDS))) if ALLOWED_GROUP_IDS else ''
    if not sql:
        return []
    args = sorted(ALLOWED_GROUP_IDS)
    if day:
        sql += ' AND day_local=?'
        args.append(day)
    if month:
        sql += ' AND day_local LIKE ?'
        args.append(month + '%')
    if category:
        sql += ' AND category=?'
        args.append(category)
    sql += ' ORDER BY message_date, chat_id, message_id'
    with open_db() as con:
        return con.execute(sql, args).fetchall()


def fmt_d(value):
    if value == 0: return '0'
    formatted = format(value, ',f')
    return formatted.rstrip('0').rstrip('.') if '.' in formatted else formatted


def summary(rows, title):
    buckets = defaultdict(lambda: {'count': 0, 'amount': Decimal(0), 'weight': Decimal(0),
                                   'missing_amount': 0, 'missing_weight': 0})
    for row in rows:
        item = buckets[row['category']]
        item['count'] += 1
        if row['amount_toman']:
            item['amount'] += Decimal(row['amount_toman'])
        else:
            item['missing_amount'] += 1
        if row['weight_g']:
            item['weight'] += Decimal(row['weight_g'])
        else:
            item['missing_weight'] += 1
    names = set(r['party'].strip() for r in rows if r['party'] and r['party'].strip())
    incomplete = sum(bool(r['problems']) for r in rows)
    lines = [title, f'تعداد پیام‌های هشتگ‌دار: {len(rows)}',
             f'طرف‌حساب‌های شناسایی‌شده: {len(names)}',
             f'پیام‌های نیازمند بررسی: {incomplete}', '']
    for cat, b in sorted(buckets.items()):
        lines.append(f'{cat}: {b["count"]} پیام')
        if cat in {'فروش', 'خرید', 'دریافت', 'پرداخت'}:
            lines.append(f'  جمع مبلغ دارای واحد معتبر: {fmt_d(b["amount"])} تومان | فاقد مبلغ معتبر: {b["missing_amount"]}')
        if cat in {'فروش','خرید','موجودی'}:
            lines.append(f'  جمع وزن قابل استخراج: {fmt_d(b["weight"])} گرم | فاقد وزن معتبر: {b["missing_weight"]}')
    lines.append('\n⚠️ مجموع‌ها فقط از داده‌های خوانا و دارای واحد مشخص استخراج شده‌اند؛ این گزارش تراز یا تأیید حسابرسی نیست.')
    return '\n'.join(lines)


def from_jalali_or_iso(s):
    s = normalize(s).replace('/', '-')
    m = re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})', s)
    if not m:
        return None
    year, mon, day = map(int, m.groups())
    try:
        if year < 1700:
            import jdatetime
            return jdatetime.date(year, mon, day).togregorian().isoformat()
        return datetime(year, mon, day).date().isoformat()
    except (ValueError, TypeError):
        return None


def handle_private(msg):
    chat = msg.get('chat', {})
    uid = int((msg.get('from') or {}).get('id', 0))
    cid = int(chat.get('id', 0))
    if uid != OWNER_ID or cid != OWNER_ID:
        # We do not disclose any information, including group names, to others.
        return
    s = normalize((msg.get('text') or '').strip())
    if not s.startswith('/'):
        return
    args = s.split()
    cmd = args[0].split('@',1)[0].lower()
    today = datetime.now(TEHRAN).date().isoformat()
    if cmd == '/start':
        send(cid, 'ربات اختصاصی حسابداری فعال است. فقط شما مجاز هستید.\nدستورات: /ping /groups /report /month /customers /quotes /incomplete /export /help')
    elif cmd == '/help':
        send(cid, 'دستورات خصوصی:\n/ping سلامت ربات\n/groups شناسه گروه‌های شناسایی‌شده\n/report [1405/07/16] گزارش روز (پیش‌فرض امروز)\n/month [1405/07] گزارش ماه (پیش‌فرض ماه جاری میلادی)\n/customers [1405/07/16] فهرست اشخاص روز\n/quotes [1405/07/16] مظنه‌های ثبت‌شده\n/incomplete [1405/07/16] ردیف‌های ناقص\n/export [1405/07/16] فایل CSV روز یا /export all همه\nهیچ پیامی در گروه ارسال نمی‌شود.')
    elif cmd == '/ping':
        send(cid, '✅ ربات در دسترس است. پایگاه داده فعلی: ' + ('آماده' if DB_PATH.exists() else 'آغاز نشده'))
    elif cmd == '/groups':
        with open_db() as con:
            groups = con.execute('SELECT * FROM seen_groups ORDER BY last_seen DESC').fetchall()
        lines = ['گروه‌های مشاهده‌شده (گروه‌های غیرمجاز خوانده یا ثبت معاملاتی نمی‌شوند):']
        for g in groups:
            lines.append(f'{g["name"]}: {g["chat_id"]} — {"مجاز" if g["chat_id"] in ALLOWED_GROUP_IDS else "غیرمجاز"}')
        send(cid, '\n'.join(lines) if groups else 'هنوز هیچ گروهی شناسایی نشده؛ ربات را به گروه گالری ماهی اضافه کنید.')
    elif cmd == '/report':
        day = from_jalali_or_iso(args[1]) if len(args)>1 else today
        if not day: send(cid, 'تاریخ نامعتبر؛ نمونه: /report 1405/07/16'); return
        send(cid, summary(rows_for(day=day), f'گزارش روز {day} (تقویم میلادی / ساعت تهران)'))
    elif cmd == '/month':
        if len(args)>1:
            s2 = normalize(args[1]).replace('/', '-').strip()
            match = re.fullmatch(r'(\d{4})-(\d{1,2})', s2)
            if not match:
                send(cid, 'ماه نامعتبر؛ نمونه: /month 1405/07'); return
            y,m = map(int, match.groups())
            if y < 1700:
                import jdatetime
                try:
                    start = jdatetime.date(y,m,1).togregorian()
                    ny, nm = (y+1,1) if m==12 else (y,m+1)
                    end = jdatetime.date(ny,nm,1).togregorian()
                except ValueError:
                    send(cid,'ماه نامعتبر'); return
                rows = [r for r in rows_for() if start.isoformat() <= r['day_local'] < end.isoformat()]
                send(cid,summary(rows, f'گزارش ماه شمسی {y}/{m:02d}'))
                return
            mon = f'{y:04d}-{m:02d}'
        else:
            mon = today[:7]
        send(cid,summary(rows_for(month=mon), f'گزارش ماه {mon} میلادی'))
    elif cmd in ('/customers', '/quotes', '/incomplete', '/export'):
        if cmd == '/export' and len(args)>1 and args[1].lower()=='all':
            rows = rows_for()
        else:
            day = from_jalali_or_iso(args[1]) if len(args)>1 else today
            if not day: send(cid,'تاریخ نامعتبر.'); return
            rows = rows_for(day=day)
        if cmd == '/customers':
            names = sorted({r['party'] for r in rows if r['party']})
            send(cid, 'طرف‌حساب‌ها ('+str(len(names))+'):\n'+'\n'.join(names[:140]) if names else 'طرف‌حساب شناسایی نشد.')
        elif cmd == '/quotes':
            matches = [r for r in rows if r['quote_raw']]
            items = [f'{r["chat_name"]} | #{r["message_id"]}: {r["quote_raw"]} — {r["party"] or "بدون طرف حساب"}' for r in matches[:100]]
            send(cid,'مظنه‌های خام ثبت‌شده ('+str(len(matches))+'):\n'+'\n'.join(items) if items else 'مظنه‌ای ثبت نشده است.')
        elif cmd == '/incomplete':
            bad = [r for r in rows if r['problems']]
            lines = [f'{r["chat_name"]} | پیام {r["message_id"]}: {r["problems"]}' for r in bad[:100]]
            send(cid,'موارد نیازمند بررسی ('+str(len(bad))+'):\n'+'\n'.join(lines) if lines else 'مورد ناقص مشخص نشد.')
        elif cmd == '/export':
            send_csv(cid, rows)
    else:
        send(cid,'دستور ناشناخته؛ /help')


@app.get('/health')
def health():
    return {'ok': True}


@app.post('/telegram-webhook')
async def webhook(request: Request):
    if not WEBHOOK_SECRET or not hmac.compare_digest(request.headers.get('X-Telegram-Bot-Api-Secret-Token',''), WEBHOOK_SECRET):
        return JSONResponse({'error': 'not found'}, status_code=404)
    if 'application/json' not in request.headers.get('content-type', ''):
        return JSONResponse({'error': 'bad request'}, status_code=400)
    try:
        update = await request.json()
    except ValueError:
        return JSONResponse({'error': 'bad JSON'}, status_code=400)
    msg = update.get('edited_message') or update.get('message')
    if not msg:
        return {'ok': True}
    typ = (msg.get('chat') or {}).get('type')
    if typ in ('group','supergroup'):
        process_group(msg)
    elif typ == 'private':
        handle_private(msg)
    return {'ok': True}


def setup_webhook():
    host = os.environ.get('WEBHOOK_BASE_URL', '').strip().rstrip('/')
    if not host:
        hostname = os.environ.get('RENDER_EXTERNAL_HOSTNAME','').strip()
        if hostname:
            host = f'https://{hostname}'
    if not host or not TOKEN or not WEBHOOK_SECRET:
        LOG.warning('Webhook not registered: missing host, token or webhook secret')
        return
    if urlparse(host).scheme != 'https':
        raise ValueError('WEBHOOK_BASE_URL must be https')
    try:
        result = tg('setWebhook', {
            'url': host + '/telegram-webhook',
            'secret_token': WEBHOOK_SECRET,
            'allowed_updates': '["message","edited_message"]',
            'drop_pending_updates': 'false',
        })
        LOG.info('Webhook registration result: %s', result.get('ok'))
    except Exception:
        LOG.exception('Webhook registration failed; service will still start')


# Gunicorn imports app, so configure while worker starts. One worker is required.
with open_db():
    pass
setup_webhook()

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', '10000')))
