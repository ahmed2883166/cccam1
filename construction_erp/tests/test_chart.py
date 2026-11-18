from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.accounting import LineInput, post_entry
from app.chart import ChartError, balances_by_account, build_tree, create_account, filter_flat, set_account_active
from app.db import Base
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
        s.add(FiscalPeriod(company_id=company.id, name="2026", start_date=date(2026, 1, 1), end_date=date(2026, 12, 31)))
        s.commit()
        s.info["company_id"] = company.id
        s.info["user_id"] = user.id
        yield s


def _new(session, code, name, acc_type, parent_id=None, postable=True):
    account = create_account(
        session,
        company_id=session.info["company_id"],
        code=code,
        name_ar=name,
        name_en=None,
        account_type=acc_type,
        parent_id=parent_id,
        is_postable=postable,
    )
    session.flush()
    return account


def _seed_assets(session):
    assets = _new(session, "1", "الأصول", AccountType.ASSET, postable=False)
    current = _new(session, "11", "الأصول المتداولة", AccountType.ASSET, assets.id, postable=False)
    cash = _new(session, "1101", "الصندوق", AccountType.ASSET, current.id)
    bank = _new(session, "1102", "البنك", AccountType.ASSET, current.id)
    session.commit()
    return assets, current, cash, bank


def test_create_valid_child_account(db):
    _, current, _, _ = _seed_assets(db)
    assert current.code == "11"


def test_duplicate_code_is_rejected(db):
    _seed_assets(db)
    with pytest.raises(ChartError, match="مستخدم بالفعل"):
        _new(db, "1101", "تكرار", AccountType.ASSET)


def test_code_must_be_digits(db):
    with pytest.raises(ChartError, match="أرقام فقط"):
        _new(db, "11A", "كود غلط", AccountType.ASSET)


def test_name_is_required(db):
    with pytest.raises(ChartError, match="اسم"):
        _new(db, "2001", "   ", AccountType.ASSET)


def test_child_cannot_go_under_postable_parent(db):
    _, _, cash, _ = _seed_assets(db)
    with pytest.raises(ChartError, match="قابل للقيد"):
        _new(db, "110101", "فرعي", AccountType.ASSET, cash.id)


def test_child_type_must_match_parent(db):
    _, current, _, _ = _seed_assets(db)
    with pytest.raises(ChartError, match="نوع"):
        _new(db, "1103", "التزام", AccountType.LIABILITY, current.id)


def test_child_code_must_extend_parent_code(db):
    _, current, _, _ = _seed_assets(db)
    with pytest.raises(ChartError, match="يبدأ بكود الأب"):
        _new(db, "2201", "كود مختلف", AccountType.ASSET, current.id)


def test_cannot_deactivate_group_with_active_children(db):
    _, current, _, _ = _seed_assets(db)
    with pytest.raises(ChartError, match="فرعية نشطة"):
        set_account_active(db, company_id=db.info["company_id"], account_id=current.id, active=False)


def test_can_deactivate_leaf_and_reactivate(db):
    _, _, cash, _ = _seed_assets(db)
    set_account_active(db, company_id=db.info["company_id"], account_id=cash.id, active=False)
    assert cash.is_active is False
    set_account_active(db, company_id=db.info["company_id"], account_id=cash.id, active=True)
    assert cash.is_active is True


def test_tree_is_ordered_and_indented(db):
    _seed_assets(db)
    accounts = db.query(Account).all()
    rows = build_tree(accounts, {})
    assert [(r.account.code, r.depth) for r in rows] == [("1", 0), ("11", 1), ("1101", 2), ("1102", 2)]


def test_group_balances_roll_up_from_postings(db):
    assets, current, cash, bank = _seed_assets(db)
    equity = _new(db, "3101", "رأس المال", AccountType.EQUITY)
    db.commit()
    post_entry(
        db,
        company_id=db.info["company_id"],
        user_id=db.info["user_id"],
        entry_date=date(2026, 3, 1),
        description="إيداع رأس المال",
        lines=[
            LineInput(cash.id, debit=Decimal("300.00")),
            LineInput(bank.id, debit=Decimal("200.00")),
            LineInput(equity.id, credit=Decimal("500.00")),
        ],
    )
    db.commit()
    balances = balances_by_account(db, db.info["company_id"])
    rows = {r.account.code: r for r in build_tree(db.query(Account).all(), balances)}
    assert rows["1101"].balance == Decimal("300.00")
    assert rows["11"].balance == Decimal("500.00")
    assert rows["1"].balance == Decimal("500.00")
    assert rows["3101"].balance == Decimal("-500.00")


def test_flat_filter_by_text_and_type(db):
    _seed_assets(db)
    accounts = db.query(Account).all()
    by_text = filter_flat(accounts, {}, "بنك", "")
    assert [r.account.code for r in by_text] == ["1102"]
    by_type = filter_flat(accounts, {}, "", "asset")
    assert {r.account.code for r in by_type} == {"1", "11", "1101", "1102"}
    assert all(r.depth == 0 for r in by_type)
