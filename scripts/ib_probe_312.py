"""Paper-account probe for IB issues #312, #319, #326. Uses ib_async only.

Default is --read-only (places nothing). --place sends ONE small marketable order.
"""

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import TypedDict

from ib_async import IB, Execution, Fill, Forex, MarketOrder, Trade

T0 = time.monotonic()

TradePred = Callable[[Trade], bool]
FillPred = Callable[[Fill], bool]


class LiveState(TypedDict, total=False):
    execs: list[Execution]
    statuses: list[tuple[str, float, float]]
    lag_seen: bool
    max_cum: float
    last_filled: float
    trade: Trade
    order_id: int
    perm_id: int


def ts() -> str:
    return f"{time.monotonic() - T0:7.3f}s"


def make_contract(symbol: str) -> Forex:
    base, _, quote = symbol.partition(".")
    if not quote:
        raise SystemExit(f"--symbol must look like EUR.USD, got {symbol!r}")
    return Forex(base + quote)


async def connect(args: argparse.Namespace, readonly: bool) -> IB:
    ib = IB()
    await ib.connectAsync(
        args.host, args.port, clientId=args.client_id, readonly=readonly, timeout=15
    )
    return ib


def fmt_trade(t: Trade) -> str:
    o, s = t.order, t.orderStatus
    return (
        f"orderId={o.orderId} clientId={o.clientId} permId={o.permId} orderRef={o.orderRef!r} "
        f"sym={t.contract.symbol} status={s.status!r} filled={s.filled!r} "
        f"remaining={s.remaining!r} fills={len(t.fills)}"
    )


def fmt_fill(f: Fill) -> str:
    e = f.execution
    return (
        f"execId={e.execId} orderId={e.orderId} clientId={e.clientId} permId={e.permId} "
        f"orderRef={e.orderRef!r} cumQty={e.cumQty} shares={e.shares} price={e.price}"
    )


async def dump_existing(ib: IB, limit: int = 10) -> tuple[list[Trade], list[Fill]]:
    trades = await ib.reqCompletedOrdersAsync(apiOnly=True)
    fills = await ib.reqExecutionsAsync()
    print(f"-- reqCompletedOrders(apiOnly=True): {len(trades)} trades (last {limit})")
    for t in trades[-limit:]:
        print("  " + fmt_trade(t))
    print(f"-- reqExecutions: {len(fills)} fills (last {limit})")
    for f in fills[-limit:]:
        print("  " + fmt_fill(f))
    return trades, fills


async def place_and_track(ib: IB, args: argparse.Namespace) -> LiveState:
    contract = make_contract(args.symbol)
    await ib.qualifyContractsAsync(contract)
    order = MarketOrder("BUY", args.qty)
    order.orderRef = args.order_ref
    order.tif = "DAY"

    live: LiveState = {
        "execs": [],
        "statuses": [],
        "lag_seen": False,
        "max_cum": 0.0,
        "last_filled": 0.0,
    }

    def on_exec(trade: Trade, fill: Fill) -> None:
        e = fill.execution
        live["execs"].append(e)
        live["max_cum"] = max(live["max_cum"], e.cumQty)
        lag = trade.orderStatus.filled < e.cumQty
        if lag:
            live["lag_seen"] = True
        print(
            f"[{ts()}] execDetails execId={e.execId} orderId={e.orderId} permId={e.permId} "
            f"orderRef={e.orderRef!r} cumQty={e.cumQty} shares={e.shares} price={e.price} "
            f"| orderStatus.filled_now={trade.orderStatus.filled} lag={lag}"
        )

    def on_status(trade: Trade) -> None:
        s = trade.orderStatus
        live["statuses"].append((s.status, s.filled, s.remaining))
        live["last_filled"] = s.filled
        lag = s.filled < live["max_cum"]
        if lag:
            live["lag_seen"] = True
        print(
            f"[{ts()}] orderStatus status={s.status} filled={s.filled} "
            f"remaining={s.remaining} lag_vs_cumQty={lag}"
        )

    ib.execDetailsEvent += on_exec
    ib.orderStatusEvent += on_status

    trade = ib.placeOrder(contract, order)
    print(f"[{ts()}] placed orderId={trade.order.orderId} orderRef={args.order_ref!r}")
    deadline = time.monotonic() + 60
    while not trade.isDone() and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
    await asyncio.sleep(1.0)  # let trailing events arrive
    if not trade.isDone():
        print(f"[{ts()}] TIMEOUT: cancelling order")
        ib.cancelOrder(trade.order)
        for _ in range(20):
            await asyncio.sleep(0.5)
            if trade.isDone():
                break
        print(f"[{ts()}] after cancel: status={trade.orderStatus.status}")
    ib.execDetailsEvent -= on_exec
    ib.orderStatusEvent -= on_status
    live["trade"] = trade
    live["order_id"] = trade.order.orderId
    live["perm_id"] = trade.order.permId or (live["execs"][0].permId if live["execs"] else 0)
    return live


def match(order_ref: str, perm_id: int, order_id: int) -> tuple[TradePred, FillPred]:
    def m_trade(t: Trade) -> bool:
        return t.order.orderRef == order_ref or (perm_id != 0 and t.order.permId == perm_id)

    def m_fill(f: Fill) -> bool:
        return f.execution.orderRef == order_ref or (perm_id != 0 and f.execution.permId == perm_id)

    return m_trade, m_fill


def yn(v: bool) -> str:
    return "YES" if v else "NO"


async def main_async(args: argparse.Namespace) -> int:
    if not args.place:
        ib = await connect(args, readonly=True)
        print(f"accounts={ib.managedAccounts()} (read-only mode, nothing placed)")
        await dump_existing(ib)
        ib.disconnect()
        return 0

    ib = await connect(args, readonly=False)
    accounts = ib.managedAccounts()
    print(f"account(s)={accounts}")
    if not accounts or not all(a.startswith("D") for a in accounts):
        print("REFUSING: account id does not start with 'D' (not a paper account)")
        ib.disconnect()
        return 2
    print(
        f"PLAN: BUY {args.qty} {args.symbol} MKT orderRef={args.order_ref!r} on {accounts[0]} "
        f"@ {args.host}:{args.port} clientId={args.client_id}; wait<=60s (cancel on timeout); "
        f"then reconnect same clientId and query completed orders + executions"
    )
    if not args.yes and input("Type 'yes' to proceed: ").strip().lower() != "yes":
        print("aborted")
        ib.disconnect()
        return 1

    live = await place_and_track(ib, args)
    trade = live["trade"]
    print(f"[{ts()}] live final: {fmt_trade(trade)}")
    order_id, perm_id = live["order_id"], live["perm_id"]

    # Same-connection reqExecutions (Q1 second half) before disconnecting.
    m_trade, m_fill = match(args.order_ref, perm_id, order_id)
    live_req = [f for f in await ib.reqExecutionsAsync() if m_fill(f)]
    print("-- reqExecutions (same connection), target:")
    for f in live_req:
        print("  " + fmt_fill(f))

    ib.disconnect()
    await asyncio.sleep(2)
    print(f"[{ts()}] reconnecting as NEW IB() clientId={args.client_id}")
    ib2 = await connect(args, readonly=True)
    trades = await ib2.reqCompletedOrdersAsync(apiOnly=True)
    fills = await ib2.reqExecutionsAsync()
    t_match = [t for t in trades if m_trade(t)]
    f_match = [f for f in fills if m_fill(f)]
    print(f"-- after reconnect: completed={len(trades)} executions={len(fills)}")
    print("-- target completed order(s):")
    for t in t_match:
        print("  " + fmt_trade(t))
    print("-- target execution(s):")
    for f in f_match:
        print("  " + fmt_fill(f))
    ib2.disconnect()

    execs = live["execs"]
    q1_live = bool(execs) and all(e.orderRef == args.order_ref for e in execs)
    q1_req = bool(live_req) and all(f.execution.orderRef == args.order_ref for f in live_req)
    q1_re = bool(f_match) and all(f.execution.orderRef == args.order_ref for f in f_match)
    q2_status = [t.orderStatus.status for t in t_match]
    q2_filled = [t.orderStatus.filled for t in t_match]
    q2_ref = [t.order.orderRef for t in t_match]
    max_cum = max((f.execution.cumQty for f in f_match), default=None)

    print("\n=== SUMMARY ===")
    print(f"Q1 execDetails live carries orderRef: {yn(q1_live)}")
    print(f"Q1 reqExecutions (same conn) carries orderRef: {yn(q1_req)}")
    print(f"Q1 reqExecutions (after reconnect) carries orderRef: {yn(q1_re)}")
    print(
        f"Q2 completed found: {yn(bool(t_match))} status={q2_status} "
        f"filled={q2_filled} orderRef={q2_ref}"
    )
    print(f"Q3 open-order orderStatus.filled lagged Execution.cumQty: {yn(live['lag_seen'])}")
    for t in t_match:
        o = t.order
        print(
            f"Q4 completed: orderId={o.orderId} clientId={o.clientId} "
            f"permId={o.permId} orderRef={o.orderRef!r} "
            f"filled={t.orderStatus.filled} status={t.orderStatus.status}"
        )
    for f in f_match:
        e = f.execution
        print(
            f"Q4 execution: orderId={e.orderId} clientId={e.clientId} "
            f"permId={e.permId} orderRef={e.orderRef!r} cumQty={e.cumQty}"
        )
    if not t_match and not f_match:
        print("Q4 nothing returned for target after reconnect")
    print(f"(max cumQty after reconnect={max_cum}, live orderId={order_id}, permId={perm_id})")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", default=os.environ.get("IB_GATEWAY_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("IB_GATEWAY_PORT", "4002")))
    p.add_argument("--client-id", type=int, default=int(os.environ.get("IB_CLIENT_ID", "958")))
    p.add_argument("--symbol", default="EUR.USD", help="forex pair on IDEALPRO, e.g. EUR.USD")
    p.add_argument(
        "--qty", type=float, default=20000, help="IDEALPRO minimum is typically 20000 base units"
    )
    p.add_argument("--order-ref", default=f"probe312-{datetime.now():%Y%m%d%H%M%S}")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--read-only", action="store_true", help="default: place nothing")
    mode.add_argument("--place", action="store_true", help="place ONE small marketable order")
    p.add_argument("--yes", action="store_true", help="skip interactive confirmation for --place")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
