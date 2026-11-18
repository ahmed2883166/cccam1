"""Chart of accounts: validation, creation, status changes, and tree/balance views."""

import re
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Account, AccountType, JournalLine

CODE_RE = re.compile(r"^[0-9]{1,20}$")

TYPE_LABELS_AR = {
    AccountType.ASSET: "أصول",
    AccountType.LIABILITY: "التزامات",
    AccountType.EQUITY: "حقوق ملكية",
    AccountType.REVENUE: "إيرادات",
    AccountType.EXPENSE: "مصروفات",
}


class ChartError(ValueError):
    """Raised when an account change breaks a chart-of-accounts rule."""


@dataclass(frozen=True)
class TreeRow:
    account: Account
    depth: int
    balance: Decimal  # net debit (positive) or net credit (negative); groups show their roll-up
    has_children: bool


def create_account(
    session: Session,
    *,
    company_id: int,
    code: str,
    name_ar: str,
    name_en: str | None,
    account_type: AccountType,
    parent_id: int | None,
    is_postable: bool,
) -> Account:
    code = (code or "").strip()
    name_ar = (name_ar or "").strip()
    if not CODE_RE.match(code):
        raise ChartError("كود الحساب لازم يكون أرقام فقط.")
    if not name_ar:
        raise ChartError("اسم الحساب بالعربي مطلوب.")

    duplicate = session.scalar(
        select(Account.id).where(Account.company_id == company_id, Account.code == code)
    )
    if duplicate is not None:
        raise ChartError(f"الكود {code} مستخدم بالفعل.")

    if parent_id:
        parent = session.scalar(
            select(Account).where(Account.id == parent_id, Account.company_id == company_id)
        )
        if parent is None:
            raise ChartError("الحساب الأب غير موجود.")
        if parent.is_postable:
            raise ChartError(f"الحساب {parent.code} قابل للقيد، والحسابات الفرعية تكون تحت حساب تجميعي فقط.")
        if parent.type != account_type:
            raise ChartError("نوع الحساب لازم يطابق نوع الحساب الأب.")
        if not (code.startswith(parent.code) and len(code) > len(parent.code)):
            raise ChartError(f"كود الحساب لازم يبدأ بكود الأب {parent.code} ويكون أطول منه.")

    account = Account(
        company_id=company_id,
        code=code,
        name_ar=name_ar,
        name_en=(name_en or "").strip() or None,
        type=account_type,
        parent_id=parent_id or None,
        is_postable=is_postable,
        is_active=True,
    )
    session.add(account)
    return account


def set_account_active(session: Session, *, company_id: int, account_id: int, active: bool) -> Account:
    account = session.scalar(select(Account).where(Account.id == account_id, Account.company_id == company_id))
    if account is None:
        raise ChartError("الحساب غير موجود.")
    if not active:
        active_children = session.scalar(
            select(func.count(Account.id)).where(Account.parent_id == account.id, Account.is_active.is_(True))
        )
        if active_children:
            raise ChartError("لا يمكن إيقاف حساب تجميعي وله حسابات فرعية نشطة.")
    account.is_active = active
    return account


def balances_by_account(session: Session, company_id: int) -> dict[int, Decimal]:
    rows = session.execute(
        select(JournalLine.account_id, func.sum(JournalLine.debit) - func.sum(JournalLine.credit))
        .join(Account, Account.id == JournalLine.account_id)
        .where(Account.company_id == company_id)
        .group_by(JournalLine.account_id)
    ).all()
    return {account_id: Decimal(str(net or 0)) for account_id, net in rows}


def build_tree(accounts: list[Account], balances: dict[int, Decimal]) -> list[TreeRow]:
    """Order accounts as a tree by code, indent by depth, and roll balances up into groups.

    An account whose parent is not in `accounts` (e.g. the parent was filtered out) is treated as a root.
    """
    known_ids = {a.id for a in accounts}
    children: dict[int, list[Account]] = {}
    roots: list[Account] = []
    for account in sorted(accounts, key=lambda a: a.code):
        if account.parent_id in known_ids:
            children.setdefault(account.parent_id, []).append(account)
        else:
            roots.append(account)

    rows: list[TreeRow] = []

    def walk(account: Account, depth: int) -> Decimal:
        index = len(rows)
        rows.append(None)  # placeholder, filled once the roll-up is known
        total = balances.get(account.id, Decimal("0"))
        for child in children.get(account.id, []):
            total += walk(child, depth + 1)
        rows[index] = TreeRow(
            account=account,
            depth=depth,
            balance=total,
            has_children=bool(children.get(account.id)),
        )
        return total

    for root in roots:
        walk(root, 0)
    return rows


def filter_flat(accounts: list[Account], balances: dict[int, Decimal], query: str, account_type: str) -> list[TreeRow]:
    """Flat list for searching or filtering by type (no hierarchy indentation)."""
    q = (query or "").strip().lower()
    rows = []
    for account in sorted(accounts, key=lambda a: a.code):
        if account_type and account.type.value != account_type:
            continue
        haystack = f"{account.code} {account.name_ar} {account.name_en or ''}".lower()
        if q and q not in haystack:
            continue
        rows.append(
            TreeRow(account=account, depth=0, balance=balances.get(account.id, Decimal("0")), has_children=False)
        )
    return rows
