import os
import tempfile
import unittest
from pathlib import Path

os.environ['DB_PATH'] = str(Path(tempfile.gettempdir())/'test_gold_bot_v1.sqlite3')
os.environ['WEBHOOK_SECRET'] = 'test-only-secret'
import bot
from fastapi.testclient import TestClient


class TestParsing(unittest.TestCase):
    def test_valid_money_and_weight(self):
        p = bot.parse_transaction('#فروش_آبشده\nبه علی رضایی\nمبلغ: ۲۵,۰۰۰,۰۰۰ تومان\nوزن: ۲.۵ گرم\nمظنه: ۴۵,۵۰۰,۰۰۰')
        self.assertEqual(p['category'], 'فروش')
        self.assertEqual(p['party'], 'علی رضایی')
        self.assertEqual(p['amount_toman'], '25000000')
        self.assertEqual(p['weight_g'], '2.5')
        self.assertEqual(p['quote_raw'], '45,500,000')
        self.assertFalse(p['problems'])

    def test_semicolon_fields_without_colon(self):
        p = bot.parse_transaction('#بانک_طلا #فروش_آبشده؛ به علی؛ مبلغ ۲۵۰۰۰ تومان؛ وزن ۰.۵ گرم؛ مظنه ۲۵۰')
        self.assertEqual(p['party'], 'علی')
        self.assertEqual(bot.Decimal(p['amount_toman']), bot.Decimal(25000))
        self.assertEqual(p['weight_g'], '0.5')

    def test_ambiguous_slash_refused(self):
        p = bot.parse_transaction('#فروش\nمشتری: محمد\nمبلغ: ۳۴/۵۰۰ تومان\nوزن: ۲/۵ گرم')
        self.assertFalse(p['amount_toman'])
        self.assertFalse(p['weight_g'])

    def test_rials_to_toman_and_sot(self):
        p = bot.parse_transaction('#خرید\nمشتری: محمد\nمبلغ: ۱۰۰٬۰۰۰٬۰۰۰ ریال\nوزن: ۵۰۰ سوت')
        self.assertEqual(bot.Decimal(p['amount_toman']), bot.Decimal('10000000'))
        self.assertEqual(bot.Decimal(p['weight_g']), bot.Decimal('0.5'))

    def test_ambiguous_units_are_not_summed(self):
        p = bot.parse_transaction('#فروش\nمشتری: محمد\nمبلغ: ۲۰۰۰۰\nوزن: ۳')
        self.assertFalse(p['amount_toman'])
        self.assertFalse(p['weight_g'])
        self.assertIn('واحد مبلغ', p['problems'])
        self.assertIn('واحد وزن', p['problems'])

    def test_unauthorized_private_no_response(self):
        calls = []
        old = bot.send
        bot.send = lambda *args: calls.append(args)
        try:
            bot.handle_private({'chat': {'type': 'private','id': 3},'from': {'id': 3},'text':'/report'})
        finally:
            bot.send = old
        self.assertEqual(calls, [])

    def test_only_approved_groups_and_no_duplicate(self):
        allowed = bot.ALLOWED_GROUP_IDS.copy()
        bot.ALLOWED_GROUP_IDS.clear()
        msg = {'message_id': 501, 'chat': {'type':'supergroup','id':-10055, 'title':'گالری ماهی'},
               'from': {'id': 1, 'first_name':'فروشنده'}, 'date':1720000000,
               'text':'#فروش\nمشتری: سارا\nمبلغ: ۱۰۰۰ تومان\nوزن: ۱ گرم'}
        try:
            bot.process_group(msg)
            self.assertEqual(bot.rows_for(), [])
            bot.ALLOWED_GROUP_IDS.add(-10055)
            bot.process_group(msg)
            bot.process_group(msg)
            self.assertEqual(len(bot.rows_for()), 1)
            msg['text'] = 'پیام اصلاح شد؛ بدون هشتگ'
            bot.process_group(msg)
            self.assertEqual(len(bot.rows_for()), 0)
        finally:
            bot.ALLOWED_GROUP_IDS.clear()
            bot.ALLOWED_GROUP_IDS.update(allowed)

    def test_webhook_requires_secret(self):
        c = TestClient(bot.app)
        self.assertEqual(c.post('/telegram-webhook', json={'message':{}}).status_code,404)
        self.assertEqual(c.post('/telegram-webhook', json={}, headers={'X-Telegram-Bot-Api-Secret-Token':'test-only-secret'}).status_code,200)


if __name__ == '__main__':
    unittest.main()
