"""Unit tests for Kraken Pydantic v2 schemas (Stage 3).

Validates that all schema models parse the recorded Stage 2 fixtures without
errors.  Also tests field coercions, type annotations, and the generic
KrakenResponse envelope.

Fixture loading uses the ``load_fixture`` fixture from conftest.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from market_connector.exchanges.kraken.schemas.enums import KrakenAPITier, KrakenOrderState
from market_connector.exchanges.kraken.schemas.rest import (
    AddOrderResult,
    AssetPairInfo,
    AssetPairsResult,
    BalanceResult,
    CancelOrderResult,
    KrakenResponse,
    WebSocketsTokenResult,
)
from market_connector.exchanges.kraken.schemas.ws import (
    BookSnapshot,
    Heartbeat,
    OwnTradesEvent,
    SubscriptionAck,
    SystemStatus,
    Trade,
    TradeEvent,
)

_FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


def _load_rest(name: str) -> Any:
    with (_FIXTURES_DIR / "rest" / name).open() as fh:
        return json.load(fh)


def _load_ws(name: str) -> Any:
    with (_FIXTURES_DIR / "ws" / name).open() as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# KrakenResponse envelope
# ---------------------------------------------------------------------------


class TestKrakenResponseEnvelope:
    def test_empty_error_list_success(self) -> None:
        raw = {"error": [], "result": {"ZUSD": "100.00"}}
        resp: KrakenResponse[BalanceResult] = KrakenResponse[BalanceResult].model_validate(raw)
        assert resp.error == []
        assert resp.result is not None

    def test_non_empty_error_list(self) -> None:
        raw = {"error": ["EOrder:Unknown order"], "result": None}
        resp: KrakenResponse[Any] = KrakenResponse[Any].model_validate(raw)
        assert resp.error == ["EOrder:Unknown order"]
        assert resp.result is None

    def test_missing_result_defaults_to_none(self) -> None:
        raw = {"error": ["EAPI:Invalid key"]}
        resp: KrakenResponse[Any] = KrakenResponse[Any].model_validate(raw)
        assert resp.result is None

    def test_missing_error_defaults_to_empty_list(self) -> None:
        raw = {"result": {"ZUSD": "50.00"}}
        resp: KrakenResponse[Any] = KrakenResponse[Any].model_validate(raw)
        assert resp.error == []


# ---------------------------------------------------------------------------
# REST fixture parsing
# ---------------------------------------------------------------------------


class TestBalanceFixture:
    def test_parse_balance_fixture(self, load_fixture) -> None:
        raw = load_fixture("rest", "balance.json")
        resp: KrakenResponse[BalanceResult] = KrakenResponse[BalanceResult].model_validate(raw)
        assert resp.error == []
        assert resp.result is not None
        result: BalanceResult = resp.result
        assert "ZUSD" in result
        assert "XXBT" in result
        assert float(result["ZUSD"]) > 0

    def test_balance_values_are_strings(self, load_fixture) -> None:
        raw = load_fixture("rest", "balance.json")
        resp: KrakenResponse[BalanceResult] = KrakenResponse[BalanceResult].model_validate(raw)
        assert resp.result is not None
        for asset, value in resp.result.items():
            assert isinstance(value, str), f"{asset}: balance should be str, got {type(value)}"


class TestAssetPairsFixture:
    def test_parse_asset_pairs_fixture(self, load_fixture) -> None:
        raw = load_fixture("rest", "asset_pairs.json")
        resp: KrakenResponse[AssetPairsResult] = KrakenResponse[AssetPairsResult].model_validate(
            raw
        )
        assert resp.error == []
        assert resp.result is not None
        assert "XXBTZUSD" in resp.result
        assert "XETHZUSD" in resp.result

    def test_asset_pair_fields(self, load_fixture) -> None:
        raw = load_fixture("rest", "asset_pairs.json")
        resp: KrakenResponse[AssetPairsResult] = KrakenResponse[AssetPairsResult].model_validate(
            raw
        )
        assert resp.result is not None
        xbt_usd: AssetPairInfo = resp.result["XXBTZUSD"]
        assert xbt_usd.altname == "XBTUSD"
        assert xbt_usd.wsname == "XBT/USD"
        assert xbt_usd.base == "XXBT"
        assert xbt_usd.quote == "ZUSD"


class TestAddOrderFixture:
    def test_parse_add_order_fixture(self, load_fixture) -> None:
        raw = load_fixture("rest", "add_order.json")
        resp: KrakenResponse[AddOrderResult] = KrakenResponse[AddOrderResult].model_validate(raw)
        assert resp.error == []
        assert resp.result is not None
        result: AddOrderResult = resp.result
        assert result.txid == ["OUF4EM-FRGI2-MQMWZD"]
        assert "buy" in result.descr.order

    def test_add_order_txid_is_list(self, load_fixture) -> None:
        raw = load_fixture("rest", "add_order.json")
        resp: KrakenResponse[AddOrderResult] = KrakenResponse[AddOrderResult].model_validate(raw)
        assert resp.result is not None
        assert isinstance(resp.result.txid, list)


class TestCancelOrderFixture:
    def test_parse_cancel_order_fixture(self, load_fixture) -> None:
        raw = load_fixture("rest", "cancel_order.json")
        resp: KrakenResponse[CancelOrderResult] = KrakenResponse[CancelOrderResult].model_validate(
            raw
        )
        assert resp.error == []
        assert resp.result is not None
        assert resp.result.count == 1


class TestWebSocketsTokenFixture:
    def test_parse_websockets_token_fixture(self, load_fixture) -> None:
        raw = load_fixture("rest", "get_websockets_token.json")
        resp: KrakenResponse[WebSocketsTokenResult] = KrakenResponse[
            WebSocketsTokenResult
        ].model_validate(raw)
        assert resp.error == []
        assert resp.result is not None
        assert resp.result.token == "NMxvFC0z8OtYhKmYL/5dXoP7iXrW2izLsogu4FUzBPg"
        assert resp.result.expires == 900


# ---------------------------------------------------------------------------
# WebSocket fixture parsing
# ---------------------------------------------------------------------------


class TestHeartbeatSchema:
    def test_parse_heartbeat(self) -> None:
        raw = _load_ws("heartbeat.json")
        hb = Heartbeat.model_validate(raw)
        assert hb.event == "heartbeat"


class TestSystemStatusSchema:
    def test_parse_system_status(self) -> None:
        raw = _load_ws("system_status.json")
        ss = SystemStatus.model_validate(raw)
        assert ss.event == "systemStatus"
        assert ss.status == "online"
        assert ss.version == "1.0.0"
        assert ss.connection_id == 8628615390848610222


class TestSubscriptionAckSchema:
    def test_parse_subscribe_ack(self) -> None:
        raw = _load_ws("subscribe_ack.json")
        ack = SubscriptionAck.model_validate(raw)
        assert ack.event == "subscriptionStatus"
        assert ack.status == "subscribed"
        assert ack.pair == "XBT/EUR"
        assert ack.channel_name == "ohlc-5"
        assert ack.subscription is not None
        assert ack.subscription.name == "ohlc"
        assert ack.subscription.interval == 5


class TestBookSnapshotSchema:
    def test_parse_book_snapshot_from_fixture(self) -> None:
        raw = _load_ws("book_snapshot.json")
        # raw is [channelID, {as: [...], bs: [...]}, "book-25", "XBT/USD"]
        payload = raw[1]
        snapshot = BookSnapshot.from_payload(payload)
        assert len(snapshot.asks) == 3
        assert len(snapshot.bids) == 3
        assert snapshot.asks[0].price == "5541.30000"
        assert snapshot.bids[0].price == "5541.20000"

    def test_book_level_price_volume(self) -> None:
        raw = _load_ws("book_snapshot.json")
        payload = raw[1]
        snapshot = BookSnapshot.from_payload(payload)
        first_ask = snapshot.asks[0]
        assert first_ask.price == "5541.30000"
        assert first_ask.volume == "2.50700000"
        assert first_ask.timestamp == "1534614248.123678"


class TestTradeEventSchema:
    def test_parse_trade_event_from_fixture(self) -> None:
        raw = _load_ws("trade_event.json")
        # raw is [channelID, [[price, vol, time, side, type, misc], ...], "trade", "XBT/USD"]
        event = TradeEvent.from_frame(raw)
        assert len(event.trades) == 2
        assert event.channel == "trade"
        assert event.pair == "XBT/USD"

    def test_trade_fields(self) -> None:
        raw = _load_ws("trade_event.json")
        event = TradeEvent.from_frame(raw)
        first = event.trades[0]
        assert first.price == "5541.20000"
        assert first.volume == "0.15850568"
        assert first.side == "s"  # sell
        assert first.order_type == "l"  # limit

    def test_trade_from_list(self) -> None:
        trade = Trade.from_list(["5541.20000", "0.15850568", "1534614057.321597", "s", "l", ""])
        assert trade.price == "5541.20000"
        assert trade.misc == ""


class TestOwnTradesSchema:
    def test_parse_own_trades_from_raw_frame(self) -> None:
        raw = _load_ws("own_trades.json")
        # raw is [[{txid: trade_detail}, ...], "ownTrades", {"sequence": N}]
        event = OwnTradesEvent.from_raw_frame(raw)
        assert len(event.trades) == 2
        assert event.sequence == 2948

    def test_own_trades_sequence_dict(self) -> None:
        """Last element is {"sequence": N} dict, not a string — known Kraken v1 quirk."""
        raw = _load_ws("own_trades.json")
        assert isinstance(raw[-1], dict)
        assert "sequence" in raw[-1]

    def test_own_trade_detail_fields(self) -> None:
        raw = _load_ws("own_trades.json")
        event = OwnTradesEvent.from_raw_frame(raw)
        detail = event.trades["TDLH43-DVQXD-2KHVYY"]
        assert detail.pair == "XBT/EUR"
        assert detail.type == "buy"


# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestKrakenAPITierEnum:
    def test_tier_values(self) -> None:
        assert KrakenAPITier.STARTER == "STARTER"
        assert KrakenAPITier.INTERMEDIATE == "INTERMEDIATE"
        assert KrakenAPITier.PRO == "PRO"

    def test_str_returns_value(self) -> None:
        assert str(KrakenAPITier.STARTER) == "STARTER"


class TestKrakenOrderStateEnum:
    def test_state_values(self) -> None:
        assert KrakenOrderState.PENDING == "pending"
        assert KrakenOrderState.OPEN == "open"
        assert KrakenOrderState.CLOSED == "closed"
        assert KrakenOrderState.CANCELED == "canceled"
        assert KrakenOrderState.EXPIRED == "expired"
