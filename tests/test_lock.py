"""The run lock: one scheduled or manual run per database at a time."""
from __future__ import annotations

import json
import os
import time

import pytest

from gitscout.lock import RunLocked, lock_path, run_lock


def test_a_second_run_is_refused_while_the_first_holds_the_lock(tmp_path):
    db = tmp_path / "g.db"
    with run_lock(db):
        assert lock_path(db).exists()
        with pytest.raises(RunLocked, match="another gitscout run"):
            with run_lock(db):
                pass
    assert not lock_path(db).exists()


def test_the_lock_is_released_even_when_the_run_fails(tmp_path):
    db = tmp_path / "g.db"
    with pytest.raises(ValueError):
        with run_lock(db):
            raise ValueError("boom")
    assert not lock_path(db).exists()
    with run_lock(db):  # and can be taken again
        pass


def test_a_lock_abandoned_by_a_crashed_run_is_taken_over(tmp_path):
    db = tmp_path / "g.db"
    path = lock_path(db)
    path.write_text('{"pid": 999999, "started": 0}', encoding="utf-8")
    old = time.time() - 7 * 3600
    os.utime(path, (old, old))

    with run_lock(db):  # older than the stale window: taken, not refused
        pass
    assert not path.exists()


def test_a_lock_whose_process_died_is_taken_over_immediately(tmp_path):
    """A killed run never deletes its lock. Without the liveness check this one --
    one minute old -- would block every run for six hours."""
    db = tmp_path / "g.db"
    path = lock_path(db)
    path.write_text('{"pid": 999999, "started": 0}', encoding="utf-8")  # no such process

    with run_lock(db):
        pass
    assert not path.exists()


def test_a_live_holder_is_respected(tmp_path):
    db = tmp_path / "g.db"
    lock_path(db).write_text(json.dumps({"pid": os.getpid(), "started": time.time()}),
                             encoding="utf-8")
    with pytest.raises(RunLocked):
        with run_lock(db):
            pass


def test_liveness_never_harms_the_process_it_checks():
    from gitscout.lock import pid_alive

    assert pid_alive(os.getpid()) is True   # still here afterwards -- not killed
    assert pid_alive(os.getpid()) is True
    assert pid_alive(999999) is False
    assert pid_alive(0) is False
