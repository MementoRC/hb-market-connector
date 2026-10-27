"""Acceptance tests for derivative primitives (issue #27), core Pydantic API."""

from decimal import Decimal

import pytest
from data_type_primitives import PositionSide
from pydantic import ValidationError

from market_connector.primitives import (
    CancellationResult,
    FundingInfo,
    FundingInfoUpdate,
    Position,
)


def _funding_info() -> FundingInfo:
    return FundingInfo(
        trading_pair="BTC-USDT",
        index_price=Decimal("50000.5"),
        mark_price=Decimal("50010.25"),
        next_funding_utc_timestamp=1700000000,
        rate=Decimal("0.0001"),
    )


def _position() -> Position:
    return Position(
        trading_pair="ETH-USDT",
        position_side=PositionSide.LONG,
        unrealized_pnl=Decimal("-12.5"),
        entry_price=Decimal("3000.1"),
        amount=Decimal("2.5"),
        leverage=Decimal("10"),
    )


class TestFundingInfo:
    def test_create_with_keywords(self) -> None:
        info = _funding_info()
        assert info.trading_pair == "BTC-USDT"
        assert info.index_price == Decimal("50000.5")
        assert info.mark_price == Decimal("50010.25")
        assert info.next_funding_utc_timestamp == 1700000000
        assert info.rate == Decimal("0.0001")

    def test_frozen(self) -> None:
        info = _funding_info()
        with pytest.raises(ValidationError):
            info.rate = Decimal("0.5")

    def test_rejects_non_numeric_index_price(self) -> None:
        with pytest.raises(ValidationError):
            FundingInfo(
                trading_pair="BTC-USDT",
                index_price="not-a-number",
                mark_price=Decimal("1"),
                next_funding_utc_timestamp=1,
                rate=Decimal("0"),
            )

    def test_rejects_missing_field(self) -> None:
        with pytest.raises(ValidationError):
            FundingInfo(
                trading_pair="BTC-USDT",
                index_price=Decimal("1"),
                mark_price=Decimal("1"),
                rate=Decimal("0"),
            )

    def test_round_trip_python(self) -> None:
        info = _funding_info()
        assert FundingInfo.model_validate(info.model_dump()) == info

    def test_round_trip_json_preserves_decimal(self) -> None:
        info = FundingInfo(
            trading_pair="BTC-USDT",
            index_price=Decimal("1"),
            mark_price=Decimal("1"),
            next_funding_utc_timestamp=1,
            rate=Decimal("0.0001"),
        )
        restored = FundingInfo.model_validate_json(info.model_dump_json())
        assert restored == info
        assert str(restored.rate) == "0.0001"

    def test_hash_and_equality_by_value(self) -> None:
        first = _funding_info()
        second = _funding_info()
        assert first == second
        assert hash(first) == hash(second)
        assert len({first, second}) == 1

    def test_with_update_partial_only_rate(self) -> None:
        original = _funding_info()
        updated = original.with_update(
            FundingInfoUpdate(trading_pair="BTC-USDT", rate=Decimal("0.0005"))
        )
        assert updated is not original
        assert updated.rate == Decimal("0.0005")
        assert updated.index_price == original.index_price
        assert updated.mark_price == original.mark_price
        assert updated.next_funding_utc_timestamp == original.next_funding_utc_timestamp
        assert updated.trading_pair == original.trading_pair
        assert original.rate == Decimal("0.0001")

    def test_with_update_full(self) -> None:
        original = _funding_info()
        update = FundingInfoUpdate(
            trading_pair="BTC-USDT",
            index_price=Decimal("51000"),
            mark_price=Decimal("51001"),
            next_funding_utc_timestamp=1700028800,
            rate=Decimal("-0.0002"),
        )
        updated = original.with_update(update)
        assert updated == FundingInfo(
            trading_pair="BTC-USDT",
            index_price=Decimal("51000"),
            mark_price=Decimal("51001"),
            next_funding_utc_timestamp=1700028800,
            rate=Decimal("-0.0002"),
        )
        assert original == _funding_info()

    def test_with_update_all_none_equals_original(self) -> None:
        original = _funding_info()
        updated = original.with_update(FundingInfoUpdate(trading_pair="BTC-USDT"))
        assert updated == original

    def test_with_update_mismatched_pair_raises(self) -> None:
        original = _funding_info()
        with pytest.raises(ValueError):
            original.with_update(FundingInfoUpdate(trading_pair="ETH-USDT", rate=Decimal("0.1")))


class TestFundingInfoUpdate:
    def test_defaults_are_none(self) -> None:
        update = FundingInfoUpdate(trading_pair="BTC-USDT")
        assert update.index_price is None
        assert update.mark_price is None
        assert update.next_funding_utc_timestamp is None
        assert update.rate is None

    def test_frozen(self) -> None:
        update = FundingInfoUpdate(trading_pair="BTC-USDT")
        with pytest.raises(ValidationError):
            update.rate = Decimal("1")


class TestCancellationResult:
    def test_create_with_keywords(self) -> None:
        result = CancellationResult(order_id="o-1", success=True)
        assert result.order_id == "o-1"
        assert result.success is True

    def test_frozen(self) -> None:
        result = CancellationResult(order_id="o-1", success=True)
        with pytest.raises(ValidationError):
            result.success = False

    def test_rejects_missing_field(self) -> None:
        with pytest.raises(ValidationError):
            CancellationResult(order_id="o-1")  # type: ignore[call-arg]

    def test_round_trip_python(self) -> None:
        result = CancellationResult(order_id="o-1", success=False)
        assert CancellationResult.model_validate(result.model_dump()) == result

    def test_round_trip_json(self) -> None:
        result = CancellationResult(order_id="o-1", success=False)
        restored = CancellationResult.model_validate_json(result.model_dump_json())
        assert restored == result

    def test_hash_and_equality_by_value(self) -> None:
        first = CancellationResult(order_id="o-1", success=True)
        second = CancellationResult(order_id="o-1", success=True)
        assert first == second
        assert hash(first) == hash(second)


class TestPosition:
    def test_create_with_keywords(self) -> None:
        position = _position()
        assert position.trading_pair == "ETH-USDT"
        assert position.position_side == PositionSide.LONG
        assert position.unrealized_pnl == Decimal("-12.5")
        assert position.entry_price == Decimal("3000.1")
        assert position.amount == Decimal("2.5")
        assert position.leverage == Decimal("10")

    def test_frozen(self) -> None:
        position = _position()
        with pytest.raises(ValidationError):
            position.amount = Decimal("9")

    def test_rejects_position_side_outside_enum(self) -> None:
        with pytest.raises(ValidationError):
            Position(
                trading_pair="ETH-USDT",
                position_side="SIDEWAYS",
                unrealized_pnl=Decimal("0"),
                entry_price=Decimal("1"),
                amount=Decimal("1"),
                leverage=Decimal("1"),
            )

    def test_rejects_non_numeric_leverage(self) -> None:
        with pytest.raises(ValidationError):
            Position(
                trading_pair="ETH-USDT",
                position_side=PositionSide.SHORT,
                unrealized_pnl=Decimal("0"),
                entry_price=Decimal("1"),
                amount=Decimal("1"),
                leverage="lots",
            )

    def test_rejects_missing_field(self) -> None:
        with pytest.raises(ValidationError):
            Position(
                trading_pair="ETH-USDT",
                position_side=PositionSide.LONG,
                unrealized_pnl=Decimal("0"),
                entry_price=Decimal("1"),
                amount=Decimal("1"),
            )

    def test_round_trip_python(self) -> None:
        position = _position()
        assert Position.model_validate(position.model_dump()) == position

    def test_round_trip_json_preserves_decimal(self) -> None:
        position = Position(
            trading_pair="ETH-USDT",
            position_side=PositionSide.BOTH,
            unrealized_pnl=Decimal("0.0001"),
            entry_price=Decimal("0.0001"),
            amount=Decimal("0.0001"),
            leverage=Decimal("0.0001"),
        )
        restored = Position.model_validate_json(position.model_dump_json())
        assert restored == position
        assert str(restored.unrealized_pnl) == "0.0001"
        assert str(restored.entry_price) == "0.0001"
        assert str(restored.amount) == "0.0001"
        assert str(restored.leverage) == "0.0001"

    def test_hash_and_equality_by_value(self) -> None:
        first = _position()
        second = _position()
        assert first == second
        assert hash(first) == hash(second)
