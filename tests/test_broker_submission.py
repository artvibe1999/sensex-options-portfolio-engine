import pytest

from sensex_engine.broker_submission import (
    BrokerOrderReceipt,
    BrokerOrderStatus,
    DurableBrokerSubmissionService,
)
from sensex_engine.execution import LegExecutionAction
from sensex_engine.order_lifecycle import OrderLifecycle, OrderState
from sensex_engine.persistence import SqliteStore


ACTION = LegExecutionAction(
    leg_index=0,
    instrument_token=123,
    tradingsymbol="SENSEX_TEST_CE",
    side="BUY",
    quantity=20,
)


def make_lifecycle(tmp_path, *, mode="live", database_name="broker.sqlite3"):
    lifecycle = OrderLifecycle(SqliteStore(tmp_path / database_name))
    lifecycle.create_intent(
        order_id="internal-1",
        position_id="position-1",
        leg_index=0,
        instrument_token=123,
        tradingsymbol="SENSEX_TEST_CE",
        side="BUY",
        quantity=20,
        order_type="LIMIT",
        product="NRML",
        mode=mode,
        idempotency_key="intent-1",
        client_order_key="sx-test-order-0001",
    )
    lifecycle.transition(
        order_id="internal-1",
        new_state=OrderState.VALIDATED,
        update_id="validated-1",
    )
    return lifecycle


class FakeBroker:
    def __init__(self, submission=None, lookups=()):
        self.submission = submission or BrokerOrderReceipt(
            "kite-123", BrokerOrderStatus.ACCEPTED
        )
        self.lookups = tuple(lookups)
        self.submit_calls = []
        self.lookup_calls = []

    def submit_order(self, action, *, client_order_key):
        self.submit_calls.append((action, client_order_key))
        if isinstance(self.submission, Exception):
            raise self.submission
        return self.submission

    def find_orders_by_client_key(self, client_order_key):
        self.lookup_calls.append(client_order_key)
        if isinstance(self.lookups, Exception):
            raise self.lookups
        return self.lookups


def test_submission_is_durably_claimed_before_single_broker_call(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker()
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )

    outcome = service.submit_once("internal-1", ACTION)
    assert outcome.state is OrderState.ACKNOWLEDGED
    assert not outcome.safe_to_advance
    assert len(broker.submit_calls) == 1
    assert broker.submit_calls[0][1] == "sx-test-order-0001"

    duplicate = service.submit_once("internal-1", ACTION)
    assert duplicate.state is OrderState.ACKNOWLEDGED
    assert len(broker.submit_calls) == 1


def test_live_submission_is_disabled_by_default(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker()
    service = DurableBrokerSubmissionService(lifecycle, broker)

    with pytest.raises(PermissionError, match="disabled"):
        service.submit_once("internal-1", ACTION)
    assert broker.submit_calls == []
    assert lifecycle.snapshot("internal-1")["state"] == "VALIDATED"


def test_paper_intent_cannot_cross_broker_submission_boundary(tmp_path):
    lifecycle = make_lifecycle(tmp_path, mode="paper")
    broker = FakeBroker()
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )

    with pytest.raises(PermissionError, match="LIVE intents"):
        service.submit_once("internal-1", ACTION)
    assert broker.submit_calls == []


def test_timeout_marks_unknown_and_restart_recovery_does_not_resubmit(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker(submission=TimeoutError("socket timed out"))
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )

    outcome = service.submit_once("internal-1", ACTION)
    assert outcome.state is OrderState.UNKNOWN
    assert len(broker.submit_calls) == 1

    restarted = OrderLifecycle(SqliteStore(tmp_path / "broker.sqlite3"))
    recovered_broker = FakeBroker(lookups=(
        BrokerOrderReceipt("kite-123", BrokerOrderStatus.OPEN),
    ))
    recovered = DurableBrokerSubmissionService(restarted, recovered_broker)

    result = recovered.recover("internal-1")
    assert result.state is OrderState.ACKNOWLEDGED
    assert result.broker_order_id == "kite-123"
    assert recovered_broker.submit_calls == []
    assert recovered_broker.lookup_calls == ["sx-test-order-0001"]


def test_no_lookup_match_remains_unknown_and_never_retries(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker(submission=TimeoutError())
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )
    service.submit_once("internal-1", ACTION)

    result = service.recover("internal-1")
    assert result.state is OrderState.UNKNOWN
    assert len(broker.submit_calls) == 1
    assert not result.safe_to_advance


def test_multiple_lookup_matches_require_manual_recovery(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker(submission=TimeoutError())
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )
    service.submit_once("internal-1", ACTION)
    broker.lookups = (
        BrokerOrderReceipt("kite-a", BrokerOrderStatus.OPEN),
        BrokerOrderReceipt("kite-b", BrokerOrderStatus.OPEN),
    )

    result = service.recover("internal-1")
    assert result.state is OrderState.RECOVERY_REQUIRED
    assert len(broker.submit_calls) == 1


def test_broker_rejection_is_persisted_as_rejected(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker(BrokerOrderReceipt(
        "kite-rejected", BrokerOrderStatus.REJECTED, "margin rejected"
    ))
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )

    result = service.submit_once("internal-1", ACTION)
    assert result.state is OrderState.REJECTED
    assert "rejected" in result.reason.lower()


def test_action_must_match_durable_intent(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker()
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )
    wrong_action = LegExecutionAction(0, 123, "SENSEX_TEST_CE", "SELL", 20)

    with pytest.raises(ValueError, match="does not match"):
        service.submit_once("internal-1", wrong_action)
    assert broker.submit_calls == []


def test_malformed_broker_reply_is_unknown_not_a_retry_signal(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker(submission=None)
    broker.submission = {"order_id": "not-a-receipt"}
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )

    result = service.submit_once("internal-1", ACTION)
    assert result.state is OrderState.UNKNOWN
    assert len(broker.submit_calls) == 1


def test_broker_lookup_exception_keeps_order_blocked(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker(submission=TimeoutError())
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )
    service.submit_once("internal-1", ACTION)
    broker.lookups = RuntimeError("broker API unavailable")

    result = service.recover("internal-1")
    assert result.state is OrderState.UNKNOWN
    assert len(broker.submit_calls) == 1


def test_acknowledgement_is_not_mistaken_for_a_fill(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker(BrokerOrderReceipt(
        "kite-open", BrokerOrderStatus.FILLED
    ))
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )

    result = service.submit_once("internal-1", ACTION)
    snapshot = lifecycle.snapshot("internal-1")
    assert result.state is OrderState.ACKNOWLEDGED
    assert snapshot["filled_quantity"] == 0
    assert snapshot["remaining_quantity"] == 20



def test_client_order_key_is_unique_across_durable_intents(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    with pytest.raises(ValueError, match="already assigned"):
        lifecycle.create_intent(
            order_id="internal-2",
            position_id="position-1",
            leg_index=1,
            instrument_token=124,
            tradingsymbol="SENSEX_TEST_PE",
            side="SELL",
            quantity=20,
            order_type="LIMIT",
            product="NRML",
            mode="live",
            idempotency_key="intent-2",
            client_order_key="sx-test-order-0001",
        )


def test_generated_client_key_is_stable_after_restart(tmp_path):
    lifecycle = OrderLifecycle(SqliteStore(tmp_path / "generated.sqlite3"))
    lifecycle.create_intent(
        order_id="generated-order",
        position_id="position-1",
        leg_index=0,
        instrument_token=123,
        tradingsymbol="SENSEX_TEST_CE",
        side="BUY",
        quantity=20,
        order_type="LIMIT",
        product="NRML",
        mode="live",
        idempotency_key="generated-intent",
    )
    original_key = lifecycle.snapshot("generated-order")["client_order_key"]
    restarted = OrderLifecycle(SqliteStore(tmp_path / "generated.sqlite3"))
    assert restarted.snapshot("generated-order")["client_order_key"] == original_key


def test_malformed_lookup_response_requires_manual_recovery(tmp_path):
    lifecycle = make_lifecycle(tmp_path)
    broker = FakeBroker(submission=TimeoutError())
    service = DurableBrokerSubmissionService(
        lifecycle, broker, live_submission_enabled=True
    )
    service.submit_once("internal-1", ACTION)
    broker.lookups = None

    result = service.recover("internal-1")
    assert result.state is OrderState.RECOVERY_REQUIRED
    assert len(broker.submit_calls) == 1
