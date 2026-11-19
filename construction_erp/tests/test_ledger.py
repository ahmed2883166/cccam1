from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.accounting import LineInput, post_entry
from app.db import Base
from app.ledger import account_ledger, entry_totals, list_entries
from app.models import Account, AccountType, Company, FiscalPeriod, User
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
        accs = {}
        for code, t in [("1102", AccountType.ASSET), ("3101", AccountType.EQUITY), ("5101", AccountType.EXPENSE)]:
            a = Account(company_id=company.id, code=code, name_ar=code, type=t)
            s.add(a)
            s.flush()
            accs[code] = a.id
        s.commit()
        s.info.update(company_id=company.id, user_id=user.id, accs=accs)
        yield s


def _post(db, on, desc, lines):
    entry = post_entry(db, company_id=db.info["company_id"], user_id=db.info["user_id"], entry_date=on, description=desc,
                       lines=[LineInput(db.info["accs"][c], debit=d, credit=cr) for c, d, cr in lines])
    db.commit()
    return entry


def test_ledger_running_balance_and_opening(db):
    cid = db.info["company_id"]
    _post(db, date(2026, 1, 2), "رأس مال", [("1102", Decimal("10000"), Decimal("0")), ("3101", Decimal("0"), Decimal("10000"))])
    _post(db, date(2026, 2, 5), "شراء", [("5101", Decimal("3000"), Decimal("0")), ("1102", Decimal("0"), Decimal("3000"))])
    _post(db, date(2026, 3, 9), "إيداع", [("1102", Decimal("500"), Decimal("0")), ("3101", Decimal("0"), Decimal("500"))])

    ledger = account_ledger(db, cid, db.info["accs"]["1102"], date(2026, 2, 1), date(2026, 12, 31))
    assert ledger.opening == Decimal("10000.00")
    assert [r.balance for r in ledger.rows] == [Decimal("7000.00"), Decimal("7500.00")]
    assert ledger.closing == Decimal("7500.00")
    assert ledger.total_debit == Decimal("500.00")
    assert ledger.total_credit == Decimal("3000.00")


def test_ledger_missing_account_returns_none(db):
    assert account_ledger(db, db.info["company_id"], 99999, date(2026, 1, 1), date(2026, 12, 31)) is None


def test_list_entries_filters_by_date_and_account(db):
    cid = db.info["company_id"]
    _post(db, date(2026, 1, 2), "أ", [("1102", Decimal("100"), Decimal("0")), ("3101", Decimal("0"), Decimal("100"))])
    _post(db, date(2026, 6, 2), "ب", [("5101", Decimal("50"), Decimal("0")), ("1102", Decimal("0"), Decimal("50"))])
    june = list_entries(db, cid, date(2026, 6, 1), date(2026, 6, 30))
    assert [e.description for e in june] == ["ب"]
    by_expense = list_entries(db, cid, date(2026, 1, 1), date(2026, 12, 31), account_id=db.info["accs"]["5101"])
    assert [e.description for e in by_expense] == ["ب"]


def test_entry_totals_balance(db):
    entry = _post(db, date(2026, 1, 2), "رأس مال", [("1102", Decimal("10000"), Decimal("0")), ("3101", Decimal("0"), Decimal("10000"))])
    debit, credit = entry_totals(entry)
    assert debit == credit == Decimal("10000")
