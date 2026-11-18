from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.accounting import LineInput, PostingError, post_entry
from app.db import Base
from app.models import Account, AccountType, Company, FiscalPeriod, JournalEntry, User
from app.security import hash_password, verify_password


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        company = Company(name="شركة اختبار")
        s.add(company)
        s.flush()
        user = User(company_id=company.id, username="u", full_name="مستخدم", password_hash=hash_password("x"))
        s.add(user)
        s.add(
            FiscalPeriod(
                company_id=company.id, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)
            )
        )
        s.add_all(
            [
                Account(company_id=company.id, code="1101", name_ar="الصندوق", type=AccountType.ASSET),
                Account(company_id=company.id, code="4101", name_ar="إيراد", type=AccountType.REVENUE),
                Account(company_id=company.id, code="1", name_ar="الأصول", type=AccountType.ASSET, is_postable=False),
            ]
        )
        s.commit()
        s.info["company_id"] = company.id
        s.info["user_id"] = user.id
        yield s


def _accounts(session):
    return {a.code: a.id for a in session.scalars(select(Account))}


def _post(session, lines, entry_date=date(2026, 3, 1), description="اختبار"):
    return post_entry(
        session,
        company_id=session.info["company_id"],
        user_id=session.info["user_id"],
        entry_date=entry_date,
        description=description,
        lines=lines,
    )


def test_balanced_entry_is_posted(session):
    ids = _accounts(session)
    entry = _post(
        session,
        [
            LineInput(ids["1101"], debit=Decimal("100.00")),
            LineInput(ids["4101"], credit=Decimal("100.00")),
        ],
    )
    session.commit()
    assert entry.number == 1
    assert len(entry.lines) == 2


def test_unbalanced_entry_is_rejected(session):
    ids = _accounts(session)
    with pytest.raises(PostingError, match="غير متوازن"):
        _post(
            session,
            [
                LineInput(ids["1101"], debit=Decimal("100.00")),
                LineInput(ids["4101"], credit=Decimal("99.99")),
            ],
        )


def test_line_cannot_be_both_debit_and_credit(session):
    ids = _accounts(session)
    with pytest.raises(PostingError):
        _post(
            session,
            [
                LineInput(ids["1101"], debit=Decimal("50"), credit=Decimal("50")),
                LineInput(ids["4101"], credit=Decimal("0.01")),
            ],
        )


def test_entry_needs_two_lines(session):
    ids = _accounts(session)
    with pytest.raises(PostingError, match="سطرين"):
        _post(session, [LineInput(ids["1101"], debit=Decimal("10"))])


def test_summary_account_cannot_be_posted(session):
    ids = _accounts(session)
    with pytest.raises(PostingError, match="تجميعي"):
        _post(
            session,
            [
                LineInput(ids["1"], debit=Decimal("10")),
                LineInput(ids["4101"], credit=Decimal("10")),
            ],
        )


def test_closed_period_is_rejected(session):
    period = session.scalar(select(FiscalPeriod))
    period.is_closed = True
    session.commit()
    ids = _accounts(session)
    with pytest.raises(PostingError, match="مقفلة"):
        _post(
            session,
            [
                LineInput(ids["1101"], debit=Decimal("10")),
                LineInput(ids["4101"], credit=Decimal("10")),
            ],
        )


def test_date_outside_any_period_is_rejected(session):
    ids = _accounts(session)
    with pytest.raises(PostingError, match="فترة"):
        _post(
            session,
            [
                LineInput(ids["1101"], debit=Decimal("10")),
                LineInput(ids["4101"], credit=Decimal("10")),
            ],
            entry_date=date(2030, 1, 1),
        )


def test_entry_numbers_increase_per_company(session):
    ids = _accounts(session)
    for _ in range(3):
        _post(
            session,
            [
                LineInput(ids["1101"], debit=Decimal("1")),
                LineInput(ids["4101"], credit=Decimal("1")),
            ],
        )
    session.commit()
    numbers = sorted(e.number for e in session.scalars(select(JournalEntry)))
    assert numbers == [1, 2, 3]


def test_password_hash_roundtrip():
    stored = hash_password("secret-123")
    assert verify_password("secret-123", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("secret-123", "garbage")
