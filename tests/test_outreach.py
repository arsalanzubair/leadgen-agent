"""
N6a email outreach and N6b LinkedIn queue tests (Build step 7).

Nothing here sends a real email or queues a real LinkedIn action: every test
runs against the dry-run sender or a mocked provider. The behaviours that
matter are the ones that protect the operator -- daily caps, business hours,
the global kill switch, and the absolute rule that N6b never automates
LinkedIn.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.counters import Counter
from src.integrations import email_sender
from src.integrations.email_sender import (
    DailyLimitReached,
    DryRunSender,
    get_sender,
)
from src.nodes.n0_config_load import load_tenant_config
from src.nodes.n6a_email_outreach import (
    is_within_business_hours,
    n6a_email_outreach,
    next_open_window,
)
from src.nodes.n6b_linkedin_outreach import (
    all_items,
    enqueue,
    is_connection_accepted,
    mark,
    n6b_linkedin_outreach,
    pending_items,
)
from src.state import new_lead_state

EXAMPLE = "example_tenant"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNTER_DIR", str(tmp_path / "counters"))
    monkeypatch.setenv("SUPPRESSION_DIR", str(tmp_path / "suppression"))
    monkeypatch.setenv("GLOBAL_DRY_RUN", "false")   # tests opt in explicitly
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    load_tenant_config.cache_clear()
    yield
    load_tenant_config.cache_clear()


@pytest.fixture
def config():
    return load_tenant_config(EXAMPLE)


def cleared_lead(**overrides):
    channel = overrides.pop("channel", "email")
    base = dict(
        tenant_id=EXAMPLE, niche_id="local_dental", region="US",
        company_name="Lakeside Family Dentistry",
        contact_email="frontdesk@lakeside.com", contact_name="Dana Whitfield",
        dry_run=False,
    )
    base.update(overrides)
    state = new_lead_state(**base)
    state["channel"] = channel
    state["approval_status"] = "approved"
    state["suppression_status"] = "clear"
    state["fit_score"] = 82
    state["draft_message"] = {
        "email": {"subject": "your booking page", "body": "Hi Dana, ... unsubscribe"},
        "linkedin": {
            "connection_note": "Hi Dana - noticed your booking page is phone-only.",
            "followup_dm": "Thanks for connecting.",
        },
    }
    return state


# =========================================================================== #
# Business hours
# =========================================================================== #

def test_3am_local_is_outside_the_window(config):
    """The headline requirement: don't send at 3am local time."""
    at_3am_et = datetime(2026, 3, 3, 8, 0, tzinfo=timezone.utc)   # 03:00 New York
    ok, reason = is_within_business_hours(config, "US", at_3am_et)
    assert not ok
    assert "sending window" in reason


def test_midday_local_is_inside_the_window(config):
    midday_et = datetime(2026, 3, 3, 17, 0, tzinfo=timezone.utc)  # 12:00 New York
    ok, _ = is_within_business_hours(config, "US", midday_et)
    assert ok


def test_weekend_is_outside_the_window(config):
    saturday = datetime(2026, 3, 7, 15, 0, tzinfo=timezone.utc)
    ok, reason = is_within_business_hours(config, "UK", saturday)
    assert not ok
    assert "not a working day" in reason


def test_middle_east_works_sunday_to_thursday(config):
    """The ME profile's working week is Sun-Thu, not Mon-Fri."""
    sunday = datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc)     # 12:00 Dubai, Sunday
    friday = datetime(2026, 3, 6, 8, 0, tzinfo=timezone.utc)     # 12:00 Dubai, Friday
    assert is_within_business_hours(config, "ME", sunday)[0]
    assert not is_within_business_hours(config, "ME", friday)[0]


def test_first_and_last_hour_are_avoided(config):
    """cadences.yaml sets avoid_first_hour / avoid_last_hour."""
    # UK hours are 09-17, so 09:xx and 16:xx are trimmed.
    at_9 = datetime(2026, 3, 3, 9, 30, tzinfo=timezone.utc)
    at_11 = datetime(2026, 3, 3, 11, 30, tzinfo=timezone.utc)
    assert not is_within_business_hours(config, "UK", at_9)[0]
    assert is_within_business_hours(config, "UK", at_11)[0]


def test_next_open_window_lands_inside_the_window(config):
    saturday_3am = datetime(2026, 3, 7, 3, 0, tzinfo=timezone.utc)
    due = next_open_window(config, "UK", saturday_3am)
    assert due > saturday_3am
    assert is_within_business_hours(config, "UK", due)[0]


def test_every_region_has_a_reachable_window(config):
    start = datetime(2026, 3, 3, 3, 0, tzinfo=timezone.utc)
    for region in config.regions:
        due = next_open_window(config, region, start)
        assert is_within_business_hours(config, region, due)[0], region


# =========================================================================== #
# N6a
# =========================================================================== #

def test_out_of_hours_send_is_deferred_not_dropped(config, monkeypatch):
    monkeypatch.setattr(
        "src.nodes.n6a_email_outreach.is_within_business_hours",
        lambda cfg, region, when=None: (False, "3am local"),
    )
    update = n6a_email_outreach(cleared_lead(), tenant_config=config)
    assert update["send_status"] == "rate_limited"
    assert update["next_touch_due"] is not None
    assert "deferred" in update["next_action"]


def test_daily_limit_queues_rather_than_failing_the_batch(config, monkeypatch):
    monkeypatch.setattr(
        "src.nodes.n6a_email_outreach.is_within_business_hours",
        lambda cfg, region, when=None: (True, ""),
    )

    class Exhausted(DryRunSender):
        provider = "gmail_smtp"

        def check_budget(self):
            raise DailyLimitReached("quota 'sends__gmail_smtp' exhausted: 40/40")

    monkeypatch.setattr(email_sender, "get_sender", lambda *a, **k: Exhausted(EXAMPLE))
    monkeypatch.setattr(
        "src.nodes.n6a_email_outreach.sender_for", lambda *a, **k: Exhausted(EXAMPLE)
    )

    update = n6a_email_outreach(cleared_lead(), tenant_config=config)
    assert update["send_status"] == "rate_limited"
    assert "next run" in update["next_action"]
    assert not update.get("needs_manual_review"), "a full quota is not a failure"


def test_an_uncleared_lead_is_never_sent(config, monkeypatch):
    """Last-line safety: N6a re-checks that N5.5 actually cleared this lead."""
    monkeypatch.setattr(
        "src.nodes.n6a_email_outreach.is_within_business_hours",
        lambda cfg, region, when=None: (True, ""),
    )
    state = cleared_lead()
    state["suppression_status"] = "blocked_optout"

    def explode(*args, **kwargs):
        raise AssertionError("an uncleared lead must never reach the sender")

    monkeypatch.setattr("src.nodes.n6a_email_outreach.sender_for", explode)
    update = n6a_email_outreach(state, tenant_config=config)
    assert update["send_status"] == "not_sent"
    assert update["archived"] is True


def test_an_incomplete_draft_flags_manual_review(config):
    state = cleared_lead()
    state["draft_message"] = {"email": {"subject": "", "body": ""}}
    update = n6a_email_outreach(state, tenant_config=config)
    assert update["send_status"] == "failed"
    assert update["needs_manual_review"] is True


def test_dry_run_prints_and_sends_nothing(config, capsys):
    update = n6a_email_outreach(cleared_lead(dry_run=True), tenant_config=config)
    assert update["send_status"] == "sent"
    assert "nothing sent" in update["next_action"]
    captured = capsys.readouterr().out
    assert "DRY RUN" in captured
    assert "was NOT" in captured


def test_global_dry_run_overrides_everything(config, monkeypatch, capsys):
    """
    GLOBAL_DRY_RUN=true must stop a send even for a lead whose own dry_run flag
    is False -- it is the kill switch, not a default.
    """
    monkeypatch.setenv("GLOBAL_DRY_RUN", "true")

    def explode(*args, **kwargs):
        raise AssertionError("GLOBAL_DRY_RUN must prevent any real sender")

    monkeypatch.setattr(email_sender, "GmailSMTPSender", explode)
    update = n6a_email_outreach(cleared_lead(dry_run=False), tenant_config=config)
    assert update["send_status"] == "sent"
    assert "DRY RUN" in capsys.readouterr().out


def test_dry_run_does_not_consume_tomorrows_budget(config):
    counter = Counter("sends__gmail_smtp", cap=40, period="day", tenant_id=EXAMPLE)
    counter.reset()
    n6a_email_outreach(cleared_lead(dry_run=True), tenant_config=config)
    assert counter.used == 0


# =========================================================================== #
# The sender abstraction
# =========================================================================== #

def test_sender_falls_back_to_dry_run_without_credentials():
    sender = get_sender(EXAMPLE, provider="gmail_smtp")
    assert isinstance(sender, DryRunSender)


def test_tenant_limit_beats_the_provider_ceiling(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "x@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    sender = get_sender(EXAMPLE, provider="gmail_smtp", daily_limits={"gmail_smtp": 40})
    assert sender.daily_limit == 40, "the tenant's 40 must beat Gmail's 500"


def test_provider_ceiling_applies_when_the_tenant_asks_for_more(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "x@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    sender = get_sender(EXAMPLE, provider="gmail_smtp", daily_limits={"gmail_smtp": 5000})
    assert sender.daily_limit == 500


def test_send_counter_is_per_tenant_and_per_provider():
    a = Counter("sends__gmail_smtp", cap=10, period="day", tenant_id="tenant_a")
    b = Counter("sends__gmail_smtp", cap=10, period="day", tenant_id="tenant_b")
    c = Counter("sends__brevo", cap=10, period="day", tenant_id="tenant_a")
    a.reset(); b.reset(); c.reset()
    a.consume(3)
    assert a.used == 3
    assert b.used == 0, "one tenant must not eat another's visible budget"
    assert c.used == 0, "one provider must not eat another's budget"


def test_daily_counter_blocks_at_the_cap(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "x@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    sender = get_sender(EXAMPLE, provider="gmail_smtp", daily_limits={"gmail_smtp": 3})
    sender.counter.reset()
    for _ in range(3):
        sender.counter.consume(1)
    with pytest.raises(DailyLimitReached):
        sender.check_budget()


def test_a_failed_send_does_not_consume_budget(monkeypatch):
    """Counting before the send would leak budget on every transport failure."""
    monkeypatch.setenv("GMAIL_ADDRESS", "x@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "app-password")
    sender = get_sender(EXAMPLE, provider="gmail_smtp", daily_limits={"gmail_smtp": 10})
    sender.counter.reset()

    def explode(**kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(sender, "_send", explode)
    with pytest.raises(RuntimeError):
        sender.send(to="a@b.com", subject="s", body="b", from_name="X", from_email="x@y.com")
    assert sender.counter.used == 0


# =========================================================================== #
# N6b -- LinkedIn queue
# =========================================================================== #

def test_n6b_never_automates_linkedin():
    """
    The constraint, asserted structurally: nothing in the module may import a
    browser driver or an HTTP client aimed at linkedin.com.
    """
    import inspect

    from src.nodes import n6b_linkedin_outreach as module

    source = inspect.getsource(module)
    for banned in ("playwright", "selenium", "webdriver", "requests.post", "httpx"):
        assert banned not in source, f"N6b must not use {banned}"


def test_n6b_queues_instead_of_sending(config):
    state = cleared_lead(
        channel="linkedin", linkedin_url="https://linkedin.com/company/lakeside"
    )
    update = n6b_linkedin_outreach(state, tenant_config=config)
    assert update["send_status"] == "pending_manual_send"

    queued = pending_items(EXAMPLE)
    assert len(queued) == 1
    assert queued[0]["company_name"] == "Lakeside Family Dentistry"
    assert queued[0]["connection_note"]
    assert queued[0]["status"] == "pending"


def test_dry_run_prints_but_does_not_queue(config, capsys):
    state = cleared_lead(
        channel="linkedin", linkedin_url="https://linkedin.com/company/x", dry_run=True
    )
    update = n6b_linkedin_outreach(state, tenant_config=config)
    assert update["send_status"] == "pending_manual_send"
    assert "nothing queued" in update["next_action"]
    assert "DRY RUN" in capsys.readouterr().out
    assert pending_items(EXAMPLE) == [], "a dry run must not leave real work queued"


def test_an_uncleared_lead_is_never_queued(config):
    state = cleared_lead(
        channel="linkedin", linkedin_url="https://linkedin.com/company/x"
    )
    state["suppression_status"] = "blocked_optout"
    update = n6b_linkedin_outreach(state, tenant_config=config)
    assert update["send_status"] == "not_sent"
    assert pending_items(EXAMPLE) == []


def test_an_over_length_note_from_a_human_edit_is_caught(config):
    """N4 enforces 300 chars, but a human edit at N5 could reintroduce it."""
    state = cleared_lead(
        channel="linkedin", linkedin_url="https://linkedin.com/company/x"
    )
    state["draft_message"]["linkedin"]["connection_note"] = "x" * 400
    update = n6b_linkedin_outreach(state, tenant_config=config)
    assert update["send_status"] == "failed"
    assert "300" in update["manual_review_reason"]
    assert pending_items(EXAMPLE) == []


def test_daily_manual_cap_holds_the_rest(config):
    cap = config.daily_send_limits["linkedin_manual"]
    for i in range(cap):
        enqueue(EXAMPLE, {
            "lead_id": f"lead-{i}", "sequence_step": 1, "company_name": f"Co {i}",
            "linkedin_url": f"https://linkedin.com/company/{i}",
            "connection_note": "hi", "status": "pending",
        })
    state = cleared_lead(
        channel="linkedin", linkedin_url="https://linkedin.com/company/overflow"
    )
    update = n6b_linkedin_outreach(state, tenant_config=config)
    assert update["send_status"] == "rate_limited"
    assert "already queued today" in update["next_action"]


def test_marking_sent_removes_it_from_pending(config):
    state = cleared_lead(
        channel="linkedin", linkedin_url="https://linkedin.com/company/x"
    )
    n6b_linkedin_outreach(state, tenant_config=config)
    assert len(pending_items(EXAMPLE)) == 1

    mark(EXAMPLE, state["lead_id"], "sent")
    assert pending_items(EXAMPLE) == []
    assert all_items(EXAMPLE, status="sent")


def test_marking_connected_unblocks_the_followup_dm(config):
    state = cleared_lead(
        channel="linkedin", linkedin_url="https://linkedin.com/company/x"
    )
    n6b_linkedin_outreach(state, tenant_config=config)
    assert not is_connection_accepted(EXAMPLE, state["lead_id"])

    mark(EXAMPLE, state["lead_id"], "connected")
    assert is_connection_accepted(EXAMPLE, state["lead_id"])


def test_requeueing_never_resurrects_an_actioned_item(config):
    """Re-running a batch must not put an already-sent message back in the queue."""
    item = {
        "lead_id": "lead-1", "sequence_step": 1, "company_name": "Co",
        "linkedin_url": "https://linkedin.com/company/co",
        "connection_note": "hi", "status": "pending",
    }
    enqueue(EXAMPLE, item)
    mark(EXAMPLE, "lead-1", "sent")
    enqueue(EXAMPLE, dict(item))
    assert pending_items(EXAMPLE) == []


def test_the_queue_is_per_tenant(config):
    enqueue("tenant_a", {
        "lead_id": "x", "sequence_step": 1, "company_name": "A Co",
        "linkedin_url": "https://linkedin.com/company/a", "connection_note": "hi",
    })
    assert len(pending_items("tenant_a")) == 1
    assert pending_items("tenant_b") == []


def test_unknown_mark_status_is_rejected(config):
    with pytest.raises(ValueError, match="unknown status"):
        mark(EXAMPLE, "x", "delivered")
