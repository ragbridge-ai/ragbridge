"""Checks that migrations were applied to the database.

The session fixture in conftest.py creates the ``<name>_test`` database and runs
`alembic upgrade head` against it before any test runs, so this checks that the
migrations actually produced what the app needs.
"""

import asyncio

from sqlalchemy import text

from ragbridge.config import Settings
from ragbridge.db.session import create_engine


def test_vector_extension_is_installed() -> None:
    async def vector_extension_exists() -> bool:
        engine = create_engine(Settings())
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            )
            exists = result.first() is not None
        await engine.dispose()
        return exists

    assert asyncio.run(vector_extension_exists())
