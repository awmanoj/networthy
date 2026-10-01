"""Keep every test off the real database.

`storage.DATA_DIR` / `DB_PATH` are module-level by design (the launcher and the
tests both repoint them), which means a test that forgets to redirect them reads
and writes `data/networthy.db` — the developer's own data.

That failure is invisible locally, because the file exists and has the right
tables, so the test passes *and silently inserts rows*. It only surfaces on a
fresh checkout with no `data/` directory, i.e. in CI, as "no such table" — which
is exactly how it was found, four releases late.

This fixture is autouse, so it applies whether or not a test remembers. Tests
with their own `client` fixture repoint to their own tmp_path afterwards and are
unaffected.
"""

import pytest

from app import storage


@pytest.fixture(autouse=True)
def _never_touch_the_real_db(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_DIR", tmp_path)
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "test.db")
