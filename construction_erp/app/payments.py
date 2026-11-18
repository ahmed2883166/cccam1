"""Collections (from customers) and disbursements (to suppliers), settling invoices, advances, and credit notes."""

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

# Customer advances are a liability (2103); supplier advances are an asset (1109).
ADVANCE_CODE = {PaymentKind.RECEIPT: "2103", PaymentKind.PAYMENT: "1109"}
CONTROL_CODE = {PaymentKind.RECEIPT: "1103", PaymentKind.PAYMENT: "2101"}


class PaymentError(ValueError):
    """Raised when a payment breaks a rule."""


@dataclass(frozen=True)
class OpenInvoice:
    invoice: Invoice
    outstanding: Decimal


def _allocated(session: Session, invoice_id: int) -> Decimal:
    value = session.scalar(
        select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(PaymentAllocation.invoice_id == invoice_id)
    )
    return Decimal(str(value))


def _credited(session: Session, invoice_id: int) -> Decimal:
    value = session.scalar(
        select(func.coalesce(func.sum(Invoice.total), 0)).where(
            Invoice.credit_for_id == invoice_id, Invoice.status == InvoiceStatus.POSTED
        )
    )
    return Decimal(str(value))


def outstanding_amount(session: Session, invoice: Invoice) -> Decimal:
    """Invoice total minus settlements and posted credit notes."""
    return (invoice.total - _allocated(session, invoice.id) - _credited(session, invoice.id)).quantize(CENT)


def advance_remaining(session: Session, payment: Payment) -> Decimal:
    used = session.scalar(
        select(func.coalesce(func.sum(PaymentAllocation.amount), 0)).where(PaymentAllocation.payment_id == payment.id)
    )
    return (payment.amount - Decimal(str(used))).quantize(CENT)


def open_invoices(
    session: Session, company_id: int, kind: PaymentKind, partner_id: int | None = None
) -> list[OpenInvoice]:
    query = (
        select(Invoice)
        .where(
            Invoice.company_id == company_id,
            Invoice.kind == INVOICE_KIND_FOR[kind],
            Invoice.status == InvoiceStatus.POSTED,
            Invoice.is_credit.is_(False),
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


def open_advances(session: Session, company_id: int, kind: PaymentKind) -> list[tuple[Payment, Decimal]]:
    payments = session.scalars(
        select(Payment).where(Payment.company_id == company_id, Payment.kind == kind).order_by(Payment.payment_date)
    ).all()
    result = []
    for payment in payments:
        remaining = advance_remaining(session, payment)
        if remaining > 0:
            result.append((payment, remaining))
    return result


def _account(session: Session, company_id: int, code: str, label: str) -> Account:
    account = session.scalar(select(Account).where(Account.company_id == company_id, Account.code == code))
    if account is None or not account.is_active:
        raise PaymentError(f"{label} ({code}) غير موجود أو موقوف. راجع شجرة الحسابات.")
    return account


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
    """Record a receipt or payment. Amounts not settled on invoices become an advance."""
    partner = session.scalar(select(Partner).where(Partner.id == partner_id, Partner.company_id == company_id))
    if partner is None or not partner.is_active:
        raise PaymentError("الطرف غير موجود أو موقوف.")
    if partner.kind != PARTNER_KIND_FOR[kind]:
        raise PaymentError("نوع الطرف لا يناسب نوع السند.")
    if amount <= 0:
        raise PaymentError("المبلغ لازم يكون أكبر من صفر.")

    seen: set[int] = set()
    total_allocated = Decimal("0")
    for invoice_id, alloc in allocations:
        if invoice_id in seen:
            raise PaymentError("الفاتورة مكررة في التسوية.")
        seen.add(invoice_id)
        if alloc <= 0:
            raise PaymentError("مبلغ التسوية لازم يكون أكبر من صفر.")
        invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == company_id))
        if invoice is None:
            raise PaymentError("فاتورة غير موجودة.")
        if invoice.is_credit:
            raise PaymentError("الإشعارات مش بتتسوّى مباشرة، بتتخصم من الفاتورة الأصلية.")
        if invoice.status != InvoiceStatus.POSTED:
            raise PaymentError(f"الفاتورة {invoice.id} لم تُرحَّل بعد.")
        if invoice.kind != INVOICE_KIND_FOR[kind] or invoice.partner_id != partner_id:
            raise PaymentError("الفاتورة لا تخص هذا الطرف أو نوعها لا يناسب السند.")
        remaining = outstanding_amount(session, invoice)
        if alloc > remaining:
            raise PaymentError(f"مبلغ التسوية أكبر من المتبقي على الفاتورة ({remaining}).")
        total_allocated += alloc

    if total_allocated > amount:
        raise PaymentError(f"مجموع التسويات ({total_allocated}) أكبر من مبلغ السند ({amount}).")
    advance = amount - total_allocated

    cash_or_bank = _account(session, company_id, METHOD_CODES[method], METHOD_LABELS_AR[method])
    control = _account(session, company_id, CONTROL_CODE[kind], "حساب العملاء" if kind == PaymentKind.RECEIPT else "حساب الموردين")
    advance_account = None
    if advance:
        advance_account = _account(
            session, company_id, ADVANCE_CODE[kind], "حساب الدفعات المقدمة"
        )

    if kind == PaymentKind.RECEIPT:
        entry_lines = [LineInput(cash_or_bank.id, debit=amount)]
        if total_allocated:
            entry_lines.append(LineInput(control.id, credit=total_allocated))
        if advance:
            entry_lines.append(LineInput(advance_account.id, credit=advance))
    else:
        entry_lines = [LineInput(cash_or_bank.id, credit=amount)]
        if total_allocated:
            entry_lines.append(LineInput(control.id, debit=total_allocated))
        if advance:
            entry_lines.append(LineInput(advance_account.id, debit=advance))

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


def apply_advance(
    session: Session,
    *,
    company_id: int,
    user_id: int,
    payment_id: int,
    invoice_id: int,
    amount: Decimal,
    apply_date: date,
) -> PaymentAllocation:
    """Use part of an existing advance to settle an open invoice of the same partner."""
    payment = session.scalar(select(Payment).where(Payment.id == payment_id, Payment.company_id == company_id))
    if payment is None:
        raise PaymentError("السند غير موجود.")
    if amount <= 0:
        raise PaymentError("المبلغ لازم يكون أكبر من صفر.")
    remaining_advance = advance_remaining(session, payment)
    if amount > remaining_advance:
        raise PaymentError(f"المبلغ أكبر من الرصيد المقدم المتاح ({remaining_advance}).")

    invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == company_id))
    if invoice is None or invoice.is_credit or invoice.status != InvoiceStatus.POSTED:
        raise PaymentError("الفاتورة غير صالحة للتسوية.")
    if invoice.kind != INVOICE_KIND_FOR[payment.kind] or invoice.partner_id != payment.partner_id:
        raise PaymentError("الفاتورة لا تخص نفس الطرف أو نوعها لا يناسب الدفعة المقدمة.")
    remaining_invoice = outstanding_amount(session, invoice)
    if amount > remaining_invoice:
        raise PaymentError(f"المبلغ أكبر من المتبقي على الفاتورة ({remaining_invoice}).")

    control = _account(session, company_id, CONTROL_CODE[payment.kind], "حساب الطرف")
    advance_account = _account(session, company_id, ADVANCE_CODE[payment.kind], "حساب الدفعات المقدمة")
    if payment.kind == PaymentKind.RECEIPT:
        entry_lines = [LineInput(advance_account.id, debit=amount), LineInput(control.id, credit=amount)]
    else:
        entry_lines = [LineInput(control.id, debit=amount), LineInput(advance_account.id, credit=amount)]

    post_entry(
        session,
        company_id=company_id,
        user_id=user_id,
        entry_date=apply_date,
        description=f"تسوية دفعة مقدمة - {payment.partner.name}",
        lines=entry_lines,
    )
    allocation = PaymentAllocation(payment_id=payment.id, invoice_id=invoice.id, amount=amount)
    session.add(allocation)
    return allocation
