"""
pytest configuration — set required environment variables before any backend
module is imported.

config.py instantiates ``SETTINGS = Settings()`` at module level (line 100).
``Settings`` calls ``_require_int_env("LEAGUE_ID")`` and
``_require_int_env("ENTRY_ID")`` via field ``default_factory``, so importing
*any* backend module in CI (where no ``.env`` file exists) raises::

    ValueError: LEAGUE_ID environment variable is required but not set.

pytest loads ``conftest.py`` before collecting or importing test modules, so
setting the variables here guarantees they are present when the first
``import backend.*`` occurs.

``os.environ.setdefault`` is used so that any real values already present in
the environment (e.g. from a developer's ``.env`` or a CI secret) are
preserved and never overwritten.
"""
import os

os.environ.setdefault("LEAGUE_ID", "99999")
os.environ.setdefault("ENTRY_ID", "88888")
