"""market_connector.OrderOutcomeUnknownError derives from the dtp base (#234), RED."""

from __future__ import annotations

import pickle

import data_type_primitives as dtp
import pytest

from market_connector.exceptions import ExchangeUnavailableError, OrderOutcomeUnknownError


def test_is_subclass_of_dtp_and_exchange_unavailable() -> None:
    assert issubclass(OrderOutcomeUnknownError, dtp.OrderOutcomeUnknownError)
    assert issubclass(OrderOutcomeUnknownError, ExchangeUnavailableError)


def test_instance_passes_both_isinstance_checks() -> None:
    err = OrderOutcomeUnknownError(client_order_id="cid-1")

    assert isinstance(err, dtp.OrderOutcomeUnknownError)
    assert isinstance(err, ExchangeUnavailableError)


def test_dtp_except_clause_catches_it() -> None:
    with pytest.raises(dtp.OrderOutcomeUnknownError) as info:
        raise OrderOutcomeUnknownError(client_order_id="cid-2")

    assert info.value.client_order_id == "cid-2"


def test_exchange_unavailable_except_clause_still_catches_it() -> None:
    with pytest.raises(ExchangeUnavailableError):
        raise OrderOutcomeUnknownError(client_order_id="cid-3")


def test_default_message_construction_keeps_client_order_id() -> None:
    err = OrderOutcomeUnknownError(client_order_id="cid-4")

    assert err.client_order_id == "cid-4"
    assert "cid-4" in str(err)


def test_explicit_message_construction() -> None:
    err = OrderOutcomeUnknownError("response lost", client_order_id="cid-5")

    assert err.client_order_id == "cid-5"
    assert "response lost" in str(err)


def test_pickle_round_trip_preserves_type_message_and_client_order_id() -> None:
    err = OrderOutcomeUnknownError("response lost", client_order_id="cid-6")

    restored = pickle.loads(pickle.dumps(err))  # noqa: S301

    assert type(restored) is OrderOutcomeUnknownError
    assert str(restored) == str(err)
    assert restored.client_order_id == "cid-6"
    assert isinstance(restored, dtp.OrderOutcomeUnknownError)
