"""Reports: receivables/payables aging and a VAT summary for a period."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Invoice, InvoiceKind, InvoiceStatus, PaymentKind
from .payments import open_invoices

ZERO = Decimal("0")

AGING_BUCKETS = [
    ("0 - 30 يوم", 0, 30),
    ("31 - 60 يوم", 31, 60),
    ("61 - 90 يوم", 61, 90),
    ("أكثر من 90 يوم", 91, None),
]


@dataclass
class AgingRow:
    name: str
    buckets: list[Decimal] = field(default_factory=lambda: [ZERO] * len(AGING_BUCKETS))

    @property
    def total(self) -> Decimal:
        return sum(self.buckets, ZERO)


def aging_report(session: Session, company_id: int, kind: PaymentKind, as_of: date) -> list[AgingRow]:
    """Open balances per partner, bucketed by days since the invoice date."""
    rows: dict[str, AgingRow] = {}
    for item in open_invoices(session, company_id, kind):
        days = (as_of - item.invoice.invoice_date).days
        index = next(
            i
            for i, (_, low, high) in enumerate(AGING_BUCKETS)
            if days >= low and (high is None or days <= high)
        ) if days >= 0 else 0  # invoices dated after as_of are shown in the first bucket
        name = item.invoice.partner.name
        row = rows.setdefault(name, AgingRow(name=name))
        row.buckets[index] += item.outstanding
    return sorted(rows.values(), key=lambda r: r.name)


def aging_totals(rows: list[AgingRow]) -> list[Decimal]:
    return [sum((r.buckets[i] for r in rows), ZERO) for i in range(len(AGING_BUCKETS))]


@dataclass
class VatSummary:
    start: date
    end: date
    sales_base_15: Decimal = ZERO
    sales_tax_15: Decimal = ZERO
    sales_base_0: Decimal = ZERO
    purchase_base_15: Decimal = ZERO
    purchase_tax_15: Decimal = ZERO
    purchase_base_0: Decimal = ZERO

    @property
    def output_vat(self) -> Decimal:
        return self.sales_tax_15

    @property
    def input_vat(self) -> Decimal:
        return self.purchase_tax_15

    @property
    def net_vat(self) -> Decimal:
        """Positive means payable to ZATCA; negative means refundable/carried forward."""
        return self.output_vat - self.input_vat


def vat_summary(session: Session, company_id: int, start: date, end: date) -> VatSummary:
    """Taxable bases and VAT for posted invoices and credit notes dated in the period.

    Credit notes subtract from the same buckets, so the summary is net of returns.
    """
    summary = VatSummary(start=start, end=end)
    invoices = session.scalars(
        select(Invoice).where(
            Invoice.company_id == company_id,
            Invoice.status == InvoiceStatus.POSTED,
            Invoice.invoice_date >= start,
            Invoice.invoice_date <= end,
        )
    ).all()
    for invoice in invoices:
        sign = Decimal("-1") if invoice.is_credit else Decimal("1")
        for line in invoice.lines:
            if invoice.kind == InvoiceKind.SALES:
                if line.tax_rate == 0:
                    summary.sales_base_0 += sign * line.net
                else:
                    summary.sales_base_15 += sign * line.net
                    summary.sales_tax_15 += sign * line.tax
            else:
                if line.tax_rate == 0:
                    summary.purchase_base_0 += sign * line.net
                else:
                    summary.purchase_base_15 += sign * line.net
                    summary.purchase_tax_15 += sign * line.tax
    return summary
