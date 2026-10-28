"""Local Cast OAuth-shaped bootstrap, accepted only by our offline S3 server.

Wire layout comes from the Cast receiver's GoogleOauthCredential parser and
RobotAccountAuthManager consumer. See docs/cast-bootstrap.md.
This never obtains or generates a Google access token.
"""

import json
import re
from pathlib import Path

from .native import _field, _fields, _frames


def captured_account_ids(capture: bytes) -> list[str]:
    """Read linked account IDs from an owned Nest's saved S3 client context."""
    frames = _frames(capture)
    if not frames:
        return []
    values = [frames[0]]
    for number in (116159442, 3, 4, 1, 2, 2, 3, 1):
        values = [value for parent in values
                  for field, wire, value in _fields(parent)
                  if field == number and wire == 2]
    return list(dict.fromkeys(value.decode('ascii') for value in values
                              if re.fullmatch(rb'[0-9]{15,25}', value)))


def token_response(account_ids: list[str]) -> bytes:
    if not account_ids or any(not re.fullmatch(r'[0-9]{15,25}', value)
                              for value in account_ids):
        raise ValueError('local bootstrap needs captured linked account IDs')
    credentials = []
    for account_id in dict.fromkeys(account_ids):
        credential = _field(2, b'local-nest-offline-session')
        credential += _field(4, 0)  # No auth error.
        credential += _field(5, account_id.encode('ascii'))
        credentials.append(_field(1, credential))
    return b''.join(credentials)


def load_token_response(data_dir: Path) -> bytes:
    account_ids = json.loads((data_dir / 'offline-account-ids.json').read_text())
    return token_response(account_ids)
