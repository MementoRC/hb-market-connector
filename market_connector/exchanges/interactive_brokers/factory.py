"""build_ib_gateway() factory: wires transport, signer, resolver."""

from __future__ import annotations

from typing import TYPE_CHECKING

from market_connector.auth.passthrough import PassThroughSigner
from market_connector.exchanges.interactive_brokers.contract_resolver import IbContractResolver
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

if TYPE_CHECKING:
    from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec


def build_ib_gateway(spec: IbConnectionSpec) -> IbGatewayGateway:
    """Construct an IbGatewayGateway with all Stage 2 dependencies wired up.

    Stage 2: transport + PassThroughSigner + IbContractResolver(transport).
    The resolver wraps the same transport instance stored in unified_transport
    so that callers can verify identity if needed.
    """
    transport = IbGatewayTransport(spec)
    resolver = IbContractResolver(transport)
    return IbGatewayGateway(
        transport=transport,
        signer=PassThroughSigner(),
        contract_resolver=resolver,
    )
