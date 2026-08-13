"""Delivery watermark shared by the durable worker and WhatsApp senders."""

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass
class DeliveryContext:
    conversation_id: int
    customer_phone: str
    generation: int
    worker_id: str
    attempted: bool = False
    delivered: bool = False
    stale_discarded: bool = False


current_delivery_context: ContextVar[Optional[DeliveryContext]] = ContextVar(
    "current_delivery_context", default=None
)
