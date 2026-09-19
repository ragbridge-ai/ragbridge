"""Keep the test suite away from the development database.

The suite empties every table before each test. It used to do that in whatever
database ``DATABASE_URL`` named - which for a developer is the one the dev stack
uses - so running ``pytest`` silently deleted their tenants, API keys and
documents. Tests now run in a separate ``<name>_test`` database on the same
server, and the code that empties tables refuses to touch any other.
"""

import os
import re

from sqlalchemy.engine import make_url

from ragbridge.config import Settings

TEST_SUFFIX = "_test"


def derive_test_database_url(url: str) -> str:
    """The URL of the test database that goes with ``url``.

    Same server, user and password; the database name gains a ``_test``
    suffix, unless it already has one.
    """
    parsed = make_url(url)
    name = parsed.database or ""
    if not name.endswith(TEST_SUFFIX):
        parsed = parsed.set(database=name + TEST_SUFFIX)
    return parsed.render_as_string(hide_password=False)


def assert_is_test_database(url: str) -> None:
    """Raise unless ``url`` names a test database.

    The last line of defence before anything destructive: even if the
    environment is wrong, tables are never emptied in a database whose name
    does not end in ``_test``.
    """
    name = make_url(url).database or ""
    if not name.endswith(TEST_SUFFIX):
        raise RuntimeError(
            f"Refusing to empty tables in database {name!r}: the test suite only "
            f"runs against a database whose name ends in {TEST_SUFFIX!r}."
        )


def safe_database_name(url: str) -> str:
    """The database name in ``url``, checked to be safe to put in ``CREATE DATABASE``."""
    name = make_url(url).database or ""
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise RuntimeError(f"Unexpected characters in database name {name!r}.")
    return name


def point_environment_at_test_database() -> None:
    """Make ``DATABASE_URL`` the test database, for this process and its children.

    Must run before anything calls ``get_settings()``: it is cached, and
    ``ragbridge.main`` calls it at import time. ``tests/__init__.py`` calls
    this, and Python imports a package before the modules inside it.
    """
    os.environ["DATABASE_URL"] = derive_test_database_url(Settings().database_url)
