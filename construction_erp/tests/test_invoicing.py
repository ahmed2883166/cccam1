from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.accounting import PostingError
from app.db import Base
from app.invoicing import InvoiceError, LineDraft, compute_line, create_draft, post_invoice
from app.models import (
    Account,
    AccountType,
    Company,
    FiscalPeriod,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    JournalLine,
    Partner,
    PartnerKind,
    User,
)
from app.security import hash_password


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        company = Company(name="شركة اختبار")
        s.add(company)
        s.flush()
        user = User(company_id=company.id, username="u", full_name="م", password_hash=hash_password("x"), is_admin=True)
        s.add(user)
        s.add(FiscalPeriod(company_id=company.id, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)))
        s.add_all(
            [
                Account(company_id=company.id, code="1103", name_ar="العملاء", type=AccountType.ASSET),
                Account(company_id=company.id, code="1108", name_ar="ضريبة مدخلات", type=AccountType.ASSET),
                Account(company_id=company.id, code="1107", name_ar="المخزون", type=AccountType.ASSET),
                Account(company_id=company.id, code="2101", name_ar="الموردون", type=AccountType.LIABILITY),
                Account(company_id=company.id, code="2104", name_ar="ضريبة مخرجات", type=AccountType.LIABILITY),
                Account(company_id=company.id, code="4101", name_ar="إيراد عقود", type=AccountType.REVENUE),
                Account(company_id=company.id, code="5101", name_ar="تكلفة المواد", type=AccountType.EXPENSE),
                Account(company_id=company.id, code="1", name_ar="الأصول", type=AccountType.ASSET, is_postable=False),
            ]
        )
        s.add_all(
            [
                Partner(company_id=company.id, kind=PartnerKind.CUSTOMER, name="عميل أ", is_active=True),
                Partner(company_id=company.id, kind=PartnerKind.SUPPLIER, name="مورد ب", is_active=True),
            ]
        )
        s.commit()
        s.info["company_id"] = company.id
        s.info["user_id"] = user.id
        yield s


def _acc(session, code):
    return session.scalar(select(Account).where(Account.code == code, Account.company_id == session.info["company_id"]))


def _partner(session, kind):
    return session.scalar(
        select(Partner).where(Partner.kind == kind, Partner.company_id == session.info["company_id"])
    )


def _draft(session, kind, lines, partner_kind=None):
    partner = _partner(session, partner_kind or (PartnerKind.CUSTOMER if kind == InvoiceKind.SALES else PartnerKind.SUPPLIER))
    invoice = create_draft(
        session,
        company_id=session.info["company_id"],
        user_id=session.info["user_id"],
        kind=kind,
        partner_id=partner.id,
        invoice_date=date(2026, 3, 1),
        lines=lines,
    )
    session.commit()
    return invoice


def _line(session, code, qty="1", price="100", tax="15", desc="بند"):
    return LineDraft(
        account_id=_acc(session, code).id,
        description=desc,
        quantity=Decimal(qty),
        unit_price=Decimal(price),
        tax_rate=Decimal(tax),
    )


def test_compute_line_rounds_to_cents():
    net, tax, total = compute_line(Decimal("3"), Decimal("33.33"), Decimal("15"))
    assert net == Decimal("99.99")
    assert tax == Decimal("15.00")  # 14.9985 rounds to 15.00
    assert total == Decimal("114.99")


def test_draft_totals_are_computed(db):
    invoice = _draft(db, InvoiceKind.SALES, [_line(db, "4101", qty="2", price="500"), _line(db, "4101", price="100", tax="0")])
    assert invoice.subtotal == Decimal("1100.00")
    assert invoice.tax_total == Decimal("150.00")
    assert invoice.total == Decimal("1250.00")
    assert invoice.status == InvoiceStatus.DRAFT
    assert invoice.number is None


def test_sales_posting_creates_balanced_entry(db):
    invoice = _draft(db, InvoiceKind.SALES, [_line(db, "4101", qty="2", price="500")])
    post_invoice(db, company_id=db.info["company_id"], user_id=db.info["user_id"], invoice_id=invoice.id)
    db.commit()

    assert invoice.status == InvoiceStatus.POSTED
    assert invoice.number == 1
    lines = db.scalars(select(JournalLine).where(JournalLine.entry_id == invoice.journal_entry_id)).all()
    by_account = {}
    for line in lines:
        by_account.setdefault(line.account.code, Decimal("0"))
        by_account[line.account.code] += line.debit - line.credit
    assert by_account == {"1103": Decimal("1150.00"), "4101": Decimal("-1000.00"), "2104": Decimal("-150.00")}
    assert sum(l.debit for l in lines) == sum(l.credit for l in lines)


def test_purchase_posting_creates_balanced_entry(db):
    invoice = _draft(db, InvoiceKind.PURCHASE, [_line(db, "5101", qty="10", price="20")])
    post_invoice(db, company_id=db.info["company_id"], user_id=db.info["user_id"], invoice_id=invoice.id)
    db.commit()

    lines = db.scalars(select(JournalLine).where(JournalLine.entry_id == invoice.journal_entry_id)).all()
    by_account = {}
    for line in lines:
        by_account.setdefault(line.account.code, Decimal("0"))
        by_account[line.account.code] += line.debit - line.credit
    assert by_account == {"5101": Decimal("200.00"), "1108": Decimal("30.00"), "2101": Decimal("-230.00")}


def test_numbers_are_sequential_per_kind(db):
    a = _draft(db, InvoiceKind.SALES, [_line(db, "4101")])
    b = _draft(db, InvoiceKind.SALES, [_line(db, "4101")])
    p = _draft(db, InvoiceKind.PURCHASE, [_line(db, "5101")])
    for inv in (a, b, p):
        post_invoice(db, company_id=db.info["company_id"], user_id=db.info["user_id"], invoice_id=inv.id)
    db.commit()
    assert (a.number, b.number, p.number) == (1, 2, 1)


def test_posted_invoice_cannot_be_posted_again(db):
    invoice = _draft(db, InvoiceKind.SALES, [_line(db, "4101")])
    post_invoice(db, company_id=db.info["company_id"], user_id=db.info["user_id"], invoice_id=invoice.id)
    with pytest.raises(InvoiceError, match="مرحّلة"):
        post_invoice(db, company_id=db.info["company_id"], user_id=db.info["user_id"], invoice_id=invoice.id)


def test_customer_cannot_be_used_on_purchase(db):
    with pytest.raises(InvoiceError, match="نوع الطرف"):
        _draft(db, InvoiceKind.PURCHASE, [_line(db, "5101")], partner_kind=PartnerKind.CUSTOMER)


def test_sales_line_must_be_revenue(db):
    with pytest.raises(InvoiceError, match="ليس حساب إيراد"):
        _draft(db, InvoiceKind.SALES, [_line(db, "5101")])


def test_purchase_line_cannot_be_revenue(db):
    with pytest.raises(InvoiceError, match="ليس حساب مصروف"):
        _draft(db, InvoiceKind.PURCHASE, [_line(db, "4101")])


def test_invalid_tax_rate_is_rejected(db):
    with pytest.raises(InvoiceError, match="الضريبة"):
        _draft(db, InvoiceKind.SALES, [_line(db, "4101", tax="5")])


def test_invoice_needs_a_line(db):
    with pytest.raises(InvoiceError, match="سطر"):
        _draft(db, InvoiceKind.SALES, [])


def test_zero_quantity_is_rejected(db):
    with pytest.raises(InvoiceError, match="الكمية"):
        _draft(db, InvoiceKind.SALES, [_line(db, "4101", qty="0")])


def test_posting_in_closed_period_fails_and_stays_draft(db):
    period = db.scalar(select(FiscalPeriod))
    period.is_closed = True
    db.commit()
    invoice = _draft(db, InvoiceKind.SALES, [_line(db, "4101")])
    with pytest.raises(PostingError, match="مقفلة"):
        post_invoice(db, company_id=db.info["company_id"], user_id=db.info["user_id"], invoice_id=invoice.id)
    db.rollback()
    db.refresh(invoice)
    assert invoice.status == InvoiceStatus.DRAFT
    assert invoice.number is None
