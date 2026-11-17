"""Second review of 2026-11-17, R04: two saves of the same text for the same turn at once (spec §5.4)."""
import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from n3memorycore import n3memory as N
from n3memorycore.core import database as D
from n3memorycore.core import processor as P


def _run_two(tmp_path, turns):
    db = str(tmp_path / "race.db")
    c = D.get_connection(db)
    D.init_db(c)
    D.migrate_schema(c)
    c.close()
    barrier = threading.Barrier(2)

    def embed(_text):
        barrier.wait(timeout=10)          # both requests passed the first check before either writes
        return [1.0] + [0.0] * 767

    with contextlib.ExitStack() as st:
        st.enter_context(patch.object(N, "DB_PATH", db))
        st.enter_context(patch.object(N, "_srv_cfg", {"owner_id": "owner", "local_id": "local"}))
        st.enter_context(patch.object(N, "_srv_session", "session"))
        st.enter_context(patch.object(P, "embed_passage", embed))
        if hasattr(N, "embed_passage"):
            st.enter_context(patch.object(N, "embed_passage", embed))
        if hasattr(N, "_entity_enqueue"):
            st.enter_context(patch.object(N, "_entity_enqueue", lambda *a: None))
        with ThreadPoolExecutor(max_workers=2) as ex:
            futs = [ex.submit(N.ep_buffer, N.BufferRequest(content="same text", turn_id=t)) for t in turns]
            results = [f.result(timeout=30) for f in futs]
    c = D.get_connection(db)
    rows = c.execute("SELECT turn_id FROM memories").fetchall()
    c.close()
    return results, rows


class TestDedupConcurrent:
    def test_concurrent_same_turn_saved_once(self, tmp_path):
        results, rows = _run_two(tmp_path, ["same-turn", "same-turn"])
        assert len(rows) == 1
        assert sorted(r.get("reason", r["status"]) for r in results) == ["exact_duplicate", "ok"]

    def test_same_text_other_turn_still_saved(self, tmp_path):
        results, rows = _run_two(tmp_path, ["turn-a", "turn-b"])
        assert len(rows) == 2 and all(r["status"] == "ok" for r in results)

    def test_insert_memory_once_returns_none_for_a_duplicate(self, tmp_path):
        db = str(tmp_path / "once.db")
        c = D.get_connection(db)
        D.init_db(c)
        D.migrate_schema(c)
        assert D.insert_memory_once(c, "id-1", "text", "2026-11-17T00:00:00+00:00", "o", turn_id="t") is not None
        assert D.insert_memory_once(c, "id-2", "text", "2026-11-17T00:00:00+00:00", "o", turn_id="t") is None
        assert not c.in_transaction
        assert c.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        c.close()
