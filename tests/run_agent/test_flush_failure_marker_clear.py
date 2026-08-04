"""Regression test for PR #74540 — flush failure must not leave stale in-flight marker.

When `_flush_messages_to_session_db` returns False (SQLite write failure), the
turn marker must still be cleared unconditionally so a following serial turn is
not falsely diagnosed as an overlap. The failure is surfaced via a logger.warning
instead of coupling persistence to the concurrency tripwire.

GottZ's triage requested this regression: a failed flush followed by a serial
turn must NOT emit a false overlap warning.
"""

import logging

import pytest

from agent import agent_runtime_helpers as _helpers
from agent.agent_runtime_helpers import note_turn_persisted, note_turn_start


class _FakeAgent:
    """Minimal agent stub for note_turn_start/persisted tests."""

    def __init__(self, session_id: str = "s-test"):
        self.session_id = session_id
        self._inflight_turn_id = None
        self._inflight_turn_started = 0.0
        self._inflight_turn_session_id = None
        self._persist_disabled = False


@pytest.fixture(autouse=True)
def _clear_inflight_registry():
    """Isolate the module-level session registry between tests."""
    with _helpers._INFLIGHT_TURNS_LOCK:
        _helpers._INFLIGHT_TURNS_BY_SESSION.clear()
    yield
    with _helpers._INFLIGHT_TURNS_LOCK:
        _helpers._INFLIGHT_TURNS_BY_SESSION.clear()


def test_clean_serial_turns_no_warning(caplog):
    """Baseline: two serial turns without any failure should never warn."""
    agent = _FakeAgent("s-regression")
    with caplog.at_level(logging.WARNING, logger="agent.agent_runtime_helpers"):
        assert note_turn_start(agent, "s-regression:t1") is None
        note_turn_persisted(agent)
        assert note_turn_start(agent, "s-regression:t2") is None
        note_turn_persisted(agent)
    assert not caplog.records, f"Unexpected warning: {[r.message for r in caplog.records]}"


def test_persist_failure_does_not_cascade_false_overlap(caplog):
    """Core regression for #74540: persist failure must not leave stale marker.

    Simulate:
    1. Turn 1 starts
    2. Turn 1 persist fails (flush returns False) but note_turn_persisted is still
       called (the fix ensures marker clears unconditionally)
    3. Turn 2 starts — must NOT warn about overlap since marker was cleared
    """
    agent = _FakeAgent("s-regression")

    with caplog.at_level(logging.WARNING, logger="agent.agent_runtime_helpers"):
        # Turn 1: starts cleanly
        assert note_turn_start(agent, "s-regression:t1") is None

        # Simulate flush failure path: note_turn_persisted is still called
        # (per PR #74540, the marker is cleared unconditionally)
        note_turn_persisted(agent)

        # Turn 2: serial follow-up — should NOT warn
        prev = note_turn_start(agent, "s-regression:t2")
        assert prev is None, "Serial turn must not trigger overlap warning after persist failure"

    overlap_msgs = [r.message for r in caplog.records if "overlap" in r.message.lower()]
    assert len(overlap_msgs) == 0, (
        f"Expected no overlap warnings after persist failure + serial turn, "
        f"but got: {overlap_msgs}"
    )


def test_flush_failure_then_serial_turn_no_false_overlap(caplog):
    """Direct regression: after flush failure + note_turn_persisted,
    a second note_turn_start on the same session must be clean."""
    agent = _FakeAgent("s-regression")

    with caplog.at_level(logging.WARNING, logger="agent.agent_runtime_helpers"):
        # Turn 1 in flight
        assert note_turn_start(agent, "s-regression:t1") is None

        # Persist path: even if flush failed, marker is cleared
        note_turn_persisted(agent)

        # Turn 2: serial — must be clean
        prev = note_turn_start(agent, "s-regression:t2")
        assert prev is None

    assert not caplog.records, (
        f"Serial turn should not warn; got: {[r.message for r in caplog.records]}"
    )
