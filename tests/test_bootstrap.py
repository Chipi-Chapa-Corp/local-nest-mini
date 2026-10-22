import unittest

from app.bootstrap import captured_account_ids, token_response
from app.native import _field, _fields, _frame


class BootstrapTest(unittest.TestCase):
    def test_account_context_and_credential_fields(self):
        ids = ['123456789012345678901', '223456789012345678901']
        context = b''.join(_field(3, _field(1, value.encode())) for value in ids)
        for number in reversed((116159442, 3, 4, 1, 2, 2)):
            context = _field(number, context)
        self.assertEqual(captured_account_ids(_frame(context)), ids)
        credentials = _fields(token_response(ids))
        self.assertEqual(len(credentials), 2)
        for (number, wire, value), account in zip(credentials, ids):
            self.assertEqual((number, wire), (1, 2))
            self.assertEqual(_fields(value), [
                (2, 2, b'local-nest-offline-session'), (4, 0, 0),
                (5, 2, account.encode())])

    def test_no_fabricated_account_fallback(self):
        for ids in ([], ['invalid']):
            with self.assertRaises(ValueError):
                token_response(ids)
