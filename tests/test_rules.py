from __future__ import annotations

from datetime import UTC, datetime

from student_agent import rules

DEFAULT_LIMIT = datetime.fromisoformat("2018-01-05T00:00:00").replace(tzinfo=UTC)


def at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC)


def order(status: str = "delivered") -> rules.OrderFacts:
    return rules.OrderFacts(
        order_id="ORD-001",
        status=status,
        purchase_at=at("2018-01-01T00:00:00"),
        approved_at=at("2018-01-02T00:00:00"),
        carrier_at=at("2018-01-06T00:00:00"),
        delivered_at=at("2018-01-06T00:00:00") if status == "delivered" else None,
        estimated_at=at("2018-01-05T00:00:00"),
    )


def item(
    price: str = "100.00",
    freight: str = "0.00",
    seller_id: str = "SELLER-001",
    limit: datetime | None = DEFAULT_LIMIT,
) -> rules.ItemLine:
    return rules.ItemLine(
        item_id="ITEM-001",
        seller_id=seller_id,
        shipping_limit_at=limit,
        price=rules.parse_money(price) or rules.ZERO,
        freight=rules.parse_money(freight) or rules.ZERO,
    )


def event(
    kind: str,
    amount: str,
    when: str,
    status: str,
    reference: str = "PAY-001",
) -> dict[str, object]:
    return {
        "order_id": "ORD-001",
        "event_at": when,
        "event_type": kind,
        "amount_brl": amount,
        "status": status,
        "payment_reference": reference,
    }


def payment_row(value: str, sequence: str, reference: str = "PAY-001") -> dict[str, object]:
    return {
        "order_id": "ORD-001",
        "payment_sequential": sequence,
        "payment_reference": reference,
        "payment_type": "credit_card",
        "payment_value": value,
    }


def test_zero_capture_does_not_qualify_as_paid() -> None:
    payments = rules.analyse_payments(
        order("canceled"),
        {"events": [event("captured", "0.00", "2018-01-02T01:00:00", "confirmed")]},
        [payment_row("0.00", "SEQ-001")],
        at("2018-01-10T00:00:00"),
    )
    diagnosis = rules.diagnose(
        order("canceled"),
        rules.ItemView(),
        payments,
        rules.RefundView(),
        rules.ShipmentView(),
    )

    assert payments.captured_total == 0
    assert diagnosis.issue == "unsupported_claim"
    assert "no_capture_in_window" in diagnosis.doubts


def test_only_open_current_mismatches_are_diagnosed() -> None:
    payments = rules.analyse_payments(
        order("processing"),
        {
            "events": [
                event("captured", "100.00", "2018-01-02T01:00:00", "confirmed"),
                event("reconciliation_mismatch", "100.00", "2018-01-03T00:00:00", "resolved"),
                event("reconciliation_mismatch", "100.00", "2018-01-11T00:00:00", "open"),
                event("reconciliation_mismatch", "100.00", "2018-01-03T01:00:00", "open"),
            ]
        },
        [payment_row("100.00", "SEQ-001")],
        at("2018-01-10T00:00:00"),
    )
    diagnosis = rules.diagnose(
        order("processing"),
        rules.ItemView([item()]),
        payments,
        rules.RefundView(),
        rules.ShipmentView(),
    )

    assert len(payments.mismatches) == 1
    assert diagnosis.issue == "payment_mismatch"
    assert diagnosis.evidence_basis_brl == rules.parse_money("100.00")


def test_aggregate_refund_can_cover_split_captures() -> None:
    payments = rules.analyse_payments(
        order("canceled"),
        {
            "events": [
                event("captured", "60.00", "2018-01-02T01:00:00", "confirmed", "PAY-001"),
                event("captured", "40.00", "2018-01-02T02:00:00", "confirmed", "PAY-002"),
            ]
        },
        [
            payment_row("60.00", "SEQ-001", "PAY-001"),
            payment_row("40.00", "SEQ-002", "PAY-002"),
        ],
        at("2018-01-10T00:00:00"),
    )
    refunds = rules.analyse_refunds(
        order("canceled"),
        {"events": [event("refund_completed", "100.00", "2018-01-05T00:00:00", "completed")]},
        payments.captures,
        at("2018-01-10T00:00:00"),
    )

    assert payments.captured_total == rules.parse_money("100.00")
    assert len(refunds.events) == 1
    assert refunds.outstanding_total == 0


def test_completed_refund_prevents_duplicate_recommendation() -> None:
    payments = rules.analyse_payments(
        order("canceled"),
        {"events": [event("captured", "100.00", "2018-01-02T01:00:00", "confirmed")]},
        [payment_row("100.00", "SEQ-001")],
        at("2018-01-10T00:00:00"),
    )
    refunds = rules.analyse_refunds(
        order("canceled"),
        {"events": [event("refund", "100.00", "2018-01-03T00:00:00", "completed")]},
        payments.captures,
        at("2018-01-10T00:00:00"),
    )
    diagnosis = rules.diagnose(
        order("canceled"), rules.ItemView(), payments, refunds, rules.ShipmentView()
    )

    assert diagnosis.issue == "unsupported_claim"
    assert diagnosis.evidence_basis_brl == 0


def test_duplicate_charge_requires_distinct_payment_identity() -> None:
    confirmed = [
        event("captured", "60.00", "2018-01-02T01:00:00", "confirmed", "PAY-001"),
        event("captured", "60.00", "2018-01-02T02:00:00", "confirmed", "PAY-002"),
    ]
    with_rows = rules.analyse_payments(
        order("processing"),
        {"events": confirmed},
        [
            payment_row("60.00", "SEQ-001", "PAY-001"),
            payment_row("60.00", "SEQ-002", "PAY-002"),
        ],
        at("2018-01-10T00:00:00"),
    )
    without_rows = rules.analyse_payments(
        order("processing"),
        {"events": confirmed},
        [],
        at("2018-01-10T00:00:00"),
    )

    duplicate = rules.diagnose(
        order("processing"),
        rules.ItemView([item()]),
        with_rows,
        rules.RefundView(),
        rules.ShipmentView(),
    )
    unsupported = rules.diagnose(
        order("processing"),
        rules.ItemView([item()]),
        without_rows,
        rules.RefundView(),
        rules.ShipmentView(),
    )
    assert duplicate.issue == "duplicate_charge"
    assert unsupported.issue == "unsupported_claim"


def test_matching_split_payments_are_valid() -> None:
    payments = rules.analyse_payments(
        order("processing"),
        {
            "events": [
                event("captured", "60.00", "2018-01-02T01:00:00", "confirmed", "PAY-001"),
                event("captured", "40.00", "2018-01-02T02:00:00", "confirmed", "PAY-002"),
            ]
        },
        [
            payment_row("60.00", "SEQ-001", "PAY-001"),
            payment_row("40.00", "SEQ-002", "PAY-002"),
        ],
        at("2018-01-10T00:00:00"),
    )
    diagnosis = rules.diagnose(
        order("processing"),
        rules.ItemView([item()]),
        payments,
        rules.RefundView(),
        rules.ShipmentView(),
    )

    assert diagnosis.issue == "valid_split_payment"
    assert diagnosis.evidence_basis_brl == 0


def test_unknown_late_actor_is_insufficient() -> None:
    shipment = rules.ShipmentView(available=True, delivered_late=True)
    diagnosis = rules.diagnose(
        order(),
        rules.ItemView([item(limit=None)]),
        rules.PaymentView(),
        rules.RefundView(),
        shipment,
    )

    assert diagnosis.issue == "insufficient_evidence"
    assert "late_actor_unknown" in diagnosis.doubts


def test_multseller_handoff_uses_first_shipping_limit() -> None:
    first = item(seller_id="SELLER-001", limit=at("2018-01-04T00:00:00"))
    second = item(seller_id="SELLER-002", limit=at("2018-01-07T00:00:00"))
    items = rules.ItemView([first, second])
    shipment = rules.analyse_shipment(order(), items, {"events": []})

    assert shipment.handoff_after_limit is True
    assert rules.responsible_sellers(order(), items, "late_delivery_seller") == [
        "SELLER-001"
    ]


def test_refund_requested_pending_is_diagnosed() -> None:
    payments = rules.analyse_payments(
        order("processing"),
        {"events": [event("captured", "89.00", "2018-01-02T01:00:00", "confirmed")]},
        [payment_row("89.00", "SEQ-001")],
        at("2018-01-10T00:00:00"),
    )
    refunds = rules.analyse_refunds(
        order("processing"),
        {"events": [event("refund_requested", "89.00", "2018-01-03T00:00:00", "pending")]},
        payments.captures,
        at("2018-01-10T00:00:00"),
    )
    diagnosis = rules.diagnose(
        order("processing"),
        rules.ItemView([item()]),
        payments,
        refunds,
        rules.ShipmentView(),
    )

    assert len(refunds.events) == 1
    assert diagnosis.issue == "refund_pending"


def test_refund_requested_failed_is_diagnosed() -> None:
    payments = rules.analyse_payments(
        order("processing"),
        {"events": [event("captured", "52.00", "2018-01-02T01:00:00", "confirmed")]},
        [payment_row("52.00", "SEQ-001")],
        at("2018-01-10T00:00:00"),
    )
    refunds = rules.analyse_refunds(
        order("processing"),
        {"events": [event("refund_requested", "52.00", "2018-01-03T00:00:00", "failed")]},
        payments.captures,
        at("2018-01-10T00:00:00"),
    )
    diagnosis = rules.diagnose(
        order("processing"),
        rules.ItemView([item()]),
        payments,
        refunds,
        rules.ShipmentView(),
    )

    assert len(refunds.events) == 1
    assert diagnosis.issue == "refund_failed"
    assert diagnosis.evidence_basis_brl == rules.parse_money("52.00")
