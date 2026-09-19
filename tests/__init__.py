"""The test suite.

Importing this package points the suite at a separate ``<name>_test`` database
before anything else runs - see ``tests/database.py`` for why that has to happen
here, and not later.
"""

from tests.database import point_environment_at_test_database

point_environment_at_test_database()
