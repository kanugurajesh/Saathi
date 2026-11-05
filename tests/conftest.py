import os

os.environ["LLM_DISABLED"] = "1"  # tests exercise the deterministic engine; no network
os.environ["MONGODB_URI"] = ""

import pytest  # noqa: E402

from app import db as dbmod  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    dbmod.reset_db_for_tests()
    yield dbmod.get_db()
    dbmod.reset_db_for_tests()


def set_setting(name, value):
    from app.config import settings

    object.__setattr__(settings, name, value)
