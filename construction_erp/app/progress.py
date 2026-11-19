"""Projects and progress billing (مستخلصات): certified work, VAT, and retention."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .accounting import LineInput, post_entry
from .models import (
    Account,
    CollectionKind,
    Partner,
    PartnerKind,
    PaymentMethod,
    Project,
    ProgressCertificate,
    ProjectCollection,
)

CENT = Decimal("0.01")
HUNDRED = Decimal("100")
VAT_RATE = Decimal("15")

# Certified work is receivable (1104), retention is held by the client (1106),
# revenue is contract revenue (4101), and VAT is output VAT (2104).
CODE_CERTIFIED = "1104"
CODE_RETENTION = "1106"
CODE_REVENUE = "4101"
CODE_OUTPUT_VAT = "2104"
CASH_CODE = {PaymentMethod.CASH: "1101", PaymentMethod.BANK: "1102"}


class ProgressError(ValueError):
    """Raised when a project or certificate breaks a rule."""


@dataclass(frozen=True)
class ProjectSummary:
    contract_value: Decimal
    certified_to_date: Decimal
    retention_held: Decimal
    remaining: Decimal


def create_project(
    session: Session,
    *,
    company_id: int,
    code: str,
    name: str,
    client_id: int,
    contract_value: Decimal,
    retention_rate: Decimal,
) -> Project:
    code, name = (code or "").strip(), (name or "").strip()
    if not code or not name:
        raise ProgressError("كود المشروع واسمه مطلوبين.")
    if contract_value <= 0:
        raise ProgressError("قيمة العقد لازم تكون أكبر من صفر.")
    if not (Decimal("0") <= retention_rate <= HUNDRED):
        raise ProgressError("نسبة المحتجز لازم تكون بين 0 و 100.")
    client = session.scalar(select(Partner).where(Partner.id == client_id, Partner.company_id == company_id))
    if client is None or client.kind != PartnerKind.CUSTOMER or not client.is_active:
        raise ProgressError("العميل غير موجود أو مش عميل نشط.")
    if session.scalar(select(Project).where(Project.company_id == company_id, Project.code == code)):
        raise ProgressError("كود المشروع موجود بالفعل.")
    project = Project(
        company_id=company_id,
        code=code,
        name=name,
        client_id=client_id,
        contract_value=contract_value,
        retention_rate=retention_rate,
    )
    session.add(project)
    return project


def _account(session: Session, company_id: int, code: str) -> Account:
    account = session.scalar(select(Account).where(Account.company_id == company_id, Account.code == code))
    if account is None or not account.is_active:
        raise ProgressError(f"الحساب {code} غير موجود أو موقوف. راجع شجرة الحسابات.")
    return account


def _certified_cumulative(session: Session, project: Project) -> Decimal:
    value = session.scalar(
        select(func.coalesce(func.max(ProgressCertificate.cumulative_value), 0)).where(
            ProgressCertificate.project_id == project.id
        )
    )
    return Decimal(str(value))


def _next_number(session: Session, company_id: int) -> int:
    current = session.scalar(select(func.max(ProgressCertificate.number)).where(ProgressCertificate.company_id == company_id))
    return (current or 0) + 1


def certify_progress(
    session: Session,
    *,
    company_id: int,
    user_id: int,
    project_id: int,
    cert_date: date,
    cumulative_value: Decimal,
) -> ProgressCertificate:
    """Certify work done to date. The amount certified now is cumulative minus the previous certificate."""
    project = session.scalar(select(Project).where(Project.id == project_id, Project.company_id == company_id))
    if project is None:
        raise ProgressError("المشروع غير موجود.")
    if not project.is_active:
        raise ProgressError("المشروع موقوف.")
    if cumulative_value > project.contract_value:
        raise ProgressError(f"الإنجاز التراكمي أكبر من قيمة العقد ({project.contract_value}).")
    previous = _certified_cumulative(session, project)
    current = (cumulative_value - previous).quantize(CENT)
    if current <= 0:
        raise ProgressError(f"الإنجاز التراكمي لازم يكون أكبر من آخر مستخلص ({previous}).")

    net = current
    tax = (net * VAT_RATE / HUNDRED).quantize(CENT)
    retention = (net * project.retention_rate / HUNDRED).quantize(CENT)
    amount_due = net + tax - retention

    certified_acc = _account(session, company_id, CODE_CERTIFIED)
    retention_acc = _account(session, company_id, CODE_RETENTION)
    revenue_acc = _account(session, company_id, CODE_REVENUE)
    tax_acc = _account(session, company_id, CODE_OUTPUT_VAT)

    lines = [LineInput(certified_acc.id, debit=amount_due)]
    if retention:
        lines.append(LineInput(retention_acc.id, debit=retention))
    lines += [LineInput(revenue_acc.id, credit=net), LineInput(tax_acc.id, credit=tax)]

    entry = post_entry(
        session,
        company_id=company_id,
        user_id=user_id,
        entry_date=cert_date,
        description=f"مستخلص - {project.code} {project.name}",
        lines=[line for line in lines if line.debit or line.credit],
    )
    session.flush()

    certificate = ProgressCertificate(
        company_id=company_id,
        project_id=project.id,
        number=_next_number(session, company_id),
        cert_date=cert_date,
        cumulative_value=cumulative_value,
        previous_value=previous,
        net=net,
        tax=tax,
        retention=retention,
        amount_due=amount_due,
        journal_entry_id=entry.id,
        created_by_id=user_id,
    )
    session.add(certificate)
    return certificate


def _collected(session: Session, *, project_id: int, kind: CollectionKind, certificate_id: int | None = None) -> Decimal:
    query = select(func.coalesce(func.sum(ProjectCollection.amount), 0)).where(
        ProjectCollection.project_id == project_id, ProjectCollection.kind == kind
    )
    if certificate_id is not None:
        query = query.where(ProjectCollection.certificate_id == certificate_id)
    return Decimal(str(session.scalar(query)))


def certificate_outstanding(session: Session, certificate: ProgressCertificate) -> Decimal:
    """Amount still owed on a certificate (amount due minus cash collected against it)."""
    collected = _collected(
        session, project_id=certificate.project_id, kind=CollectionKind.CERTIFICATE, certificate_id=certificate.id
    )
    return (certificate.amount_due - collected).quantize(CENT)


def retention_held(session: Session, project: Project) -> Decimal:
    """Retention deducted on certificates and not yet released to the contractor."""
    deducted = session.scalar(
        select(func.coalesce(func.sum(ProgressCertificate.retention), 0)).where(
            ProgressCertificate.project_id == project.id
        )
    )
    released = _collected(session, project_id=project.id, kind=CollectionKind.RETENTION)
    return Decimal(str(deducted)) - released


def collect_from_client(
    session: Session,
    *,
    company_id: int,
    user_id: int,
    project_id: int,
    kind: CollectionKind,
    method: PaymentMethod,
    collection_date: date,
    amount: Decimal,
    certificate_id: int | None = None,
) -> ProjectCollection:
    """Record cash from the client: against a certificate (clears 1104) or as released retention (clears 1106)."""
    project = session.scalar(select(Project).where(Project.id == project_id, Project.company_id == company_id))
    if project is None:
        raise ProgressError("المشروع غير موجود.")
    if amount <= 0:
        raise ProgressError("المبلغ لازم يكون أكبر من صفر.")

    if kind == CollectionKind.CERTIFICATE:
        if certificate_id is None:
            raise ProgressError("اختار المستخلص اللي بيتحصّل منه.")
        certificate = session.scalar(
            select(ProgressCertificate).where(
                ProgressCertificate.id == certificate_id,
                ProgressCertificate.project_id == project.id,
                ProgressCertificate.company_id == company_id,
            )
        )
        if certificate is None:
            raise ProgressError("المستخلص غير موجود في هذا المشروع.")
        open_amount = certificate_outstanding(session, certificate)
        if amount > open_amount:
            raise ProgressError(f"المبلغ أكبر من المتبقي على المستخلص ({open_amount}).")
        credit_code = CODE_CERTIFIED
    else:
        certificate_id = None
        held = retention_held(session, project)
        if amount > held:
            raise ProgressError(f"المبلغ أكبر من المحتجز المتاح ({held}).")
        credit_code = CODE_RETENTION

    cash = _account(session, company_id, CASH_CODE[method])
    credited = _account(session, company_id, credit_code)
    entry = post_entry(
        session,
        company_id=company_id,
        user_id=user_id,
        entry_date=collection_date,
        description=f"تحصيل من العميل - {project.code} ({'مستخلص' if kind == CollectionKind.CERTIFICATE else 'محتجز'})",
        lines=[LineInput(cash.id, debit=amount), LineInput(credited.id, credit=amount)],
    )
    session.flush()

    collection = ProjectCollection(
        company_id=company_id,
        project_id=project.id,
        certificate_id=certificate_id,
        kind=kind,
        method=method,
        collection_date=collection_date,
        amount=amount,
        journal_entry_id=entry.id,
        created_by_id=user_id,
    )
    session.add(collection)
    return collection


def project_summary(session: Session, project: Project) -> ProjectSummary:
    certified = _certified_cumulative(session, project)
    return ProjectSummary(
        contract_value=project.contract_value,
        certified_to_date=certified,
        retention_held=retention_held(session, project),
        remaining=project.contract_value - certified,
    )
