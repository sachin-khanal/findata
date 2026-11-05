from datetime import UTC, datetime, timedelta

import pytest

from findata.daemon.grants import GRANTABLE, ApprovalStore, GrantStore


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def test_nothing_allowed_without_a_grant():
    store = GrantStore(FakeClock())
    assert store.current() is None
    for op in GRANTABLE:
        assert not store.allows(op)


def test_grant_allows_only_named_operations():
    store = GrantStore(FakeClock())
    out = store.grant({"sync"}, 60)
    assert out.operations == ["sync"]
    assert store.allows("sync")
    assert not store.allows("items.remove")


def test_grant_expires_at_the_exact_instant():
    clock = FakeClock()
    store = GrantStore(clock)
    out = store.grant({"sync"}, 60)
    assert out.expires_at == clock.now + timedelta(seconds=60)
    clock.advance(59)
    assert store.allows("sync")
    assert store.current() is not None
    clock.advance(1)
    assert not store.allows("sync")
    assert store.current() is None


def test_revoke_clears_immediately():
    store = GrantStore(FakeClock())
    store.grant({"sync", "link"}, 60)
    store.revoke()
    assert not store.allows("sync")
    assert store.current() is None


def test_new_grant_replaces_the_old_one():
    store = GrantStore(FakeClock())
    store.grant({"sync"}, 60)
    store.grant({"link"}, 60)
    assert store.allows("link")
    assert not store.allows("sync")


def test_unknown_operation_cannot_be_granted():
    store = GrantStore(FakeClock())
    with pytest.raises(ValueError):
        store.grant({"sync", "db.drop"}, 60)
    assert store.current() is None


def test_empty_operations_and_bad_ttl_rejected():
    store = GrantStore(FakeClock())
    with pytest.raises(ValueError):
        store.grant(set(), 60)
    with pytest.raises(ValueError):
        store.grant({"sync"}, 0)
    with pytest.raises(ValueError):
        store.grant({"sync"}, -5)


def test_approval_is_single_use():
    approvals = ApprovalStore(FakeClock())
    rid = approvals.request("items.remove", "item-a")
    assert not approvals.consume(rid, "items.remove", "item-a")  # not yet approved
    approvals.approve(rid)
    assert approvals.consume(rid, "items.remove", "item-a")
    assert not approvals.consume(rid, "items.remove", "item-a")


def test_approval_is_bound_to_operation_and_subject():
    approvals = ApprovalStore(FakeClock())
    rid = approvals.request("items.remove", "item-a")
    approvals.approve(rid)
    assert not approvals.consume(rid, "items.remove", "item-b")
    assert not approvals.consume(rid, "sync", "item-a")
    # wrong attempts did not burn the approval
    assert approvals.consume(rid, "items.remove", "item-a")


def test_approval_expires():
    clock = FakeClock()
    approvals = ApprovalStore(clock, ttl_s=300)
    rid = approvals.request("items.remove", "item-a")
    approvals.approve(rid)
    clock.advance(300)
    assert not approvals.consume(rid, "items.remove", "item-a")
    with pytest.raises(KeyError):
        approvals.approve(rid)


def test_approve_unknown_id_raises():
    with pytest.raises(KeyError):
        ApprovalStore(FakeClock()).approve("nope")


def test_pending_lists_unapproved_unexpired_only():
    clock = FakeClock()
    approvals = ApprovalStore(clock, ttl_s=300)
    a = approvals.request("items.remove", "item-a")
    b = approvals.request("items.remove", "item-b")
    approvals.approve(b)
    pending = approvals.pending()
    assert [p["request_id"] for p in pending] == [a]
    assert set(pending[0]) == {"request_id", "operation", "subject", "institution", "created_at"}
    clock.advance(300)
    assert approvals.pending() == []
