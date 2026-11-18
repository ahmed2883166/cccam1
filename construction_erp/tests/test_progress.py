from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Account, AccountType, Company, FiscalPeriod, JournalLine, Partner, PartnerKind, User
from app.progress import ProgressError, certify_progress, create_project, project_summary
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
        for code, acc_type in [
            ("1104", AccountType.ASSET), ("1106", AccountType.ASSET), ("4101", AccountType.REVENUE),
            ("2104", AccountType.LIABILITY),
        ]:
            s.add(Account(company_id=company.id, code=code, name_ar=code, type=acc_type))
        s.add(Partner(company_id=company.id, kind=PartnerKind.CUSTOMER, name="مالك المشروع"))
        s.commit()
        s.info.update(company_id=company.id, user_id=user.id)
        yield s


def _project(db, retention="5", contract="100000"):
    client = db.scalar(select(Partner).where(Partner.name == "مالك المشروع"))
    project = create_project(
        db,
        company_id=db.info["company_id"],
        code="P-001",
        name="برج سكني",
        client_id=client.id,
        contract_value=Decimal(contract),
        retention_rate=Decimal(retention),
    )
    db.commit()
    return project


def _certify(db, project, cumulative, on=date(2026, 5, 31)):
    cert = certify_progress(
        db,
        company_id=db.info["company_id"],
        user_id=db.info["user_id"],
        project_id=project.id,
        cert_date=on,
        cumulative_value=Decimal(cumulative),
    )
    db.commit()
    return cert


def test_first_certificate_amounts(db):
    project = _project(db)
    cert = _certify(db, project, "20000")
    assert cert.number == 1
    assert cert.previous_value == Decimal("0")
    assert cert.net == Decimal("20000.00")
    assert cert.tax == Decimal("3000.00")
    assert cert.retention == Decimal("1000.00")  # 5%
    assert cert.amount_due == Decimal("22000.00")  # 23000 - 1000


def test_later_certificate_certifies_only_the_increment(db):
    project = _project(db)
    _certify(db, project, "20000")
    second = _certify(db, project, "50000", on=date(2026, 6, 30))
    assert second.number == 2
    assert second.previous_value == Decimal("20000.00")
    assert second.net == Decimal("30000.00")
    assert second.retention == Decimal("1500.00")


def test_entry_is_balanced_and_maps_accounts(db):
    project = _project(db)
    cert = _certify(db, project, "20000")
    lines = db.scalars(select(JournalLine).where(JournalLine.entry_id == cert.journal_entry_id)).all()
    assert sum(l.debit for l in lines) == sum(l.credit for l in lines) == Decimal("23000.00")
    by_code = {l.account.code: (l.debit, l.credit) for l in lines}
    assert by_code["1104"][0] == Decimal("22000.00")
    assert by_code["1106"][0] == Decimal("1000.00")
    assert by_code["4101"][1] == Decimal("20000.00")
    assert by_code["2104"][1] == Decimal("3000.00")


def test_cannot_exceed_contract_value(db):
    project = _project(db, contract="100000")
    with pytest.raises(ProgressError, match="أكبر من قيمة العقد"):
        _certify(db, project, "100001")


def test_cumulative_must_increase(db):
    project = _project(db)
    _certify(db, project, "20000")
    with pytest.raises(ProgressError, match="أكبر من آخر مستخلص"):
        _certify(db, project, "20000")


def test_zero_retention_creates_no_retention_line(db):
    project = _project(db, retention="0")
    cert = _certify(db, project, "10000")
    assert cert.retention == Decimal("0.00")
    lines = db.scalars(select(JournalLine).where(JournalLine.entry_id == cert.journal_entry_id)).all()
    assert all(l.account.code != "1106" for l in lines)


def test_summary_tracks_certified_retention_and_remaining(db):
    project = _project(db)
    _certify(db, project, "20000")
    _certify(db, project, "50000", on=date(2026, 6, 30))
    summary = project_summary(db, project)
    assert summary.certified_to_date == Decimal("50000.00")
    assert summary.retention_held == Decimal("2500.00")
    assert summary.remaining == Decimal("50000.00")


def test_project_requires_customer_and_unique_code(db):
    client = db.scalar(select(Partner).where(Partner.name == "مالك المشروع"))
    create_project(db, company_id=db.info["company_id"], code="P-9", name="أ", client_id=client.id,
                   contract_value=Decimal("1000"), retention_rate=Decimal("5"))
    db.commit()
    with pytest.raises(ProgressError, match="موجود بالفعل"):
        create_project(db, company_id=db.info["company_id"], code="P-9", name="ب", client_id=client.id,
                       contract_value=Decimal("1000"), retention_rate=Decimal("5"))
