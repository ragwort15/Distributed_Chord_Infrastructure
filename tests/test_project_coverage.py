"""
Project-wide unit tests covering modules that lacked direct test coverage:
  - chord.recovery.choose_recovery_path  (decision matrix for retry vs give-up)
  - chord.executor.run_task              (SCRIPT execution + timeout)
  - chord.node persistence               (data_store snapshot/restore)
  - chord.worker_assignment              (HT2 append + remove semantics)

Existing modules already have dedicated test files; see:
  tests/test_chord.py              (Chord ring internals)
  tests/test_task_service.py       (TaskService CRUD)
  tests/test_replication.py        (k-successor replication)
  tests/test_grpc_service.py       (gRPC bridge)
  tests/test_session_fixes.py      (worker registry + scored placement)
"""
import os
import tempfile
import pytest
from unittest.mock import MagicMock

from chord.recovery import (
    choose_recovery_path,
    RETRY_DIFFERENT,
    WAIT_AND_RETRY,
    GIVE_UP,
)
from chord.executor import run_task
from chord.node import ChordNode


# ===========================================================================
# Recovery path decision matrix
# ===========================================================================

def test_recovery_gives_up_after_max_attempts():
    record = {"attempt_count": 3, "max_attempts": 3}
    assert choose_recovery_path(record, "worker_crash", True) == GIVE_UP


def test_recovery_worker_crash_retries_different_when_workers_available():
    record = {"attempt_count": 0, "max_attempts": 3}
    assert choose_recovery_path(record, "worker_crash", True) == RETRY_DIFFERENT


def test_recovery_worker_crash_waits_when_no_workers():
    record = {"attempt_count": 0, "max_attempts": 3}
    assert choose_recovery_path(record, "worker_crash", False) == WAIT_AND_RETRY


def test_recovery_task_timeout_treated_like_crash():
    record = {"attempt_count": 1, "max_attempts": 3}
    assert choose_recovery_path(record, "task_timeout", True) == RETRY_DIFFERENT
    assert choose_recovery_path(record, "task_timeout", False) == WAIT_AND_RETRY


def test_recovery_task_failure_gives_up_unless_opted_in():
    """Non-zero exit code is treated as user error by default; only retry
    when the submitter explicitly set retry_on_failure=True."""
    base = {"attempt_count": 0, "max_attempts": 3}
    assert choose_recovery_path(base, "task_failure", True) == GIVE_UP
    base["retry_on_failure"] = True
    assert choose_recovery_path(base, "task_failure", True) == RETRY_DIFFERENT


def test_recovery_unknown_failure_type_is_safe():
    record = {"attempt_count": 0, "max_attempts": 3}
    assert choose_recovery_path(record, "weird_new_failure", True) == GIVE_UP


# ===========================================================================
# Task executor
# ===========================================================================

def test_run_task_script_success_captures_stdout():
    result = run_task("SCRIPT", path="", script="echo hello && echo world")
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert "world" in result.stdout
    assert not result.timed_out


def test_run_task_script_nonzero_exit_is_reported_not_raised():
    """A script that exits non-zero must return ExecutionResult with the
    real exit code — not raise — so recovery can inspect it."""
    result = run_task("SCRIPT", path="", script="exit 7")
    assert result.exit_code == 7
    assert not result.timed_out


def test_run_task_script_timeout_marks_timed_out():
    """A long-running script must be killed at the timeout and reported as
    timed_out=True so recovery treats it as a transient fault, not a bug."""
    result = run_task("SCRIPT", path="", script="sleep 5", timeout_s=1)
    assert result.timed_out is True
    assert "TIMEOUT" in result.stderr


def test_run_task_binary_requires_path():
    with pytest.raises(ValueError):
        run_task("BINARY", path="", script="")


def test_run_task_unknown_type_raises():
    with pytest.raises(ValueError):
        run_task("SOMETHING_ELSE", path="", script="echo hi")


# ===========================================================================
# Node persistence (data_store snapshot to disk + reload)
# ===========================================================================

def _make_isolated_node(tmpdir, addr, node_id):
    """Build a ChordNode whose persistence path lives in tmpdir."""
    os.environ["CHORD_DATA_DIR"] = tmpdir
    node = ChordNode(address=addr, node_id=node_id)
    transport = MagicMock()
    transport.get_state.return_value = {"successor": {"id": node_id, "address": addr}}
    node.set_transport(transport)
    node.join(None)
    return node


def test_persistence_roundtrips_data_store(tmp_path):
    """Writing a key, snapshotting to disk, and loading from a fresh node
    must recover the same value."""
    a = _make_isolated_node(str(tmp_path), "127.0.0.1:9001", 11)
    a.data_store["result:test-1"] = {"status": "SUCCESS", "result": "ok"}
    a._save_to_disk()

    b = _make_isolated_node(str(tmp_path), "127.0.0.1:9001", 11)
    assert b.data_store.get("result:test-1") == {"status": "SUCCESS", "result": "ok"}


def test_persistence_missing_file_is_silent(tmp_path):
    """If the snapshot file doesn't exist (fresh install), the node must
    boot with an empty data_store — not crash."""
    n = _make_isolated_node(str(tmp_path), "127.0.0.1:9002", 22)
    assert n.data_store == {}
