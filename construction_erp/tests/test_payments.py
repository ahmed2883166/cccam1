from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.invoicing import LineDraft, create_draft, post_invoice
from app.models import (
    Account,
    AccountType,
    Company,
    FiscalPeriod,
    Invoice,
    InvoiceKind,
    JournalLine,
    Partner,
    PartnerKind,
    PaymentKind,
    PaymentMethod,
    User,
)
from app.payments import PaymentError, open_invoices, record_payment
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
                Account(company_id=company.id, code="1101", name_ar="الصندوق", type=AccountType.ASSET),
                Account(company_id=company.id, code="1102", name_ar="البنك", type=AccountType.ASSET),
                Account(company_id=company.id, code="1103", name_ar="العملاء", type=AccountType.ASSET),
                Account(company_id=company.id, code="1108", name_ar="ضريبة مدخلات", type=AccountType.ASSET),
                Account(company_id=company.id, code="2101", name_ar="الموردون", type=AccountType.LIABILITY),
                Account(company_id=company.id, code="2104", name_ar="ضريبة مخرجات", type=AccountType.LIABILITY),
                Account(company_id=company.id, code="4101", name_ar="إيراد", type=AccountType.REVENUE),
                Account(company_id=company.id, code="5101", name_ar="مواد", type=AccountType.EXPENSE),
            ]
        )
        s.add_all(
            [
                Partner(company_id=company.id, kind=PartnerKind.CUSTOMER, name="عميل أ"),
                Partner(company_id=company.id, kind=PartnerKind.CUSTOMER, name="عميل ب"),
                Partner(company_id=company.id, kind=PartnerKind.SUPPLIER, name="مورد ج"),
            ]
        )
        s.commit()
        s.info["company_id"] = company.id
        s.info["user_id"] = user.id
        yield s


def _acc(s, code):
    return s.scalar(select(Account).where(Account.code == code, Account.company_id == s.info["company_id"]))


def _partner(s, name):
    return s.scalar(select(Partner).where(Partner.name == name, Partner.company_id == s.info["company_id"]))


def _posted_invoice(s, kind, partner_name, account_code, qty="1", price="1000", tax="15"):
    draft = create_draft(
        s,
        company_id=s.info["company_id"],
        user_id=s.info["user_id"],
        kind=kind,
        partner_id=_partner(s, partner_name).id,
        invoice_date=date(2026, 3, 1),
        lines=[
            LineDraft(
                account_id=_acc(s, account_code).id,
                description="بند",
                quantity=Decimal(qty),
                unit_price=Decimal(price),
                tax_rate=Decimal(tax),
            )
        ],
    )
    s.commit()  # assigns draft.id
    post_invoice(s, company_id=s.info["company_id"], user_id=s.info["user_id"], invoice_id=draft.id)
    s.commit()
    return draft


def _receipt(s, partner_name, amount, allocations, method=PaymentMethod.BANK):
    return record_payment(
        s,
        company_id=s.info["company_id"],
        user_id=s.info["user_id"],
        kind=PaymentKind.RECEIPT,
        partner_id=_partner(s, partner_name).id,
        payment_date=date(2026, 4, 1),
        method=method,
        amount=Decimal(amount),
        allocations=[(inv.id, Decimal(a)) for inv, a in allocations],
    )


def _payment(s, partner_name, amount, allocations):
    return record_payment(
        s,
        company_id=s.info["company_id"],
        user_id=s.info["user_id"],
        kind=PaymentKind.PAYMENT,
        partner_id=_partner(s, partner_name).id,
        payment_date=date(2026, 4, 1),
        method=PaymentMethod.CASH,
        amount=Decimal(amount),
        allocations=[(inv.id, Decimal(a)) for inv, a in allocations],
    )


def _net_by_code(s, entry_id):
    lines = s.scalars(select(JournalLine).where(JournalLine.entry_id == entry_id)).all()
    out = {}
    for l in lines:
        out.setdefault(l.account.code, Decimal("0"))
        out[l.account.code] += l.debit - l.credit
    return out


def test_full_receipt_clears_invoice(db):
    inv = _posted_invoice(db, InvoiceKind.SALES, "عميل أ", "4101")  # total 1150
    payment = _receipt(db, "عميل أ", "1150.00", [(inv, "1150.00")])
    db.commit()
    assert open_invoices(db, db.info["company_id"], PaymentKind.RECEIPT) == []
    assert payment.number == 1


def test_receipt_entry_debits_bank_and_credits_receivables(db):
    inv = _posted_invoice(db, InvoiceKind.SALES, "عميل أ", "4101")
    payment = _receipt(db, "عميل أ", "500.00", [(inv, "500.00")], method=PaymentMethod.BANK)
    db.commit()
    assert _net_by_code(db, payment.journal_entry_id) == {"1102": Decimal("500.00"), "1103": Decimal("-500.00")}


def test_partial_receipt_leaves_outstanding(db):
    inv = _posted_invoice(db, InvoiceKind.SALES, "عميل أ", "4101")  # 1150
    _receipt(db, "عميل أ", "400.00", [(inv, "400.00")])
    db.commit()
    items = open_invoices(db, db.info["company_id"], PaymentKind.RECEIPT)
    assert len(items) == 1
    assert items[0].outstanding == Decimal("750.00")


def test_payment_to_supplier_debits_payables(db):
    bill = _posted_invoice(db, InvoiceKind.PURCHASE, "مورد ج", "5101", qty="2", price="500")  # total 1150
    payment = _payment(db, "مورد ج", "1150.00", [(bill, "1150.00")])
    db.commit()
    assert _net_by_code(db, payment.journal_entry_id) == {"2101": Decimal("1150.00"), "1101": Decimal("-1150.00")}


def test_allocation_cannot_exceed_outstanding(db):
    inv = _posted_invoice(db, InvoiceKind.SALES, "عميل أ", "4101")  # 1150
    _receipt(db, "عميل أ", "1000.00", [(inv, "1000.00")])
    db.commit()
    with pytest.raises(PaymentError, match="أكبر من المتبقي"):
        _receipt(db, "عميل أ", "200.00", [(inv, "200.00")])


def test_allocations_must_equal_amount(db):
    inv = _posted_invoice(db, InvoiceKind.SALES, "عميل أ", "4101")
    with pytest.raises(PaymentError, match="يساوي"):
        _receipt(db, "عميل أ", "500.00", [(inv, "400.00")])


def test_invoice_must_belong_to_partner(db):
    inv = _posted_invoice(db, InvoiceKind.SALES, "عميل أ", "4101")
    with pytest.raises(PaymentError, match="لا تخص هذا الطرف"):
        _receipt(db, "عميل ب", "100.00", [(inv, "100.00")])


def test_receipt_cannot_settle_purchase_invoice(db):
    bill = _posted_invoice(db, InvoiceKind.PURCHASE, "مورد ج", "5101")
    with pytest.raises(PaymentError):
        record_payment(
            db,
            company_id=db.info["company_id"],
            user_id=db.info["user_id"],
            kind=PaymentKind.RECEIPT,
            partner_id=_partner(db, "مورد ج").id,
            payment_date=date(2026, 4, 1),
            method=PaymentMethod.BANK,
            amount=Decimal("100"),
            allocations=[(bill.id, Decimal("100"))],
        )


def test_unposted_invoice_cannot_be_settled(db):
    draft = create_draft(
        db,
        company_id=db.info["company_id"],
        user_id=db.info["user_id"],
        kind=InvoiceKind.SALES,
        partner_id=_partner(db, "عميل أ").id,
        invoice_date=date(2026, 3, 1),
        lines=[LineDraft(_acc(db, "4101").id, "بند", Decimal("1"), Decimal("100"), Decimal("0"))],
    )
    db.commit()
    with pytest.raises(PaymentError, match="لم تُرحَّل"):
        _receipt(db, "عميل أ", "100", [(draft, "100")])


def test_payment_numbers_are_sequential_per_kind(db):
    inv1 = _posted_invoice(db, InvoiceKind.SALES, "عميل أ", "4101")
    inv2 = _posted_invoice(db, InvoiceKind.SALES, "عميل أ", "4101")
    r1 = _receipt(db, "عميل أ", "100", [(inv1, "100")])
    r2 = _receipt(db, "عميل أ", "100", [(inv2, "100")])
    db.commit()
    assert (r1.number, r2.number) == (1, 2)


def test_open_invoices_reflects_all_partners(db):
    _posted_invoice(db, InvoiceKind.SALES, "عميل أ", "4101")
    _posted_invoice(db, InvoiceKind.SALES, "عميل ب", "4101")
    db.commit()
    items = open_invoices(db, db.info["company_id"], PaymentKind.RECEIPT)
    assert len(items) == 2
    only_b = open_invoices(db, db.info["company_id"], PaymentKind.RECEIPT, partner_id=_partner(db, "عميل ب").id)
    assert len(only_b) == 1
