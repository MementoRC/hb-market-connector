"""Acceptance tests for issue #237: HBOrder.trigger (TriggerSpec) + validators.

Contract (issue #237):
- ``HBOrder.trigger: TriggerSpec | None`` (L0 ``data_type_primitives.TriggerSpec``).
- trigger only valid with a conditional OrderType; conditional types require a
  trigger (or the deprecated ``stop_price``); ``*_LIMIT`` types require ``price``.
- ``stop_price`` still accepted: mapped to an absolute-price TriggerSpec with a
  DeprecationWarning; setting both stop_price and trigger is a ValueError.
- HBOrder stays frozen and hashable.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from data_type_primitives import TriggerKind, TriggerReference, TriggerSpec
from pydantic import ValidationError

from market_connector.orders import HBOrder, OrderType, TradeType

STOP = Decimal("95.25")
LIMIT_PRICE = Decimal("94.75")
AMOUNT = Decimal("10")

# pydantic.ValidationError subclasses ValueError, so ``pytest.raises(ValueError)``
# is satisfied by model-validator failures.


def _absolute(value: Decimal = STOP) -> TriggerSpec:
    return TriggerSpec(kind=TriggerKind.ABSOLUTE, value=value)


def _order(order_type: OrderType, **fields: Any) -> HBOrder:
    return HBOrder(order_type=order_type, side=TradeType.SELL, amount=AMOUNT, **fields)


class TestTriggerAccepted:
    def test_default_trigger_is_none(self):
        assert _order(OrderType.MARKET).trigger is None

    def test_stop_loss_with_trigger_builds(self):
        trigger = _absolute()
        order = _order(OrderType.STOP_LOSS, trigger=trigger)
        assert order.trigger == trigger

    def test_stop_loss_limit_with_trigger_and_price_builds(self):
        order = _order(OrderType.STOP_LOSS_LIMIT, trigger=_absolute(), price=LIMIT_PRICE)
        assert order.trigger == _absolute()
        assert order.price == LIMIT_PRICE

    def test_non_default_reference_is_preserved(self):
        trigger = TriggerSpec(
            kind=TriggerKind.ABSOLUTE, value=STOP, reference=TriggerReference.MARK
        )
        order = _order(OrderType.STOP_LOSS, trigger=trigger)
        assert order.trigger is not None
        assert order.trigger.reference == TriggerReference.MARK

    def test_trigger_given_without_warning(self, recwarn: pytest.WarningsRecorder):
        _order(OrderType.STOP_LOSS, trigger=_absolute())
        assert not [w for w in recwarn if issubclass(w.category, DeprecationWarning)]


class TestTriggerRejectedOnNonConditional:
    @pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.LIMIT])
    def test_trigger_on_non_conditional_type_raises(self, order_type: OrderType):
        extra: dict[str, Any] = {"price": Decimal("100")} if order_type == OrderType.LIMIT else {}
        with pytest.raises(ValueError, match=r"(?i)trigger"):
            _order(order_type, trigger=_absolute(), **extra)

    def test_trigger_on_limit_maker_raises(self):
        with pytest.raises(ValueError, match=r"(?i)trigger"):
            _order(OrderType.LIMIT_MAKER, trigger=_absolute(), price=Decimal("100"))

    @pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.LIMIT])
    def test_stop_price_on_non_conditional_type_raises(self, order_type: OrderType):
        # IB used to reject this at transport time; it now fails at construction.
        extra: dict[str, Any] = {"price": Decimal("100")} if order_type == OrderType.LIMIT else {}
        with pytest.warns(DeprecationWarning), pytest.raises(ValueError, match=r"(?i)trigger"):
            _order(order_type, stop_price=STOP, **extra)


class TestConditionalRequiresTrigger:
    def test_stop_loss_without_trigger_or_stop_price_raises(self):
        with pytest.raises(ValueError, match=r"(?i)trigger"):
            _order(OrderType.STOP_LOSS)

    def test_stop_loss_limit_without_trigger_raises(self):
        with pytest.raises(ValueError, match=r"(?i)trigger"):
            _order(OrderType.STOP_LOSS_LIMIT, price=LIMIT_PRICE)


class TestLimitTypesRequirePrice:
    def test_stop_loss_limit_with_trigger_but_no_price_raises(self):
        with pytest.raises(ValueError, match=r"(?i)price"):
            _order(OrderType.STOP_LOSS_LIMIT, trigger=_absolute())

    def test_stop_loss_limit_with_stop_price_but_no_price_raises(self):
        with pytest.warns(DeprecationWarning), pytest.raises(ValueError, match=r"(?i)price"):
            _order(OrderType.STOP_LOSS_LIMIT, stop_price=STOP)


class TestStopPriceDeprecatedAlias:
    def test_stop_price_emits_deprecation_warning(self):
        with pytest.warns(DeprecationWarning, match=r"stop_price"):
            _order(OrderType.STOP_LOSS, stop_price=STOP)

    def test_stop_price_maps_to_absolute_trigger(self):
        with pytest.warns(DeprecationWarning):
            order = _order(OrderType.STOP_LOSS, stop_price=STOP)
        assert order.trigger == _absolute(STOP)

    def test_stop_price_maps_to_absolute_trigger_for_stop_limit(self):
        with pytest.warns(DeprecationWarning):
            order = _order(OrderType.STOP_LOSS_LIMIT, stop_price=STOP, price=LIMIT_PRICE)
        assert order.trigger == _absolute(STOP)

    def test_stop_price_trigger_has_last_reference_and_no_trail(self):
        with pytest.warns(DeprecationWarning):
            order = _order(OrderType.STOP_LOSS, stop_price=STOP)
        assert order.trigger is not None
        assert order.trigger.kind == TriggerKind.ABSOLUTE
        assert order.trigger.reference == TriggerReference.LAST
        assert order.trigger.trail is None

    def test_stop_price_order_equals_trigger_order(self):
        with pytest.warns(DeprecationWarning):
            legacy = _order(OrderType.STOP_LOSS, stop_price=STOP)
        modern = _order(OrderType.STOP_LOSS, trigger=_absolute(STOP))
        assert legacy.trigger == modern.trigger
        assert legacy.order_type == modern.order_type
        assert legacy.amount == modern.amount

    def test_both_stop_price_and_trigger_raises(self):
        with pytest.raises(ValueError, match=r"(?i)stop_price.*trigger|trigger.*stop_price"):
            _order(OrderType.STOP_LOSS, stop_price=STOP, trigger=_absolute())

    def test_both_raises_even_when_values_agree(self):
        with pytest.raises(ValueError, match=r"(?i)stop_price"):
            _order(OrderType.STOP_LOSS, stop_price=STOP, trigger=_absolute(STOP))


class TestConstructionFailsEarly:
    def test_invalid_order_fails_at_construction_not_transport(self):
        # Pydantic ValidationError is the ValueError subclass raised by model validators.
        with pytest.raises(ValidationError):
            _order(OrderType.STOP_LOSS)


class TestFrozenAndHashable:
    def test_assignment_raises(self):
        order = _order(OrderType.STOP_LOSS, trigger=_absolute())
        with pytest.raises(ValidationError):
            order.trigger = None  # type: ignore[misc]

    def test_assigning_amount_still_raises(self):
        order = _order(OrderType.STOP_LOSS, trigger=_absolute())
        with pytest.raises(ValidationError):
            order.amount = Decimal("1")  # type: ignore[misc]

    def test_equal_orders_hash_equal(self):
        a = _order(OrderType.STOP_LOSS_LIMIT, trigger=_absolute(), price=LIMIT_PRICE)
        b = _order(OrderType.STOP_LOSS_LIMIT, trigger=_absolute(), price=LIMIT_PRICE)
        assert a == b
        assert hash(a) == hash(b)

    def test_plain_order_remains_hashable(self):
        order = _order(OrderType.LIMIT, price=Decimal("100"))
        assert isinstance(hash(order), int)

    def test_orders_usable_in_set(self):
        a = _order(OrderType.STOP_LOSS, trigger=_absolute(STOP))
        b = _order(OrderType.STOP_LOSS, trigger=_absolute(STOP))
        c = _order(OrderType.STOP_LOSS, trigger=_absolute(Decimal("90")))
        assert len({a, b, c}) == 2
