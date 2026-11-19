from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.accounting import LineInput, post_entry
from app.dashboard import compute_kpis
from app.db import Base
from app.invoicing import LineDraft, create_draft, post_invoice
from app.models import Account, AccountType, Company, FiscalPeriod, InvoiceKind, Partner, PartnerKind, User
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
        for code, t in [("1101", AccountType.ASSET), ("1102", AccountType.ASSET), ("1103", AccountType.ASSET),
                        ("1108", AccountType.ASSET), ("1109", AccountType.ASSET), ("1106", AccountType.ASSET),
                        ("2101", AccountType.LIABILITY), ("2103", AccountType.LIABILITY), ("2104", AccountType.LIABILITY),
                        ("3101", AccountType.EQUITY), ("4101", AccountType.REVENUE), ("5101", AccountType.EXPENSE)]:
            s.add(Account(company_id=company.id, code=code, name_ar=code, type=t))
        s.add(Partner(company_id=company.id, kind=PartnerKind.CUSTOMER, name="عميل"))
        s.commit()
        s.info.update(company_id=company.id, user_id=user.id)
        yield s


def _acc(db, code):
    return db.scalar(select(Account).where(Account.code == code, Account.company_id == db.info["company_id"]))


def test_cash_and_receivables(db):
    cid, uid = db.info["company_id"], db.info["user_id"]
    post_entry(db, company_id=cid, user_id=uid, entry_date=date(2026, 1, 5), description="رأس مال",
               lines=[LineInput(_acc(db, "1102").id, debit=Decimal("10000")), LineInput(_acc(db, "3101").id, credit=Decimal("10000"))])
    customer = db.scalar(select(Partner).where(Partner.name == "عميل"))
    inv = create_draft(db, company_id=cid, user_id=uid, kind=InvoiceKind.SALES, partner_id=customer.id,
                       invoice_date=date(2026, 1, 10),
                       lines=[LineDraft(_acc(db, "4101").id, "خدمة", Decimal("1"), Decimal("1000"), Decimal("0"))])
    db.flush()
    post_invoice(db, company_id=cid, user_id=uid, invoice_id=inv.id)
    db.commit()

    k = compute_kpis(db, cid, date(2026, 1, 20))
    assert k.cash_and_bank == Decimal("10000.00")
    assert k.receivables == Decimal("1000.00")
    assert k.overdue_receivables == Decimal("0")
    assert k.profit_ytd == Decimal("1000.00")


def test_overdue_counts_only_over_60_days(db):
    cid, uid = db.info["company_id"], db.info["user_id"]
    customer = db.scalar(select(Partner).where(Partner.name == "عميل"))
    inv = create_draft(db, company_id=cid, user_id=uid, kind=InvoiceKind.SALES, partner_id=customer.id,
                       invoice_date=date(2026, 1, 1),
                       lines=[LineDraft(_acc(db, "4101").id, "خدمة", Decimal("1"), Decimal("500"), Decimal("0"))])
    db.flush()
    post_invoice(db, company_id=cid, user_id=uid, invoice_id=inv.id)
    db.commit()
    k = compute_kpis(db, cid, date(2026, 4, 1))  # 90 days old
    assert k.overdue_receivables == Decimal("500.00")
