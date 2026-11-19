from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.accounting import LineInput, post_entry
from app.db import Base
from app.models import Account, AccountType, Company, FiscalPeriod, User
from app.security import hash_password
from app.statements import financial_position, income_statement


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
        for code, t in [("1102", AccountType.ASSET), ("1104", AccountType.ASSET), ("2101", AccountType.LIABILITY),
                        ("3101", AccountType.EQUITY), ("4101", AccountType.REVENUE), ("5101", AccountType.EXPENSE)]:
            a = Account(company_id=company.id, code=code, name_ar=code, type=t)
            s.add(a)
            s.flush()
            accs[code] = a.id
        s.commit()
        s.info.update(company_id=company.id, user_id=user.id, accs=accs)
        yield s


def _post(db, on, lines):
    post_entry(db, company_id=db.info["company_id"], user_id=db.info["user_id"], entry_date=on,
               description="t", lines=[LineInput(db.info["accs"][c], debit=d, credit=cr) for c, d, cr in lines])
    db.commit()


def _seed(db):
    _post(db, date(2026, 2, 1), [("1102", Decimal("10000"), Decimal("0")), ("3101", Decimal("0"), Decimal("10000"))])
    _post(db, date(2026, 3, 1), [("1104", Decimal("5000"), Decimal("0")), ("4101", Decimal("0"), Decimal("5000"))])
    _post(db, date(2026, 4, 1), [("5101", Decimal("2000"), Decimal("0")), ("2101", Decimal("0"), Decimal("2000"))])


def test_financial_position_balances_with_profit_to_date(db):
    _seed(db)
    pos = financial_position(db, db.info["company_id"], date(2026, 12, 31))
    assert pos.total_assets == Decimal("15000")  # bank 10000 + receivable 5000
    assert pos.total_liabilities == Decimal("2000")
    assert pos.total_equity == Decimal("13000")  # capital 10000 + profit 3000
    assert pos.difference == 0


def test_position_respects_as_of_date(db):
    _seed(db)
    early = financial_position(db, db.info["company_id"], date(2026, 2, 28))
    assert early.total_assets == Decimal("10000")
    assert early.total_liabilities == Decimal("0")
    assert early.profit_to_date == Decimal("0")
    assert early.difference == 0


def test_income_statement_for_period(db):
    _seed(db)
    inc = income_statement(db, db.info["company_id"], date(2026, 1, 1), date(2026, 12, 31))
    assert inc.total_revenue == Decimal("5000")
    assert inc.total_expenses == Decimal("2000")
    assert inc.net_profit == Decimal("3000")


def test_income_statement_excludes_outside_period(db):
    _seed(db)
    inc = income_statement(db, db.info["company_id"], date(2026, 3, 15), date(2026, 12, 31))
    assert inc.total_revenue == Decimal("0")
    assert inc.total_expenses == Decimal("2000")
