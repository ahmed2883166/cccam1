"""Financial statements built from posted journal lines: statement of financial position and income statement."""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Account, AccountType, JournalEntry, JournalLine

ZERO = Decimal("0")


@dataclass
class Line:
    code: str
    name: str
    amount: Decimal


@dataclass
class FinancialPosition:
    as_of: date
    assets: list[Line] = field(default_factory=list)
    liabilities: list[Line] = field(default_factory=list)
    equity: list[Line] = field(default_factory=list)
    profit_to_date: Decimal = ZERO

    @property
    def total_assets(self) -> Decimal:
        return sum((l.amount for l in self.assets), ZERO)

    @property
    def total_liabilities(self) -> Decimal:
        return sum((l.amount for l in self.liabilities), ZERO)

    @property
    def total_equity(self) -> Decimal:
        return sum((l.amount for l in self.equity), ZERO) + self.profit_to_date

    @property
    def difference(self) -> Decimal:
        """Zero when the statement balances."""
        return self.total_assets - self.total_liabilities - self.total_equity


@dataclass
class IncomeStatement:
    start: date
    end: date
    revenue: list[Line] = field(default_factory=list)
    expenses: list[Line] = field(default_factory=list)

    @property
    def total_revenue(self) -> Decimal:
        return sum((l.amount for l in self.revenue), ZERO)

    @property
    def total_expenses(self) -> Decimal:
        return sum((l.amount for l in self.expenses), ZERO)

    @property
    def net_profit(self) -> Decimal:
        return self.total_revenue - self.total_expenses


def _net_by_account(session: Session, company_id: int, start: date | None, end: date) -> dict[int, Decimal]:
    """Sum of debit minus credit per account for entries dated in [start, end]."""
    query = (
        select(JournalLine.account_id, func.sum(JournalLine.debit) - func.sum(JournalLine.credit))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .join(Account, Account.id == JournalLine.account_id)
        .where(Account.company_id == company_id, JournalEntry.entry_date <= end)
        .group_by(JournalLine.account_id)
    )
    if start is not None:
        query = query.where(JournalEntry.entry_date >= start)
    return {account_id: Decimal(str(net or 0)) for account_id, net in session.execute(query).all()}


def _accounts(session: Session, company_id: int) -> dict[int, Account]:
    accounts = session.scalars(select(Account).where(Account.company_id == company_id)).all()
    return {a.id: a for a in accounts if a.is_postable}


def financial_position(session: Session, company_id: int, as_of: date) -> FinancialPosition:
    accounts = _accounts(session, company_id)
    net = _net_by_account(session, company_id, None, as_of)
    report = FinancialPosition(as_of=as_of)
    for account_id, amount in sorted(net.items(), key=lambda kv: accounts[kv[0]].code if kv[0] in accounts else ""):
        account = accounts.get(account_id)
        if account is None or amount == 0:
            continue
        if account.type == AccountType.ASSET:
            report.assets.append(Line(account.code, account.name_ar, amount))
        elif account.type == AccountType.LIABILITY:
            report.liabilities.append(Line(account.code, account.name_ar, -amount))
        elif account.type == AccountType.EQUITY:
            report.equity.append(Line(account.code, account.name_ar, -amount))
        else:
            # Revenue and expense accounts are open until a closing entry, so their net
            # belongs to equity as profit to date.
            report.profit_to_date += -amount
    return report


def income_statement(session: Session, company_id: int, start: date, end: date) -> IncomeStatement:
    accounts = _accounts(session, company_id)
    net = _net_by_account(session, company_id, start, end)
    report = IncomeStatement(start=start, end=end)
    for account_id, amount in sorted(net.items(), key=lambda kv: accounts[kv[0]].code if kv[0] in accounts else ""):
        account = accounts.get(account_id)
        if account is None or amount == 0:
            continue
        if account.type == AccountType.REVENUE:
            report.revenue.append(Line(account.code, account.name_ar, -amount))
        elif account.type == AccountType.EXPENSE:
            report.expenses.append(Line(account.code, account.name_ar, amount))
    return report
