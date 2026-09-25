from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
CAPTURE_WINDOW = timedelta(hours=24)
MISMATCH_WINDOW = timedelta(hours=24)
ITEM_HORIZON = timedelta(days=30)

POLICY_ISSUES = (
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
)
INSUFFICIENT = "insufficient_evidence"
CAPTURE_KINDS = frozenset({"captured", "capture", "payment_captured", "charge_captured"})
MISMATCH_KINDS = frozenset(
    {"reconciliation_mismatch", "payment_mismatch", "mismatch", "reconciliation_error"}
)
REFUND_KIND_PREFIX = "refund_"
CONFIRMED_STATUSES = frozenset({"confirmed", "complete", "completed", "captured", "settled"})
OPEN_MISMATCH_STATUSES = frozenset(
    {"open", "pending", "unresolved", "mismatch", "reconciliation_mismatch"}
)
COMPLETED_REFUND_STATUSES = frozenset(
    {"complete", "completed", "confirmed", "paid", "settled", "succeeded", "success"}
)
FAILED_REFUND_STATUSES = frozenset({"failed", "error", "rejected"})
PENDING_REFUND_STATUSES = frozenset(
    {"approved", "created", "initiated", "pending", "processing", "requested"}
)
CONTAINER_KEYS = ("order", "orders", "items", "payments", "events", "sellers", "shipments")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value).strip()).quantize(CENT)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount < 0:
        return None
    return amount


def money_equal(left: Decimal | None, right: Decimal | None) -> bool:
    return left is not None and right is not None and abs(left - right) <= CENT / 2


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _label(value: Any) -> str | None:
    text = _text(value)
    return text.casefold().replace("-", "_").replace(" ", "_") if text else None


def _rows(data: Any) -> list[Mapping[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, Mapping)]
    if not isinstance(data, Mapping):
        return []
    for key in CONTAINER_KEYS:
        nested = data.get(key)
        if isinstance(nested, list):
            return [row for row in nested if isinstance(row, Mapping)]
        if isinstance(nested, Mapping):
            return [nested]
    return [data]


def _unique_rows(rows: Iterable[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], int]:
    materialised = list(rows)
    seen: set[tuple[tuple[str, str], ...]] = set()
    unique: list[Mapping[str, Any]] = []
    for row in materialised:
        key = tuple(sorted((str(key), repr(value)) for key, value in row.items()))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique, len(materialised) - len(unique)


@dataclass(frozen=True)
class OrderFacts:
    order_id: str
    status: str
    purchase_at: datetime | None
    approved_at: datetime | None
    carrier_at: datetime | None
    delivered_at: datetime | None
    estimated_at: datetime | None

    @property
    def payment_anchor(self) -> datetime | None:
        return self.approved_at or self.purchase_at


def order_facts(data: Any, order_id: str) -> OrderFacts | None:
    matches = [row for row in _rows(data) if _text(row.get("order_id")) == order_id]
    unique, _ = _unique_rows(matches)
    if len(unique) != 1:
        return None
    row = unique[0]
    status = _label(row.get("order_status") or row.get("status")) or "unknown"
    return OrderFacts(
        order_id=order_id,
        status=status,
        purchase_at=parse_time(row.get("order_purchase_timestamp")),
        approved_at=parse_time(row.get("order_approved_at")),
        carrier_at=parse_time(row.get("order_delivered_carrier_date")),
        delivered_at=parse_time(row.get("order_delivered_customer_date")),
        estimated_at=parse_time(row.get("order_estimated_delivery_date")),
    )


@dataclass(frozen=True)
class ItemLine:
    item_id: str
    seller_id: str | None
    shipping_limit_at: datetime | None
    price: Decimal
    freight: Decimal


@dataclass
class ItemView:
    lines: list[ItemLine] = field(default_factory=list)
    excluded_rows: int = 0
    collapsed_rows: int = 0
    available: bool = False

    @property
    def item_ids(self) -> list[str]:
        return sorted({line.item_id for line in self.lines})

    @property
    def seller_ids(self) -> list[str]:
        return sorted({line.seller_id for line in self.lines if line.seller_id})

    @property
    def order_value(self) -> Decimal:
        return sum((line.price + line.freight for line in self.lines), ZERO)

    @property
    def freight_total(self) -> Decimal:
        return sum((line.freight for line in self.lines), ZERO)

    @property
    def earliest_shipping_limit(self) -> datetime | None:
        limits = [line.shipping_limit_at for line in self.lines if line.shipping_limit_at]
        return min(limits) if limits else None


def analyse_items(order: OrderFacts, data: Any) -> ItemView:
    raw = _rows(data)
    unique, collapsed = _unique_rows(raw)
    lower = order.purchase_at
    upper = order.estimated_at or (lower + ITEM_HORIZON if lower else None)
    view = ItemView(available=bool(unique), collapsed_rows=collapsed)
    for row in unique:
        item_id = _text(row.get("order_item_id") or row.get("item_id"))
        price = parse_money(row.get("price"))
        freight = parse_money(row.get("freight_value") or row.get("freight"))
        limit = parse_time(row.get("shipping_limit_date"))
        row_order = _text(row.get("order_id"))
        if (
            row_order not in (None, order.order_id)
            or item_id is None
            or price is None
            or freight is None
            or limit is None
            or lower is None
            or upper is None
            or not lower <= limit <= upper
        ):
            view.excluded_rows += 1
            continue
        view.lines.append(ItemLine(item_id, _text(row.get("seller_id")), limit, price, freight))
    return view


def responsible_sellers(order: OrderFacts | None, items: ItemView, issue: str) -> list[str]:
    if issue not in {"late_delivery_seller", "unavailable_order_paid"}:
        return []
    if issue == "unavailable_order_paid":
        return items.seller_ids
    carrier_at = order.carrier_at if order else None
    if carrier_at is None:
        return items.seller_ids
    late = {
        line.seller_id
        for line in items.lines
        if line.seller_id
        and line.shipping_limit_at is not None
        and carrier_at > line.shipping_limit_at
    }
    return sorted(late) or items.seller_ids


def known_sellers(data: Any, seller_ids: Iterable[str]) -> list[str]:
    wanted = set(seller_ids)
    return sorted({_text(row.get("seller_id")) or "" for row in _rows(data)} & wanted)


@dataclass(frozen=True)
class MoneyEvent:
    at: datetime
    kind: str
    amount: Decimal | None
    status: str
    reference: str | None = None


@dataclass(frozen=True)
class PaymentRow:
    sequential: str | None
    reference: str | None
    payment_type: str | None
    value: Decimal

    @property
    def identity(self) -> str | None:
        return self.sequential or self.reference


@dataclass
class PaymentView:
    available: bool = False
    captures: list[MoneyEvent] = field(default_factory=list)
    mismatches: list[MoneyEvent] = field(default_factory=list)
    matched_rows: list[PaymentRow] = field(default_factory=list)
    excluded_events: int = 0
    excluded_rows: int = 0
    collapsed_rows: int = 0
    unmatched_captures: int = 0
    row_sources_disagree: bool = False

    @property
    def captured_total(self) -> Decimal:
        return sum(
            (
                event.amount
                for event in self.captures
                if event.amount is not None and event.amount > 0
            ),
            ZERO,
        )

    @property
    def payment_references(self) -> list[str]:
        return sorted({row.reference for row in self.matched_rows if row.reference})


def _events(data: Any) -> list[Mapping[str, Any]]:
    if not isinstance(data, Mapping):
        return []
    nested = data.get("events")
    if isinstance(nested, list):
        return [row for row in nested if isinstance(row, Mapping)]
    if "event_at" in data:
        return [data]
    return []


def _money_event(row: Mapping[str, Any]) -> MoneyEvent | None:
    at = parse_time(row.get("event_at"))
    kind = _label(row.get("event_type"))
    if at is None or kind is None:
        return None
    return MoneyEvent(
        at=at,
        kind=kind,
        amount=parse_money(row.get("amount_brl")),
        status=_label(row.get("status")) or "",
        reference=_text(
            row.get("payment_reference")
            or row.get("transaction_reference")
            or row.get("event_id")
        ),
    )


def _payment_row(row: Mapping[str, Any]) -> PaymentRow | None:
    value = parse_money(row.get("payment_value"))
    if value is None or value <= 0:
        return None
    return PaymentRow(
        sequential=_text(row.get("payment_sequential")),
        reference=_text(row.get("payment_reference") or row.get("transaction_reference")),
        payment_type=_text(row.get("payment_type")),
        value=value,
    )


def analyse_payments(
    order: OrderFacts, timeline: Any, payment_rows: Any, opened_at: datetime | None
) -> PaymentView:
    view = PaymentView(available=isinstance(timeline, Mapping))
    anchor = order.payment_anchor
    raw_events = [
        row
        for row in _events(timeline)
        if _text(row.get("order_id")) in (None, order.order_id)
    ]
    view.excluded_events += len(_events(timeline)) - len(raw_events)
    unique_events, _ = _unique_rows(raw_events)
    parsed = [event for event in map(_money_event, unique_events) if event is not None]
    view.excluded_events += len(unique_events) - len(parsed)

    for event in sorted(parsed, key=lambda item: item.at):
        if event.kind not in CAPTURE_KINDS:
            continue
        in_window = (
            anchor is not None
            and anchor <= event.at <= anchor + CAPTURE_WINDOW
            and (opened_at is None or event.at <= opened_at)
            and event.amount is not None
            and event.amount > 0
            and event.status in CONFIRMED_STATUSES
        )
        if in_window:
            view.captures.append(event)
        else:
            view.excluded_events += 1

    for event in sorted(parsed, key=lambda item: item.at):
        if event.kind in CAPTURE_KINDS:
            continue
        follows_capture = any(
            capture.at <= event.at <= capture.at + MISMATCH_WINDOW for capture in view.captures
        )
        in_scope = (
            event.kind in MISMATCH_KINDS
            and follows_capture
            and (opened_at is None or event.at <= opened_at)
            and event.amount is not None
            and event.amount > 0
            and event.status in OPEN_MISMATCH_STATUSES
        )
        if in_scope:
            view.mismatches.append(event)
        else:
            view.excluded_events += 1

    rows_source = payment_rows if payment_rows is not None else _timeline_payments(timeline)
    scoped_rows = [
        row
        for row in _rows(rows_source)
        if _text(row.get("order_id")) in (None, order.order_id)
    ]
    unique_rows, view.collapsed_rows = _unique_rows(scoped_rows)
    candidates = [row for row in map(_payment_row, unique_rows) if row is not None]
    view.excluded_rows = len(_rows(rows_source)) - len(scoped_rows)
    view.excluded_rows += len(unique_rows) - len(candidates)
    remaining = list(candidates)
    for capture in view.captures:
        match = next(
            (row for row in remaining if money_equal(row.value, capture.amount)),
            None,
        )
        if match is None:
            view.unmatched_captures += 1
            continue
        remaining.remove(match)
        view.matched_rows.append(match)
    view.excluded_rows += len(remaining)

    if payment_rows is not None and isinstance(timeline, Mapping):
        mirror = _timeline_payments(timeline)
        if mirror is not None:
            view.row_sources_disagree = _row_keys(payment_rows) != _row_keys(mirror)
    return view


def _timeline_payments(timeline: Any) -> Any:
    return timeline.get("payments") if isinstance(timeline, Mapping) else None


def _row_keys(data: Any) -> list[str]:
    return sorted(repr(sorted(row.items())) for row in _rows(data))


@dataclass
class RefundView:
    captured_total: Decimal = ZERO
    events: list[MoneyEvent] = field(default_factory=list)
    excluded_events: int = 0
    available: bool = False

    def with_status(self, statuses: frozenset[str]) -> list[MoneyEvent]:
        return [event for event in self.events if event.status in statuses]

    @property
    def completed_total(self) -> Decimal:
        return sum(
            (
                event.amount
                for event in self.with_status(COMPLETED_REFUND_STATUSES)
                if event.amount is not None
            ),
            ZERO,
        )

    @property
    def outstanding_total(self) -> Decimal:
        return max(ZERO, self.captured_total - self.completed_total)


def analyse_refunds(
    order: OrderFacts, data: Any, captures: list[MoneyEvent], opened_at: datetime | None
) -> RefundView:
    captured_total = sum(
        (
            capture.amount
            for capture in captures
            if capture.amount is not None and capture.amount > 0
        ),
        ZERO,
    )
    view = RefundView(captured_total=captured_total, available=isinstance(data, Mapping))
    anchor = order.payment_anchor
    rows = [row for row in _events(data) if _text(row.get("order_id")) in (None, order.order_id)]
    view.excluded_events = len(_events(data)) - len(rows)
    unique, _ = _unique_rows(rows)
    for event in sorted(filter(None, map(_money_event, unique)), key=lambda item: item.at):
        in_scope = (
            (event.kind == "refund" or event.kind.startswith(REFUND_KIND_PREFIX))
            and anchor is not None
            and anchor < event.at
            and (opened_at is None or event.at <= opened_at)
            and event.amount is not None
            and event.amount > 0
            and event.amount <= captured_total + CENT / 2
        )
        if in_scope:
            view.events.append(event)
        else:
            view.excluded_events += 1
    return view


@dataclass
class ShipmentView:
    available: bool = False
    delivered_late: bool = False
    handoff_after_limit: bool | None = None
    late_event_actor: str | None = None
    shipment_id: str | None = None
    excluded_events: int = 0
    conflicting_fields: list[str] = field(default_factory=list)


SHIPMENT_MIRROR = (
    ("order_status", "status"),
    ("delivered_carrier_at", "carrier_at"),
    ("delivered_customer_at", "delivered_at"),
    ("estimated_delivery_at", "estimated_at"),
)


def analyse_shipment(order: OrderFacts, items: ItemView, data: Any) -> ShipmentView:
    view = ShipmentView(available=isinstance(data, Mapping))
    view.delivered_late = (
        order.status == "delivered"
        and order.delivered_at is not None
        and order.estimated_at is not None
        and order.delivered_at > order.estimated_at
    )
    limit = items.earliest_shipping_limit
    if order.carrier_at is not None and limit is not None:
        view.handoff_after_limit = order.carrier_at > limit
    if not isinstance(data, Mapping):
        return view
    view.shipment_id = _text(data.get("shipment_id"))
    for shipment_key, order_attr in SHIPMENT_MIRROR:
        expected = getattr(order, order_attr)
        actual = data.get(shipment_key)
        if order_attr == "status":
            same = (_label(actual) or "") == expected
        else:
            same = parse_time(actual) == expected
        if not same:
            view.conflicting_fields.append(shipment_key)
    for row in _events(data):
        at = parse_time(row.get("event_at"))
        kind = _label(row.get("event_type"))
        matches_delivery = (
            kind in {"delivered_late", "delivered", "late_delivery"}
            and view.delivered_late
            and at == order.delivered_at
            and _text(row.get("order_id")) in (None, order.order_id)
        )
        if matches_delivery and view.late_event_actor is None:
            view.late_event_actor = _label(row.get("actor"))
        else:
            view.excluded_events += 1
    return view


@dataclass(frozen=True)
class Diagnosis:
    issue: str
    evidence_basis_brl: Decimal
    basis_entity: str | None
    signals: tuple[str, ...]
    doubts: tuple[str, ...]


def _duplicate_capture(payments: PaymentView, order_value: Decimal) -> MoneyEvent | None:
    if len(payments.captures) < 2 or payments.captured_total <= order_value + CENT / 2:
        return None
    by_amount: dict[Decimal, list[MoneyEvent]] = {}
    for capture in payments.captures:
        if capture.amount is not None and capture.amount > 0:
            by_amount.setdefault(capture.amount, []).append(capture)
    for amount, captures in by_amount.items():
        identities = {
            row.identity
            for row in payments.matched_rows
            if money_equal(row.value, amount) and row.identity is not None
        }
        repeated_times = len({capture.at for capture in captures}) >= 2
        if len(captures) >= 2 and repeated_times and len(identities) >= 2:
            return captures[1]
    return None


def diagnose(
    order: OrderFacts | None,
    items: ItemView,
    payments: PaymentView,
    refunds: RefundView,
    shipment: ShipmentView,
) -> Diagnosis:
    if order is None:
        return Diagnosis(INSUFFICIENT, ZERO, None, ("order_missing",), ("order_missing",))
    captured = payments.captured_total
    outstanding = refunds.outstanding_total if refunds.available else captured
    doubts: list[str] = []
    if payments.unmatched_captures:
        doubts.append("capture_without_payment_row")
    if shipment.conflicting_fields:
        doubts.append("shipment_disagrees_with_order")

    if order.status in {"canceled", "unavailable"} and captured > 0:
        if outstanding <= 0:
            return Diagnosis(
                "unsupported_claim",
                ZERO,
                order.order_id,
                ("refund_already_completed",),
                tuple(doubts),
            )
        signals = [f"status_{order.status}", "capture_in_window"]
        if refunds.completed_total > 0:
            signals.append("partial_refund_completed")
        return Diagnosis(
            f"{order.status}_order_paid",
            outstanding,
            order.order_id,
            tuple(signals),
            tuple(doubts),
        )

    if shipment.delivered_late:
        seller_late = shipment.handoff_after_limit
        actor = shipment.late_event_actor
        if seller_late is None and actor is not None:
            seller_late = actor == "seller"
        if seller_late is None:
            doubts.append("late_actor_unknown")
            return Diagnosis(
                INSUFFICIENT,
                ZERO,
                order.order_id,
                ("delivered_after_estimate", "responsible_actor_unknown"),
                tuple(doubts),
            )
        expected_actor = "seller" if seller_late else "logistics_provider"
        if actor is not None and actor != expected_actor:
            doubts.append("late_event_actor_disagrees")
        issue = "late_delivery_seller" if seller_late else "late_delivery_logistics"
        basis = min(items.freight_total, outstanding)
        entity = items.item_ids[0] if len(items.item_ids) == 1 else order.order_id
        handoff = "handoff_after_limit" if seller_late else "handoff_within_limit"
        return Diagnosis(issue, basis, entity, ("delivered_after_estimate", handoff), tuple(doubts))

    completed = refunds.with_status(COMPLETED_REFUND_STATUSES)
    if completed and refunds.outstanding_total <= 0:
        return Diagnosis(
            "unsupported_claim",
            ZERO,
            order.order_id,
            ("refund_already_completed",),
            tuple(doubts),
        )
    failed = refunds.with_status(FAILED_REFUND_STATUSES)
    if failed and refunds.outstanding_total > 0:
        basis = min(
            sum((event.amount or ZERO for event in failed), ZERO),
            refunds.outstanding_total,
        )
        return Diagnosis(
            "refund_failed",
            basis,
            order.order_id,
            ("refund_failed_in_window",),
            tuple(doubts),
        )
    if refunds.with_status(PENDING_REFUND_STATUSES) and refunds.outstanding_total > 0:
        return Diagnosis(
            "refund_pending",
            ZERO,
            order.order_id,
            ("refund_pending_in_window",),
            tuple(doubts),
        )

    if payments.mismatches:
        basis = sum((event.amount or ZERO for event in payments.mismatches), ZERO)
        return Diagnosis(
            "payment_mismatch",
            basis,
            order.order_id,
            ("reconciliation_mismatch_open",),
            tuple(doubts),
        )

    order_value = items.order_value
    duplicate = _duplicate_capture(payments, order_value) if items.lines else None
    if duplicate is not None and duplicate.amount is not None:
        return Diagnosis(
            "duplicate_charge",
            duplicate.amount,
            order.order_id,
            ("repeated_capture_distinct_sequence", "captured_above_order_value"),
            tuple(doubts),
        )
    if (
        len(payments.captures) >= 2
        and items.lines
        and len(payments.matched_rows) >= len(payments.captures)
        and not payments.unmatched_captures
        and money_equal(payments.captured_total, order_value)
    ):
        return Diagnosis(
            "valid_split_payment",
            ZERO,
            order.order_id,
            ("captures_sum_to_order_value",),
            tuple(doubts),
        )
    if captured <= 0:
        doubts.append("no_capture_in_window")
    return Diagnosis(
        "unsupported_claim",
        ZERO,
        order.order_id,
        ("no_policy_trigger_in_evidence",),
        tuple(doubts),
    )
