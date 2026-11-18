"""Collections (from customers) and disbursements (to suppliers), settling posted invoices."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .accounting import LineInput, post_entry
from .models import (
    Account,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    Partner,
    PartnerKind,
    Payment,
    PaymentAllocation,
    PaymentKind,
    PaymentMethod,
)

CENT = Decimal("0.01")
METHOD_CODES = {PaymentMethod.CASH: "1101", PaymentMethod.BANK: "1102"}
METHOD_LABELS_AR = {PaymentMethod.CASH: "صندوق", PaymentMethod.BANK: "بنك"}
PAYMENT_LABELS_AR = {PaymentKind.RECEIPT: "سند قبض", PaymentKind.PAYMENT: "سند صرف"}
INVOICE_KIND_FOR = {PaymentKind.RECEIPT: InvoiceKind.SALES, PaymentKind.PAYMENT: InvoiceKind.PURCHASE}
PARTNER_KIND_FOR = {PaymentKind.RECEIPT: PartnerKind.CUSTOMER, PaymentKind.PAYMENT: PartnerKind.SUPPLIER}


@dataclass(frozen=True)
class OpenInvoice:
    invoice: Invoice
    outstanding: Decimal


def _allocated(session: Session, invoice_id: int) -> Decimal:
    value = session.scalar(
        select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(PaymentAllocation.invoice_id == invoice_id)
    )
    return Decimal(str(value))


def outstanding_amount(session: Session, invoice: Invoice) -> Decimal:
    return (invoice.total - _allocated(session, invoice.id)).quantize(CENT)


def open_invoices(
    session: Session, company_id: int, kind: PaymentKind, partner_id: int | None = None
) -> list[OpenInvoice]:
    query = (
        select(Invoice)
        .where(
            Invoice.company_id == company_id,
            Invoice.kind == INVOICE_KIND_FOR[kind],
            Invoice.status == InvoiceStatus.POSTED,
        )
        .order_by(Invoice.invoice_date, Invoice.number)
    )
    if partner_id:
        query = query.where(Invoice.partner_id == partner_id)
    result = []
    for invoice in session.scalars(query).all():
        remaining = outstanding_amount(session, invoice)
        if remaining > 0:
            result.append(OpenInvoice(invoice=invoice, outstanding=remaining))
    return result


def _control_account(session: Session, company_id: int, code: str, label: str) -> Account:
    account = session.scalar(select(Account).where(Account.company_id == company_id, Account.code == code))
    if account is None or not account.is_active:
        raise PaymentError(f"{label} ({code}) غير موجود أو موقوف. راجع شجرة الحسابات.")
    return account


class PaymentError(ValueError):
    """Raised when a payment breaks a rule."""


def _next_number(session: Session, company_id: int, kind: PaymentKind) -> int:
    current = session.scalar(
        select(func.max(Payment.number)).where(Payment.company_id == company_id, Payment.kind == kind)
    )
    return (current or 0) + 1


def record_payment(
    session: Session,
    *,
    company_id: int,
    user_id: int,
    kind: PaymentKind,
    partner_id: int,
    payment_date: date,
    method: PaymentMethod,
    amount: Decimal,
    allocations: list[tuple[int, Decimal]],
) -> Payment:
    partner = session.scalar(select(Partner).where(Partner.id == partner_id, Partner.company_id == company_id))
    if partner is None or not partner.is_active:
        raise PaymentError("الطرف غير موجود أو موقوف.")
    if partner.kind != PARTNER_KIND_FOR[kind]:
        raise PaymentError("نوع الطرف لا يناسب نوع السند.")
    if amount <= 0:
        raise PaymentError("المبلغ لازم يكون أكبر من صفر.")
    if not allocations:
        raise PaymentError("اختار فاتورة واحدة على الأقل لتسوية السند عليها.")

    seen: set[int] = set()
    total_allocated = Decimal("0")
    invoices: dict[int, Invoice] = {}
    for invoice_id, alloc in allocations:
        if invoice_id in seen:
            raise PaymentError("الفاتورة مكررة في التسوية.")
        seen.add(invoice_id)
        if alloc <= 0:
            raise PaymentError("مبلغ التسوية لازم يكون أكبر من صفر.")
        invoice = session.scalar(
            select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == company_id)
        )
        if invoice is None:
            raise PaymentError("فاتورة غير موجودة.")
        if invoice.status != InvoiceStatus.POSTED:
            raise PaymentError(f"الفاتورة {invoice.id} لم تُرحَّل بعد.")
        if invoice.kind != INVOICE_KIND_FOR[kind] or invoice.partner_id != partner_id:
            raise PaymentError("الفاتورة لا تخص هذا الطرف أو نوعها لا يناسب السند.")
        remaining = outstanding_amount(session, invoice)
        if alloc > remaining:
            raise PaymentError(f"مبلغ التسوية أكبر من المتبقي على الفاتورة ({remaining}).")
        invoices[invoice_id] = invoice
        total_allocated += alloc

    if total_allocated != amount:
        raise PaymentError(f"مجموع التسويات ({total_allocated}) لازم يساوي مبلغ السند ({amount}).")

    cash_or_bank = _control_account(
        session, company_id, METHOD_CODES[method], METHOD_LABELS_AR[method]
    )
    control = _control_account(
        session,
        company_id,
        "1103" if kind == PaymentKind.RECEIPT else "2101",
        "حساب العملاء" if kind == PaymentKind.RECEIPT else "حساب الموردين",
    )

    if kind == PaymentKind.RECEIPT:
        entry_lines = [
            LineInput(cash_or_bank.id, debit=amount),
            LineInput(control.id, credit=amount),
        ]
    else:
        entry_lines = [
            LineInput(control.id, debit=amount),
            LineInput(cash_or_bank.id, credit=amount),
        ]

    description = f"{PAYMENT_LABELS_AR[kind]} - {partner.name}"
    entry = post_entry(
        session,
        company_id=company_id,
        user_id=user_id,
        entry_date=payment_date,
        description=description,
        lines=entry_lines,
    )
    session.flush()

    payment = Payment(
        company_id=company_id,
        kind=kind,
        number=_next_number(session, company_id, kind),
        partner_id=partner_id,
        payment_date=payment_date,
        method=method,
        amount=amount,
        journal_entry_id=entry.id,
        created_by_id=user_id,
    )
    payment.allocations = [
        PaymentAllocation(invoice_id=invoice_id, amount=alloc) for invoice_id, alloc in allocations
    ]
    session.add(payment)
    return payment
