"""Headline figures for the home dashboard, all read from posted data."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .chart import balances_by_account
from .models import Account, PaymentKind, Project
from .payments import open_advances
from .progress import project_summary, retention_held
from .reports import aging_report, aging_totals, vat_summary
from .statements import income_statement

ZERO = Decimal("0")


@dataclass(frozen=True)
class Kpis:
    cash_and_bank: Decimal
    receivables: Decimal
    payables: Decimal
    retention_receivable: Decimal
    customer_advances: Decimal
    supplier_advances: Decimal
    overdue_receivables: Decimal  # older than 60 days
    vat_net_month: Decimal
    profit_ytd: Decimal
    active_projects: int
    remaining_contract_value: Decimal


def _account_balance(balances: dict[int, Decimal], accounts: dict[str, Account], code: str) -> Decimal:
    account = accounts.get(code)
    return balances.get(account.id, ZERO) if account else ZERO


def compute_kpis(session: Session, company_id: int, today: date) -> Kpis:
    balances = balances_by_account(session, company_id)
    accounts = {a.code: a for a in session.scalars(select(Account).where(Account.company_id == company_id)).all()}

    receivable_rows = aging_report(session, company_id, PaymentKind.RECEIPT, today)
    payable_rows = aging_report(session, company_id, PaymentKind.PAYMENT, today)
    receivables = sum(aging_totals(receivable_rows), ZERO)
    payables = sum(aging_totals(payable_rows), ZERO)
    overdue = sum(aging_totals(receivable_rows)[2:], ZERO)

    customer_adv = sum((rem for _, rem in open_advances(session, company_id, PaymentKind.RECEIPT)), ZERO)
    supplier_adv = sum((rem for _, rem in open_advances(session, company_id, PaymentKind.PAYMENT)), ZERO)

    month_start = today.replace(day=1)
    vat = vat_summary(session, company_id, month_start, today)

    year_start = date(today.year, 1, 1)
    profit = income_statement(session, company_id, year_start, today).net_profit

    projects = session.scalars(select(Project).where(Project.company_id == company_id, Project.is_active.is_(True))).all()
    retention = sum((retention_held(session, p) for p in projects), ZERO)
    remaining = sum((p.contract_value for p in projects), ZERO)
    remaining -= sum((_certified(session, p) for p in projects), ZERO)

    return Kpis(
        cash_and_bank=_account_balance(balances, accounts, "1101") + _account_balance(balances, accounts, "1102"),
        receivables=receivables,
        payables=payables,
        retention_receivable=retention,
        customer_advances=customer_adv,
        supplier_advances=supplier_adv,
        overdue_receivables=overdue,
        vat_net_month=vat.net_vat,
        profit_ytd=profit,
        active_projects=len(projects),
        remaining_contract_value=remaining,
    )


def _certified(session: Session, project: Project) -> Decimal:
    return project_summary(session, project).certified_to_date
