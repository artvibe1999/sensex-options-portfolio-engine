from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sensex_engine.domain import Contract, OptionLeg, Position, Right, Side, TradingMode
from sensex_engine.execution import ExecutionPhase, OrderResult, OrderStatus
from sensex_engine.sequential_execution import PaperSequentialExecutionCoordinator


def make_position(mode=TradingMode.PAPER):
    expiry = datetime.now(UTC)
    long = Contract("LONG", 11, expiry, Decimal("80000"), Right.CE, 10, Decimal("0.05"))
    short = Contract("SHORT", 12, expiry, Decimal("80200"), Right.CE, 10, Decimal("0.05"))
    return Position.create(
        "test-spread",
        (OptionLeg(short, Side.SELL, 1), OptionLeg(long, Side.BUY, 1)),
        mode,
    )


class FakeSubmitter:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def submit_leg(self, action, *, client_order_key):
        self.calls.append((action, client_order_key))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def filled(order_id, quantity=10):
    return OrderResult(order_id, OrderStatus.FILLED, quantity, Decimal("100"))


def test_opening_waits_for_protective_long_full_fill_before_short():
    submitter = FakeSubmitter([filled("long"), filled("short")])
    report = PaperSequentialExecutionCoordinator(submitter).execute(
        make_position(), ExecutionPhase.OPEN
    )
    assert report.completed and not report.blocked
    assert [call[0].side for call in submitter.calls] == ["BUY", "SELL"]
    assert [outcome.confirmed_filled for outcome in report.outcomes] == [True, True]


@pytest.mark.parametrize(
    "first_result",
    [
        OrderResult("accepted", OrderStatus.ACCEPTED, 0, None),
        OrderResult("partial", OrderStatus.PARTIALLY_FILLED, 5, Decimal("100")),
        OrderResult("rejected", OrderStatus.REJECTED, 0, None),
    ],
)
def test_opening_stops_before_short_when_long_is_not_fully_filled(first_result):
    submitter = FakeSubmitter([first_result, filled("must-not-submit")])
    report = PaperSequentialExecutionCoordinator(submitter).execute(
        make_position(), ExecutionPhase.OPEN
    )
    assert report.blocked and not report.completed
    assert len(submitter.calls) == 1
    assert submitter.calls[0][0].side == "BUY"


def test_closing_buys_back_short_before_selling_long():
    submitter = FakeSubmitter([filled("cover"), filled("sell-long")])
    report = PaperSequentialExecutionCoordinator(submitter).execute(
        make_position(), ExecutionPhase.CLOSE
    )
    assert report.completed
    assert [call[0].side for call in submitter.calls] == ["BUY", "SELL"]


def test_closing_stops_before_selling_long_if_short_cover_not_fully_filled():
    submitter = FakeSubmitter([
        OrderResult("partial-cover", OrderStatus.PARTIALLY_FILLED, 5, Decimal("100")),
        filled("must-not-sell-long"),
    ])
    report = PaperSequentialExecutionCoordinator(submitter).execute(
        make_position(), ExecutionPhase.CLOSE
    )
    assert report.blocked
    assert len(submitter.calls) == 1


def test_uncertain_submitter_exception_halts_sequence():
    submitter = FakeSubmitter([TimeoutError(), filled("must-not-submit")])
    report = PaperSequentialExecutionCoordinator(submitter).execute(
        make_position(), ExecutionPhase.OPEN
    )
    assert report.blocked
    assert "uncertain" in report.outcomes[0].reason.lower()
    assert len(submitter.calls) == 1


def test_coordinator_refuses_live_position():
    submitter = FakeSubmitter([filled("unused")])
    with pytest.raises(PermissionError, match="paper-mode"):
        PaperSequentialExecutionCoordinator(submitter).execute(
            make_position(TradingMode.LIVE), ExecutionPhase.OPEN
        )
    assert submitter.calls == []


def test_full_fill_requires_matching_quantity_and_valid_price():
    submitter = FakeSubmitter([
        OrderResult("bad-fill", OrderStatus.FILLED, 9, Decimal("100"))
    ])
    report = PaperSequentialExecutionCoordinator(submitter).execute(
        make_position(), ExecutionPhase.OPEN
    )
    assert report.blocked and not report.completed
    assert len(submitter.calls) == 1


def test_malformed_submitter_response_halts_without_submitting_next_leg():
    submitter = FakeSubmitter([None, filled("must-not-submit")])
    report = PaperSequentialExecutionCoordinator(submitter).execute(
        make_position(), ExecutionPhase.OPEN
    )
    assert report.blocked and not report.completed
    assert report.outcomes[0].result is None
    assert len(submitter.calls) == 1
