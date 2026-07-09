"""Refund bookkeeping.

When a booking is cancelled a refund is calculated from its price and the
applicable notice tier, then written to the refund ledger with a processed
status. Amounts are stored in whole cents.
"""
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Booking, RefundLog


def log_refund(db: Session, booking: Booking, percent: int) -> RefundLog:
    # Exact half-up rounding to whole cents using integer arithmetic only
    # (built-in round() is banker's rounding; float math is lossy).
    amount_cents = (booking.price_cents * percent + 50) // 100
    entry = RefundLog(
        booking_id=booking.id,
        amount_cents=amount_cents,
        status="processed",
        processed_at=datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry
