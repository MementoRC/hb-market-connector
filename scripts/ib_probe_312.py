"""Paper-account probe for IB issues #312, #319, #326. Uses ib_async only.

Default is --read-only (places nothing). --place sends ONE small marketable order.
"""

import argparse
import asyncio
import contextlib
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, NamedTuple, TypedDict

from ib_async import IB, Execution, Fill, Forex, MarketOrder, Trade

T0 = time.monotonic()

TradePred = Callable[[Trade], bool]
FillPred = Callable[[Fill], bool]

# Query labels. bounded() appends a label to TIMED_OUT when that query times out.
EXEC_SAME_CONN = "reqExecutions (same conn)"
COMPLETED_AFTER = "reqCompletedOrders (after reconnect)"
EXEC_AFTER = "reqExecutions (after reconnect)"
TIMED_OUT: list[str] = []


class LiveState(TypedDict, total=False):
    execs: list[Execution]
    statuses: list[tuple[str, float, float]]
    lag_seen: bool
    max_cum: float
    last_filled: float
    trade: Trade
    order_id: int
    perm_id: int


class Matchers(NamedTuple):
    """Independent predicates: permId and orderRef are matched separately."""

    trade_perm: TradePred
    fill_perm: FillPred
    trade_ref: TradePred
    fill_ref: FillPred


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
    trades = await bounded(ib.reqCompletedOrdersAsync(apiOnly=True), "reqCompletedOrders")
    fills = await bounded(ib.reqExecutionsAsync(), "reqExecutions")
    print(f"-- reqCompletedOrders(apiOnly=True): {len(trades)} trades (last {limit})")
    for t in trades[-limit:]:
        print("  " + fmt_trade(t))
    print(f"-- reqExecutions: {len(fills)} fills (last {limit})")
    for f in fills[-limit:]:
        print("  " + fmt_fill(f))
    return trades, fills


async def cancel_and_wait(ib: IB, trade: Trade) -> None:
    ib.cancelOrder(trade.order)
    for _ in range(20):
        await asyncio.sleep(0.5)
        if trade.isDone():
            break
    print(f"[{ts()}] after cancel: status={trade.orderStatus.status}")
    if not trade.isDone():
        print(
            f"[{ts()}] WARNING: order {trade.order.orderId} still active after cancel; "
            "check and cancel it manually in the paper account"
        )


async def bounded(coro: Awaitable[list[Any]], what: str, timeout: float = 30) -> list[Any]:
    """Await an IB query with a timeout; report and return [] if it never completes."""
    try:
        return await asyncio.wait_for(coro, timeout)
    except TimeoutError:
        print(f"[{ts()}] WARNING: {what} timed out after {timeout:.0f}s")
        TIMED_OUT.append(what)
        return []


async def place_and_track(ib: IB, args: argparse.Namespace, account: str) -> LiveState:
    contract = make_contract(args.symbol)
    await ib.qualifyContractsAsync(contract)
    order = MarketOrder("BUY", args.qty)
    order.orderRef = args.order_ref
    order.account = account
    order.tif = "DAY"

    live: LiveState = {
        "execs": [],
        "statuses": [],
        "lag_seen": False,
        "max_cum": 0.0,
        "last_filled": 0.0,
    }

    def on_exec(trade: Trade, fill: Fill) -> None:
        if trade.order is not order:  # ignore other orders' events
            return
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
        if trade.order is not order:  # ignore other orders' events
            return
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
    try:
        deadline = time.monotonic() + 60
        while not trade.isDone() and time.monotonic() < deadline:
            await asyncio.sleep(0.25)
        await asyncio.sleep(1.0)  # let trailing events arrive
        if not trade.isDone():
            print(f"[{ts()}] TIMEOUT: cancelling order")
            await cancel_and_wait(ib, trade)
    except asyncio.CancelledError:
        # Interrupted while waiting: do not leave the DAY order working.
        if not trade.isDone():
            print(f"[{ts()}] INTERRUPTED: cancelling order")
            ib.cancelOrder(trade.order)
            # Give the cancel request time to reach the gateway before disconnecting.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(asyncio.sleep(1))
        ib.disconnect()
        raise
    finally:
        ib.execDetailsEvent -= on_exec
        ib.orderStatusEvent -= on_status
    live["trade"] = trade
    live["order_id"] = trade.order.orderId
    live["perm_id"] = trade.order.permId or (live["execs"][0].permId if live["execs"] else 0)
    return live


def match(order_ref: str, perm_id: int) -> Matchers:
    def trade_perm(t: Trade) -> bool:
        return perm_id != 0 and t.order.permId == perm_id

    def fill_perm(f: Fill) -> bool:
        return perm_id != 0 and f.execution.permId == perm_id

    def trade_ref(t: Trade) -> bool:
        return t.order.orderRef == order_ref

    def fill_ref(f: Fill) -> bool:
        return f.execution.orderRef == order_ref

    return Matchers(trade_perm, fill_perm, trade_ref, fill_ref)


def how(by_perm: bool, by_ref: bool) -> str:
    if by_perm and by_ref:
        return "both"
    return "permId" if by_perm else "orderRef"


def tagged[T](
    items: Sequence[T], by_perm: Callable[[T], bool], by_ref: Callable[[T], bool]
) -> list[tuple[T, str]]:
    """Return every item matched by either predicate, tagged with how it matched."""
    out: list[tuple[T, str]] = []
    for item in items:
        p, r = by_perm(item), by_ref(item)
        if p or r:
            out.append((item, how(p, r)))
    return out


def tally[T](
    label: str, items: Sequence[T], by_perm: Callable[[T], bool], by_ref: Callable[[T], bool]
) -> str:
    perm = [by_perm(x) for x in items]
    ref = [by_ref(x) for x in items]
    n_both = sum(p and r for p, r in zip(perm, ref, strict=True))
    return f"{label}: matched by permId: {sum(perm)}, by orderRef: {sum(ref)}, by both: {n_both}"


def verdict(found: bool, *deps: str) -> str:
    """YES if found; INCONCLUSIVE if not found and a dependent query timed out; else NO."""
    if found:
        return "YES"
    if any(d in TIMED_OUT for d in deps):
        return "INCONCLUSIVE"
    return "NO"


def print_summary(
    ref: str,
    live: LiveState,
    m: Matchers,
    same_fills: list[Fill],
    trades: list[Trade],
    fills: list[Fill],
) -> None:
    perm_id = live["perm_id"]
    execs = live["execs"]
    t_ref = [t for t in trades if m.trade_ref(t)]
    f_ref = [f for f in fills if m.fill_ref(f)]
    t_targets = tagged(trades, m.trade_perm, m.trade_ref)
    f_targets = tagged(fills, m.fill_perm, m.fill_ref)
    q1_live = bool(execs) and all(e.orderRef == ref for e in execs)
    q1_req = any(m.fill_ref(f) for f in same_fills)
    max_cum = max((f.execution.cumQty for f, _ in f_targets), default=None)

    print("\n=== SUMMARY ===")
    if TIMED_OUT:
        print(f"TIMED OUT (results below INCONCLUSIVE): {', '.join(TIMED_OUT)}")
    print(f"Q1 execDetails live carries orderRef: {verdict(q1_live)}")
    print(f"Q1 reqExecutions (same conn) carries orderRef: {verdict(q1_req, EXEC_SAME_CONN)}")
    print(
        f"Q1 reqExecutions (after reconnect) carries orderRef: {verdict(bool(f_ref), EXEC_AFTER)}"
    )
    print(
        f"Q2 completed found by orderRef: {verdict(bool(t_ref), COMPLETED_AFTER)} "
        f"status={[t.orderStatus.status for t in t_ref]} "
        f"filled={[t.orderStatus.filled for t in t_ref]} "
        f"orderRef={[t.order.orderRef for t in t_ref]}"
    )
    if perm_id == 0:
        perm_verdict = "INCONCLUSIVE (permId unknown)"
    else:
        perm_found = any(m.trade_perm(t) for t in trades)
        perm_verdict = verdict(perm_found, COMPLETED_AFTER)
    print(f"Q2 completed found by permId: {perm_verdict}")
    print(tally("completed orders", trades, m.trade_perm, m.trade_ref))
    print(tally("executions", fills, m.fill_perm, m.fill_ref))
    print(f"Q3 open-order orderStatus.filled lagged Execution.cumQty: {verdict(live['lag_seen'])}")
    for t, tag in t_targets:
        o = t.order
        print(
            f"Q4 completed [{tag}]: orderId={o.orderId} clientId={o.clientId} "
            f"permId={o.permId} orderRef={o.orderRef!r} "
            f"filled={t.orderStatus.filled} status={t.orderStatus.status}"
        )
    for f, tag in f_targets:
        e = f.execution
        print(
            f"Q4 execution [{tag}]: orderId={e.orderId} clientId={e.clientId} "
            f"permId={e.permId} orderRef={e.orderRef!r} cumQty={e.cumQty}"
        )
    if not t_targets and not f_targets:
        found = verdict(False, COMPLETED_AFTER, EXEC_AFTER)
        print(f"Q4 target found after reconnect: {found}")
    print(
        f"(max cumQty after reconnect={max_cum}, live orderId={live['order_id']}, permId={perm_id})"
    )


async def main_async(args: argparse.Namespace) -> int:
    if not args.place:
        ib = await connect(args, readonly=True)
        print(f"accounts={ib.managedAccounts()} (read-only mode, nothing placed)")
        await dump_existing(ib, args.limit)
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

    live = await place_and_track(ib, args, accounts[0])
    trade = live["trade"]
    print(f"[{ts()}] live final: {fmt_trade(trade)}")
    perm_id = live["perm_id"]
    m = match(args.order_ref, perm_id)

    # Same-connection reqExecutions (Q1 second half) before disconnecting.
    same_fills: list[Fill] = await bounded(ib.reqExecutionsAsync(), EXEC_SAME_CONN)
    print("-- reqExecutions (same connection), target:")
    for f, tag in tagged(same_fills, m.fill_perm, m.fill_ref):
        print(f"  [{tag}] {fmt_fill(f)}")

    ib.disconnect()
    await asyncio.sleep(2)
    print(f"[{ts()}] reconnecting as NEW IB() clientId={args.client_id}")
    ib2 = await connect(args, readonly=True)
    trades: list[Trade] = await bounded(ib2.reqCompletedOrdersAsync(apiOnly=True), COMPLETED_AFTER)
    fills: list[Fill] = await bounded(ib2.reqExecutionsAsync(), EXEC_AFTER)
    ib2.disconnect()
    print(f"-- after reconnect: completed={len(trades)} executions={len(fills)}")
    print("-- target completed order(s):")
    for t, tag in tagged(trades, m.trade_perm, m.trade_ref):
        print(f"  [{tag}] {fmt_trade(t)}")
    print("-- target execution(s):")
    for f, tag in tagged(fills, m.fill_perm, m.fill_ref):
        print(f"  [{tag}] {fmt_fill(f)}")

    print_summary(args.order_ref, live, m, same_fills, trades, fills)
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
    p.add_argument("--limit", type=int, default=10, help="rows per table in the read-only dump")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--read-only", action="store_true", help="default: place nothing")
    mode.add_argument("--place", action="store_true", help="place ONE small marketable order")
    p.add_argument("--yes", action="store_true", help="skip interactive confirmation for --place")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
