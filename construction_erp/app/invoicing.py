"""Sales and purchase invoices, credit/debit notes, validation, totals, and posting to the ledger."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .accounting import LineInput, post_entry
from .models import (
    Account,
    AccountType,
    Invoice,
    InvoiceKind,
    InvoiceLine,
    InvoiceStatus,
    Partner,
    PartnerKind,
)

CENT = Decimal("0.01")
ALLOWED_TAX_RATES = (Decimal("0"), Decimal("15"))

# Control accounts, looked up by code from the company's chart of accounts.
CONTROL_CODES = {
    InvoiceKind.SALES: {"partner": "1103", "tax": "2104"},  # receivables, output VAT
    InvoiceKind.PURCHASE: {"partner": "2101", "tax": "1108"},  # payables, input VAT
}

KIND_LABELS_AR = {InvoiceKind.SALES: "فاتورة مبيعات", InvoiceKind.PURCHASE: "فاتورة مشتريات"}
STATUS_LABELS_AR = {InvoiceStatus.DRAFT: "مسودة", InvoiceStatus.POSTED: "مرحّلة"}
CREDIT_LABELS_AR = {InvoiceKind.SALES: "إشعار دائن", InvoiceKind.PURCHASE: "إشعار مدين"}


class InvoiceError(ValueError):
    """Raised when an invoice breaks a rule."""


@dataclass(frozen=True)
class LineDraft:
    account_id: int
    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_rate: Decimal


def parse_decimal(raw: str, field: str) -> Decimal:
    try:
        value = Decimal((raw or "").strip())
    except (InvalidOperation, ValueError):
        raise InvoiceError(f"قيمة غير صحيحة في {field}.")
    if not value.is_finite():
        raise InvoiceError(f"قيمة غير صحيحة في {field}.")
    return value


def compute_line(quantity: Decimal, unit_price: Decimal, tax_rate: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    net = (quantity * unit_price).quantize(CENT)
    tax = (net * tax_rate / Decimal("100")).quantize(CENT)
    return net, tax, net + tax


def _allowed_account_types(kind: InvoiceKind) -> tuple[AccountType, ...]:
    if kind == InvoiceKind.SALES:
        return (AccountType.REVENUE,)
    return (AccountType.EXPENSE, AccountType.ASSET)


def _get_partner(session: Session, company_id: int, partner_id: int, kind: InvoiceKind) -> Partner:
    partner = session.scalar(select(Partner).where(Partner.id == partner_id, Partner.company_id == company_id))
    if partner is None:
        raise InvoiceError("الطرف المحدد غير موجود.")
    if not partner.is_active:
        raise InvoiceError("الطرف المحدد موقوف.")
    expected = PartnerKind.CUSTOMER if kind == InvoiceKind.SALES else PartnerKind.SUPPLIER
    if partner.kind != expected:
        raise InvoiceError("نوع الطرف لا يناسب نوع الفاتورة (عميل للمبيعات، ومورد للمشتريات).")
    return partner


def _get_account(session: Session, company_id: int, account_id: int, kind: InvoiceKind) -> Account:
    account = session.scalar(select(Account).where(Account.id == account_id, Account.company_id == company_id))
    if account is None:
        raise InvoiceError("أحد الحسابات المختارة غير موجود.")
    if not account.is_active or not account.is_postable:
        raise InvoiceError(f"الحساب {account.code} لا يقبل قيودًا.")
    if account.type not in _allowed_account_types(kind):
        if kind == InvoiceKind.SALES:
            raise InvoiceError(f"الحساب {account.code} ليس حساب إيراد.")
        raise InvoiceError(f"الحساب {account.code} ليس حساب مصروف أو مخزون.")
    return account


def create_draft(
    session: Session,
    *,
    company_id: int,
    user_id: int,
    kind: InvoiceKind,
    partner_id: int,
    invoice_date: date,
    lines: list[LineDraft],
) -> Invoice:
    _get_partner(session, company_id, partner_id, kind)
    if not lines:
        raise InvoiceError("الفاتورة لازم يكون فيها سطر واحد على الأقل.")

    invoice = Invoice(
        company_id=company_id,
        kind=kind,
        status=InvoiceStatus.DRAFT,
        is_credit=False,
        partner_id=partner_id,
        invoice_date=invoice_date,
        created_by_id=user_id,
    )
    subtotal = Decimal("0")
    tax_total = Decimal("0")
    for draft in lines:
        description = (draft.description or "").strip()
        if not description:
            raise InvoiceError("وصف كل سطر مطلوب.")
        if draft.quantity <= 0:
            raise InvoiceError("الكمية لازم تكون أكبر من صفر.")
        if draft.unit_price < 0:
            raise InvoiceError("السعر مش ممكن يكون سالب.")
        if draft.tax_rate not in ALLOWED_TAX_RATES:
            raise InvoiceError("نسبة الضريبة لازم تكون 0% أو 15%.")
        _get_account(session, company_id, draft.account_id, kind)

        net, tax, total = compute_line(draft.quantity, draft.unit_price, draft.tax_rate)
        subtotal += net
        tax_total += tax
        invoice.lines.append(
            InvoiceLine(
                account_id=draft.account_id,
                description=description,
                quantity=draft.quantity,
                unit_price=draft.unit_price,
                tax_rate=draft.tax_rate,
                net=net,
                tax=tax,
                total=total,
            )
        )

    invoice.subtotal = subtotal
    invoice.tax_total = tax_total
    invoice.total = subtotal + tax_total
    session.add(invoice)
    return invoice


def credited_quantity(session: Session, source_line_id: int) -> Decimal:
    """Quantity already credited on posted credit notes for one original invoice line."""
    value = session.scalar(
        select(func.coalesce(func.sum(InvoiceLine.quantity), 0))
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(InvoiceLine.source_line_id == source_line_id, Invoice.status == InvoiceStatus.POSTED)
    )
    return Decimal(str(value))


def create_credit_note(
    session: Session,
    *,
    company_id: int,
    user_id: int,
    original_id: int,
    credit_quantities: dict[int, Decimal],
    credit_date: date,
) -> Invoice:
    """Create and post a credit note (sales) or debit note (purchases) against a posted invoice."""
    original = session.scalar(select(Invoice).where(Invoice.id == original_id, Invoice.company_id == company_id))
    if original is None:
        raise InvoiceError("الفاتورة الأصلية غير موجودة.")
    if original.is_credit:
        raise InvoiceError("لا يمكن إنشاء إشعار على إشعار.")
    if original.status != InvoiceStatus.POSTED:
        raise InvoiceError("لازم الفاتورة تكون مرحّلة قبل إنشاء إشعار.")

    credit_lines: list[InvoiceLine] = []
    for line in original.lines:
        qty = credit_quantities.get(line.id, Decimal("0"))
        if qty <= 0:
            continue
        remaining = line.quantity - credited_quantity(session, line.id)
        if qty > remaining:
            raise InvoiceError(f"الكمية المرتجعة للبند «{line.description}» أكبر من المتاح ({remaining}).")
        net, tax, total = compute_line(qty, line.unit_price, line.tax_rate)
        credit_lines.append(
            InvoiceLine(
                account_id=line.account_id,
                description=line.description,
                quantity=qty,
                unit_price=line.unit_price,
                tax_rate=line.tax_rate,
                net=net,
                tax=tax,
                total=total,
                source_line_id=line.id,
            )
        )
    if not credit_lines:
        raise InvoiceError("اختار كمية مرتجعة لبند واحد على الأقل.")

    credit = Invoice(
        company_id=company_id,
        kind=original.kind,
        status=InvoiceStatus.DRAFT,
        is_credit=True,
        credit_for_id=original.id,
        partner_id=original.partner_id,
        invoice_date=credit_date,
        created_by_id=user_id,
    )
    credit.lines = credit_lines
    credit.subtotal = sum((l.net for l in credit_lines), Decimal("0"))
    credit.tax_total = sum((l.tax for l in credit_lines), Decimal("0"))
    credit.total = credit.subtotal + credit.tax_total
    session.add(credit)
    session.flush()
    post_invoice(session, company_id=company_id, user_id=user_id, invoice_id=credit.id)
    return credit


def _control_account(session: Session, company_id: int, kind: InvoiceKind, key: str) -> Account:
    code = CONTROL_CODES[kind][key]
    account = session.scalar(select(Account).where(Account.company_id == company_id, Account.code == code))
    if account is None or not account.is_active:
        raise InvoiceError(f"حساب الرقابة {code} غير موجود أو موقوف. راجع شجرة الحسابات.")
    return account


def _next_number(session: Session, company_id: int, kind: InvoiceKind, is_credit: bool) -> int:
    current = session.scalar(
        select(func.max(Invoice.number)).where(
            Invoice.company_id == company_id, Invoice.kind == kind, Invoice.is_credit == is_credit
        )
    )
    return (current or 0) + 1


def _entry_lines(invoice: Invoice, partner_control: Account, tax_control: Account) -> list[LineInput]:
    """Build the journal lines. Normal invoices and credit notes differ only by direction."""
    total, tax = invoice.total, invoice.tax_total
    lines: list[LineInput] = []
    if invoice.kind == InvoiceKind.SALES:
        if not invoice.is_credit:
            lines.append(LineInput(partner_control.id, debit=total))
            lines += [LineInput(l.account_id, credit=l.net, memo=l.description) for l in invoice.lines]
            if tax:
                lines.append(LineInput(tax_control.id, credit=tax))
        else:
            lines += [LineInput(l.account_id, debit=l.net, memo=l.description) for l in invoice.lines]
            if tax:
                lines.append(LineInput(tax_control.id, debit=tax))
            lines.append(LineInput(partner_control.id, credit=total))
    else:
        if not invoice.is_credit:
            lines += [LineInput(l.account_id, debit=l.net, memo=l.description) for l in invoice.lines]
            if tax:
                lines.append(LineInput(tax_control.id, debit=tax))
            lines.append(LineInput(partner_control.id, credit=total))
        else:
            lines.append(LineInput(partner_control.id, debit=total))
            lines += [LineInput(l.account_id, credit=l.net, memo=l.description) for l in invoice.lines]
            if tax:
                lines.append(LineInput(tax_control.id, credit=tax))
    return [line for line in lines if line.debit or line.credit]


def post_invoice(session: Session, *, company_id: int, user_id: int, invoice_id: int) -> Invoice:
    invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == company_id))
    if invoice is None:
        raise InvoiceError("الفاتورة غير موجودة.")
    if invoice.status != InvoiceStatus.DRAFT:
        raise InvoiceError("الفاتورة مرحّلة بالفعل.")
    if not invoice.lines:
        raise InvoiceError("لا يمكن ترحيل فاتورة بدون أسطر.")

    kind = invoice.kind
    partner_control = _control_account(session, company_id, kind, "partner")
    tax_control = _control_account(session, company_id, kind, "tax")

    description_prefix = CREDIT_LABELS_AR[kind] if invoice.is_credit else KIND_LABELS_AR[kind]
    entry = post_entry(
        session,
        company_id=company_id,
        user_id=user_id,
        entry_date=invoice.invoice_date,
        description=f"{description_prefix} - {invoice.partner.name}",
        lines=_entry_lines(invoice, partner_control, tax_control),
    )
    session.flush()  # assigns entry.id

    invoice.number = _next_number(session, company_id, kind, invoice.is_credit)
    invoice.status = InvoiceStatus.POSTED
    invoice.journal_entry_id = entry.id
    return invoice
