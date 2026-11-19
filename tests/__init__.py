"""Never allow tests to fall through to a deployment URL from the local .env."""
import os

from sqlalchemy.engine import make_url
from finagent.storage import database as _storage

_original_get_database = _storage.get_database


def _test_database(settings=None):
    raw = settings.url.get_secret_value() if settings is not None else os.environ.get('FINAGENT_MYSQL_URL', '')
    try:
        url = make_url(raw)
        allowed = url.drivername == 'mysql+pymysql' and bool(url.database) and url.database.startswith('finagent_test')
    except Exception:
        allowed = False
    if not allowed:
        raise RuntimeError('Tests require an explicit disposable FINAGENT_MYSQL_URL with database name finagent_test*; production .env fallback is disabled.')
    return _original_get_database(settings)


_storage.get_database = _test_database
