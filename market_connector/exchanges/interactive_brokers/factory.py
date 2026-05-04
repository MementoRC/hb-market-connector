"""build_ib_gateway() factory: wires transport, signer, resolver."""

from __future__ import annotations

from typing import TYPE_CHECKING

from market_connector.auth.passthrough import PassThroughSigner
from market_connector.exchanges.interactive_brokers.ib_gateway import IbGatewayGateway
from market_connector.exchanges.interactive_brokers.transport import IbGatewayTransport

if TYPE_CHECKING:
    from market_connector.exchanges.interactive_brokers.specs import IbConnectionSpec


def build_ib_gateway(spec: IbConnectionSpec) -> IbGatewayGateway:
    """Construct an IbGatewayGateway with all Stage 1 dependencies wired up.

    Stage 1: transport + PassThroughSigner; contract_resolver=None.
    Stage 2 will inject IbContractResolver(transport).
    """
    transport = IbGatewayTransport(spec)
    return IbGatewayGateway(
        transport=transport,
        signer=PassThroughSigner(),
        contract_resolver=None,
    )
