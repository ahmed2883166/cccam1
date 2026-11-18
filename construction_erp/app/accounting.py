"""Core posting rules. Every journal entry must pass through post_entry()."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Account, FiscalPeriod, JournalEntry, JournalLine

CENT = Decimal("0.01")


class PostingError(ValueError):
    """Raised when an entry breaks an accounting rule."""


@dataclass(frozen=True)
class LineInput:
    account_id: int
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    memo: str | None = None


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT)


def _check_period(session: Session, company_id: int, entry_date: date) -> None:
    period = session.scalar(
        select(FiscalPeriod).where(
            FiscalPeriod.company_id == company_id,
            FiscalPeriod.start_date <= entry_date,
            FiscalPeriod.end_date >= entry_date,
        )
    )
    if period is None:
        raise PostingError("لا توجد فترة مالية تغطي تاريخ القيد.")
    if period.is_closed:
        raise PostingError(f"الفترة المالية {period.name} مقفلة.")


def _check_lines(session: Session, company_id: int, lines: list[LineInput]) -> None:
    if len(lines) < 2:
        raise PostingError("القيد يحتاج سطرين على الأقل.")

    total_debit = Decimal("0")
    total_credit = Decimal("0")
    for line in lines:
        debit, credit = _money(line.debit), _money(line.credit)
        if debit < 0 or credit < 0:
            raise PostingError("لا يُسمح بقيم سالبة في القيد.")
        if debit > 0 and credit > 0:
            raise PostingError("السطر الواحد يكون مدينًا أو دائنًا، مش الاتنين.")
        if debit == 0 and credit == 0:
            raise PostingError("سطر بدون مبلغ.")
        total_debit += debit
        total_credit += credit

    if total_debit != total_credit:
        raise PostingError(
            f"القيد غير متوازن: المدين {total_debit} والدائن {total_credit}."
        )

    account_ids = {line.account_id for line in lines}
    accounts = session.scalars(
        select(Account).where(Account.company_id == company_id, Account.id.in_(account_ids))
    ).all()
    if len(accounts) != len(account_ids):
        raise PostingError("أحد الحسابات غير موجود في هذه الشركة.")
    for account in accounts:
        if not account.is_active:
            raise PostingError(f"الحساب {account.code} غير نشط.")
        if not account.is_postable:
            raise PostingError(f"الحساب {account.code} حساب تجميعي ولا يقبل قيود.")


def next_entry_number(session: Session, company_id: int) -> int:
    current = session.scalar(
        select(func.max(JournalEntry.number)).where(JournalEntry.company_id == company_id)
    )
    return (current or 0) + 1


def post_entry(
    session: Session,
    *,
    company_id: int,
    user_id: int,
    entry_date: date,
    description: str,
    lines: list[LineInput],
) -> JournalEntry:
    """Validate and add a balanced journal entry to the session.

    The caller commits. Raises PostingError if any rule is broken.
    """
    if not description.strip():
        raise PostingError("وصف القيد مطلوب.")
    _check_period(session, company_id, entry_date)
    _check_lines(session, company_id, lines)

    entry = JournalEntry(
        company_id=company_id,
        number=next_entry_number(session, company_id),
        entry_date=entry_date,
        description=description.strip(),
        created_by_id=user_id,
    )
    entry.lines = [
        JournalLine(
            account_id=line.account_id,
            debit=_money(line.debit),
            credit=_money(line.credit),
            memo=line.memo,
        )
        for line in lines
    ]
    session.add(entry)
    return entry
