from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.accounting import LineInput  # noqa: F401  (imported to ensure accounting module loads)
from app.db import Base
from app.invoicing import InvoiceError, LineDraft, create_credit_note, create_draft, post_invoice
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
    PaymentKind,
    PaymentMethod,
    User,
)
from app.payments import PaymentError, apply_advance, open_advances, outstanding_amount, record_payment
from app.reports import aging_report, aging_totals, vat_summary
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
        s.flush()
        s.add(FiscalPeriod(company_id=company.id, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)))
        codes = [
            ("1101", AccountType.ASSET), ("1102", AccountType.ASSET), ("1103", AccountType.ASSET),
            ("1108", AccountType.ASSET), ("1109", AccountType.ASSET), ("2101", AccountType.LIABILITY),
            ("2103", AccountType.LIABILITY), ("2104", AccountType.LIABILITY), ("4101", AccountType.REVENUE),
            ("5101", AccountType.EXPENSE),
        ]
        for code, acc_type in codes:
            s.add(Account(company_id=company.id, code=code, name_ar=code, type=acc_type))
        s.add_all(
            [
                Partner(company_id=company.id, kind=PartnerKind.CUSTOMER, name="عميل أ"),
                Partner(company_id=company.id, kind=PartnerKind.SUPPLIER, name="مورد ج"),
            ]
        )
        s.commit()
        s.info.update(company_id=company.id, user_id=user.id)
        yield s


def _acc(db, code):
    return db.scalar(select(Account).where(Account.code == code, Account.company_id == db.info["company_id"]))


def _invoice(db, kind, partner_name, account_code, date_, qty="10", price="100", rate="15"):
    partner = db.scalar(select(Partner).where(Partner.name == partner_name))
    inv = create_draft(
        db,
        company_id=db.info["company_id"],
        user_id=db.info["user_id"],
        kind=kind,
        partner_id=partner.id,
        invoice_date=date_,
        lines=[LineDraft(_acc(db, account_code).id, "بند", Decimal(qty), Decimal(price), Decimal(rate))],
    )
    db.flush()
    post_invoice(db, company_id=db.info["company_id"], user_id=db.info["user_id"], invoice_id=inv.id)
    db.commit()
    return inv


def _line_id(inv):
    return inv.lines[0].id


def _credit(db, original, qty, on=date(2026, 5, 1)):
    credit = create_credit_note(
        db,
        company_id=db.info["company_id"],
        user_id=db.info["user_id"],
        original_id=original.id,
        credit_quantities={_line_id(original): Decimal(qty)},
        credit_date=on,
    )
    db.commit()
    return credit


# ---------- credit notes ----------

def test_credit_note_posts_reversal_and_reduces_outstanding(db):
    inv = _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 4, 1))  # total 1150
    credit = _credit(db, inv, "2")  # 200 + 30 VAT = 230
    assert credit.is_credit and credit.status == InvoiceStatus.POSTED
    assert credit.number == 1
    assert credit.total == Decimal("230.00")
    assert outstanding_amount(db, inv) == Decimal("920.00")


def test_credit_note_numbering_is_separate_from_invoices(db):
    inv = _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 4, 1))
    credit = _credit(db, inv, "1")
    assert inv.number == 1
    assert credit.number == 1


def test_credit_note_cannot_exceed_remaining_quantity(db):
    inv = _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 4, 1), qty="10")
    _credit(db, inv, "7")
    with pytest.raises(InvoiceError, match="أكبر من المتاح"):
        _credit(db, inv, "4")


def test_credit_note_entry_balances(db):
    inv = _invoice(db, InvoiceKind.PURCHASE, "مورد ج", "5101", date(2026, 4, 1))
    credit = _credit(db, inv, "10")  # full return
    lines = db.scalars(select(JournalLine).where(JournalLine.entry_id == credit.journal_entry_id)).all()
    assert sum(l.debit for l in lines) == sum(l.credit for l in lines) == Decimal("1150.00")


def test_cannot_credit_a_draft(db):
    partner = db.scalar(select(Partner).where(Partner.name == "عميل أ"))
    draft = create_draft(
        db,
        company_id=db.info["company_id"],
        user_id=db.info["user_id"],
        kind=InvoiceKind.SALES,
        partner_id=partner.id,
        invoice_date=date(2026, 4, 1),
        lines=[LineDraft(_acc(db, "4101").id, "بند", Decimal("1"), Decimal("100"), Decimal("0"))],
    )
    db.commit()
    with pytest.raises(InvoiceError, match="مرحّلة"):
        _credit(db, draft, "1")


# ---------- advances ----------

def test_advance_is_applied_to_invoice(db):
    inv = _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 4, 1))  # 1150
    payment = record_payment(
        db,
        company_id=db.info["company_id"],
        user_id=db.info["user_id"],
        kind=PaymentKind.RECEIPT,
        partner_id=inv.partner_id,
        payment_date=date(2026, 4, 2),
        method=PaymentMethod.BANK,
        amount=Decimal("500"),
        allocations=[],
    )
    db.commit()
    assert open_advances(db, db.info["company_id"], PaymentKind.RECEIPT)[0][1] == Decimal("500.00")

    apply_advance(
        db,
        company_id=db.info["company_id"],
        user_id=db.info["user_id"],
        payment_id=payment.id,
        invoice_id=inv.id,
        amount=Decimal("300"),
        apply_date=date(2026, 4, 3),
    )
    db.commit()
    assert outstanding_amount(db, inv) == Decimal("850.00")
    assert open_advances(db, db.info["company_id"], PaymentKind.RECEIPT)[0][1] == Decimal("200.00")


def test_advance_cannot_exceed_available_balance(db):
    inv = _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 4, 1))
    payment = record_payment(
        db,
        company_id=db.info["company_id"],
        user_id=db.info["user_id"],
        kind=PaymentKind.RECEIPT,
        partner_id=inv.partner_id,
        payment_date=date(2026, 4, 2),
        method=PaymentMethod.CASH,
        amount=Decimal("100"),
        allocations=[],
    )
    db.commit()
    with pytest.raises(PaymentError, match="الرصيد المقدم"):
        apply_advance(
            db,
            company_id=db.info["company_id"],
            user_id=db.info["user_id"],
            payment_id=payment.id,
            invoice_id=inv.id,
            amount=Decimal("150"),
            apply_date=date(2026, 4, 3),
        )


def test_supplier_advance_uses_asset_account(db):
    bill = _invoice(db, InvoiceKind.PURCHASE, "مورد ج", "5101", date(2026, 4, 1))  # 1150
    payment = record_payment(
        db,
        company_id=db.info["company_id"],
        user_id=db.info["user_id"],
        kind=PaymentKind.PAYMENT,
        partner_id=bill.partner_id,
        payment_date=date(2026, 4, 2),
        method=PaymentMethod.BANK,
        amount=Decimal("400"),
        allocations=[],
    )
    db.commit()
    lines = db.scalars(select(JournalLine).where(JournalLine.entry_id == payment.journal_entry_id)).all()
    by_code = {l.account.code: (l.debit, l.credit) for l in lines}
    assert by_code["1109"][0] == Decimal("400.00")
    assert by_code["1102"][1] == Decimal("400.00")


# ---------- reports ----------

def test_aging_buckets_by_invoice_age(db):
    _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 10, 20), qty="1", price="100", rate="0")  # 20 days
    _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 9, 1), qty="1", price="200", rate="0")  # 79 days
    _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 1, 1), qty="1", price="50", rate="0")  # >90 days
    rows = aging_report(db, db.info["company_id"], PaymentKind.RECEIPT, as_of=date(2026, 11, 9))
    assert len(rows) == 1
    assert rows[0].buckets == [Decimal("100.00"), Decimal("0"), Decimal("200.00"), Decimal("50.00")]
    assert aging_totals(rows) == [Decimal("100.00"), Decimal("0"), Decimal("200.00"), Decimal("50.00")]


def test_aging_excludes_settled_and_credited(db):
    inv = _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 10, 1), qty="1", price="100", rate="0")
    _credit(db, inv, "1", on=date(2026, 10, 2))
    rows = aging_report(db, db.info["company_id"], PaymentKind.RECEIPT, as_of=date(2026, 11, 9))
    assert rows == []


def test_vat_summary_nets_credit_notes(db):
    sale = _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 4, 1), qty="10", price="100", rate="15")
    _credit(db, sale, "2", on=date(2026, 4, 10))  # -200 base, -30 VAT
    _invoice(db, InvoiceKind.PURCHASE, "مورد ج", "5101", date(2026, 4, 5), qty="1", price="1000", rate="15")
    _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 7, 1), qty="1", price="999", rate="15")  # outside period

    summary = vat_summary(db, db.info["company_id"], date(2026, 4, 1), date(2026, 6, 30))
    assert summary.sales_base_15 == Decimal("800.00")
    assert summary.output_vat == Decimal("120.00")
    assert summary.purchase_base_15 == Decimal("1000.00")
    assert summary.input_vat == Decimal("150.00")
    assert summary.net_vat == Decimal("-30.00")


def test_vat_summary_separates_zero_rated(db):
    _invoice(db, InvoiceKind.SALES, "عميل أ", "4101", date(2026, 4, 1), qty="1", price="500", rate="0")
    summary = vat_summary(db, db.info["company_id"], date(2026, 4, 1), date(2026, 4, 30))
    assert summary.sales_base_0 == Decimal("500.00")
    assert summary.sales_base_15 == Decimal("0")
    assert summary.output_vat == Decimal("0")
