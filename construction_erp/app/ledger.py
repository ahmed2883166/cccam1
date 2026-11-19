"""Read-only views of posted journal entries: entry list, entry detail, and account ledger with running balance."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Account, JournalEntry, JournalLine

ZERO = Decimal("0")


@dataclass(frozen=True)
class LedgerRow:
    entry_id: int
    entry_number: int
    entry_date: date
    description: str
    memo: str | None
    debit: Decimal
    credit: Decimal
    balance: Decimal


@dataclass(frozen=True)
class Ledger:
    account: Account
    start: date
    end: date
    opening: Decimal
    rows: list[LedgerRow]

    @property
    def closing(self) -> Decimal:
        return self.rows[-1].balance if self.rows else self.opening

    @property
    def total_debit(self) -> Decimal:
        return sum((r.debit for r in self.rows), ZERO)

    @property
    def total_credit(self) -> Decimal:
        return sum((r.credit for r in self.rows), ZERO)


def list_entries(
    session: Session, company_id: int, start: date, end: date, account_id: int | None = None, limit: int = 200
) -> list[JournalEntry]:
    query = select(JournalEntry).where(
        JournalEntry.company_id == company_id,
        JournalEntry.entry_date >= start,
        JournalEntry.entry_date <= end,
    )
    if account_id:
        query = query.where(JournalEntry.id.in_(select(JournalLine.entry_id).where(JournalLine.account_id == account_id)))
    return list(
        session.scalars(query.order_by(JournalEntry.entry_date.desc(), JournalEntry.number.desc()).limit(limit)).all()
    )


def entry_totals(entry: JournalEntry) -> tuple[Decimal, Decimal]:
    return (
        sum((l.debit for l in entry.lines), ZERO),
        sum((l.credit for l in entry.lines), ZERO),
    )


def account_ledger(session: Session, company_id: int, account_id: int, start: date, end: date) -> Ledger | None:
    account = session.scalar(select(Account).where(Account.id == account_id, Account.company_id == company_id))
    if account is None:
        return None

    opening_net = session.scalar(
        select(func.coalesce(func.sum(JournalLine.debit) - func.sum(JournalLine.credit), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(JournalLine.account_id == account.id, JournalEntry.entry_date < start)
    )
    opening = Decimal(str(opening_net or 0))

    lines = session.execute(
        select(JournalLine, JournalEntry)
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .where(
            JournalLine.account_id == account.id,
            JournalEntry.entry_date >= start,
            JournalEntry.entry_date <= end,
        )
        .order_by(JournalEntry.entry_date, JournalEntry.number, JournalLine.id)
    ).all()

    rows: list[LedgerRow] = []
    running = opening
    for line, entry in lines:
        running += line.debit - line.credit
        rows.append(
            LedgerRow(
                entry_id=entry.id,
                entry_number=entry.number,
                entry_date=entry.entry_date,
                description=entry.description,
                memo=line.memo,
                debit=line.debit,
                credit=line.credit,
                balance=running,
            )
        )
    return Ledger(account=account, start=start, end=end, opening=opening, rows=rows)
