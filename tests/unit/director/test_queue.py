"""Tests for DirectorQueue: enqueue, peek, ack, dedup, resolve, persistence."""

from __future__ import annotations

import time

from worldseed.engine.director.models import DirectorSignal, PendingDMRequest
from worldseed.engine.director.queue import DirectorQueue


def _signal(
    *,
    sid: str,
    type_: str = "urgent",
    target: str | None = None,
    event_ref: str | None = None,
    reason: str = "",
) -> DirectorSignal:
    refs: dict[str, object] = {}
    if event_ref is not None:
        refs["event_ref"] = event_ref
    return DirectorSignal(
        id=sid,
        type=type_,  # type: ignore[arg-type]
        tick=1,
        created_at=time.time(),
        reason=reason,
        target_agent_id=target,
        refs=refs,
    )


def _dm_request(rid: str, source_name: str = "say") -> PendingDMRequest:
    return PendingDMRequest(
        id=rid,
        source_type="action",
        source_name=source_name,
        actor_agent_id="alice",
        tick=1,
        dm_config={},
        action={"action_type": "say"},
        ctx={},
        dm_context={},
    )


class TestSignalEnqueueAndPeek:
    def test_enqueue_then_peek_pending(self) -> None:
        q = DirectorQueue()
        s1 = _signal(sid="s1", target="a", event_ref="e1")
        assert q.enqueue_signal(s1) is True
        out = q.peek_pending()
        assert [s.id for s in out] == ["s1"]

    def test_peek_filters_by_type(self) -> None:
        q = DirectorQueue()
        q.enqueue_signal(_signal(sid="s1", type_="urgent", target="a", event_ref="e1"))
        q.enqueue_signal(_signal(sid="s2", type_="checkpoint", reason="cadence"))
        urgents = q.peek_pending(types=["urgent"])
        assert [s.id for s in urgents] == ["s1"]

    def test_peek_respects_limit(self) -> None:
        q = DirectorQueue()
        for i in range(5):
            q.enqueue_signal(_signal(sid=f"s{i}", target=f"a{i}", event_ref=f"e{i}"))
        out = q.peek_pending(limit=2)
        assert len(out) == 2

    def test_peek_does_not_drain(self) -> None:
        q = DirectorQueue()
        q.enqueue_signal(_signal(sid="s1", target="a", event_ref="e1"))
        q.peek_pending()
        assert len(q.peek_pending()) == 1


class TestUrgentDedup:
    def test_same_target_and_event_ref_deduped(self) -> None:
        q = DirectorQueue()
        assert q.enqueue_signal(_signal(sid="s1", target="a", event_ref="e1")) is True
        assert q.enqueue_signal(_signal(sid="s2", target="a", event_ref="e1")) is False
        assert len(q.peek_pending()) == 1

    def test_different_target_not_deduped(self) -> None:
        q = DirectorQueue()
        q.enqueue_signal(_signal(sid="s1", target="a", event_ref="e1"))
        assert q.enqueue_signal(_signal(sid="s2", target="b", event_ref="e1")) is True

    def test_after_ack_re_enqueue_allowed(self) -> None:
        q = DirectorQueue()
        q.enqueue_signal(_signal(sid="s1", target="a", event_ref="e1"))
        assert q.ack_signal("s1") is True
        # Now a new urgent for the same agent + ref should be accepted because
        # nothing pending blocks it.
        assert q.enqueue_signal(_signal(sid="s2", target="a", event_ref="e1")) is True


class TestCheckpointDedup:
    def test_only_one_pending_checkpoint(self) -> None:
        q = DirectorQueue()
        assert q.enqueue_signal(_signal(sid="c1", type_="checkpoint")) is True
        assert q.enqueue_signal(_signal(sid="c2", type_="checkpoint")) is False
        assert len(q.peek_pending(types=["checkpoint"])) == 1

    def test_after_ack_new_checkpoint_allowed(self) -> None:
        q = DirectorQueue()
        q.enqueue_signal(_signal(sid="c1", type_="checkpoint"))
        q.ack_signal("c1")
        assert q.enqueue_signal(_signal(sid="c2", type_="checkpoint")) is True


class TestDMRequestNoDedup:
    def test_two_dm_requests_with_same_source_kept(self) -> None:
        q = DirectorQueue()
        q.enqueue_dm_request(_dm_request("r1"))
        q.enqueue_dm_request(_dm_request("r2"))
        assert q.pending_dm_count() == 2

    def test_resolve_marks_resolved(self) -> None:
        q = DirectorQueue()
        q.enqueue_dm_request(_dm_request("r1"))
        ok = q.resolve_dm_request("r1", {"narrative": "ok", "effects": []})
        assert ok is True
        req = q.get_dm_request("r1")
        assert req is not None
        assert req.status == "resolved"

    def test_fail_marks_failed(self) -> None:
        q = DirectorQueue()
        q.enqueue_dm_request(_dm_request("r1"))
        assert q.fail_dm_request("r1", "queue_full") is True
        req = q.get_dm_request("r1")
        assert req is not None
        assert req.status == "failed"


class TestSignalLinkedToDMRequest:
    def test_resolve_dm_marks_signal_resolved(self) -> None:
        q = DirectorQueue()
        q.enqueue_dm_request(_dm_request("r1"))
        sig = DirectorSignal(
            id="s1",
            type="dm_request",
            tick=1,
            created_at=time.time(),
            refs={"dm_request_id": "r1"},
        )
        q.enqueue_signal(sig)
        q.resolve_dm_request("r1", {"narrative": "ok", "effects": []})
        assert q.get_signal("s1") is not None
        assert q.get_signal("s1").status == "resolved"  # type: ignore[union-attr]

    def test_ack_rejects_dm_request_signal(self) -> None:
        q = DirectorQueue()
        sig = DirectorSignal(
            id="s1",
            type="dm_request",
            tick=1,
            created_at=time.time(),
            refs={"dm_request_id": "r1"},
        )
        q.enqueue_signal(sig)
        assert q.ack_signal("s1") is False


class TestPersistenceRoundtrip:
    def test_roundtrip_preserves_state(self) -> None:
        q = DirectorQueue()
        q.enqueue_signal(_signal(sid="s1", target="a", event_ref="e1"))
        q.enqueue_signal(_signal(sid="c1", type_="checkpoint"))
        q.enqueue_dm_request(_dm_request("r1"))
        q.ack_signal("s1")

        snapshot = q.to_dict()
        restored = DirectorQueue()
        restored.restore(snapshot)

        assert restored.get_signal("s1") is not None
        assert restored.get_signal("s1").status == "acked"  # type: ignore[union-attr]
        assert len(restored.peek_pending(types=["checkpoint"])) == 1
        assert restored.get_dm_request("r1") is not None
