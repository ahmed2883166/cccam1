from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class AccountType(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3), default="SAR")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    username: Mapped[str] = mapped_column(String(80), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("company_id", "code", name="uq_account_code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))
    name_ar: Mapped[str] = mapped_column(String(200))
    name_en: Mapped[str | None] = mapped_column(String(200), nullable=True)
    type: Mapped[AccountType] = mapped_column(SAEnum(AccountType, native_enum=False, length=20))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    is_postable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    parent: Mapped["Account | None"] = relationship(remote_side="Account.id")


class FiscalPeriod(Base):
    __tablename__ = "fiscal_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String(50))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)


class JournalEntry(Base):
    __tablename__ = "journal_entries"
    __table_args__ = (UniqueConstraint("company_id", "number", name="uq_entry_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    number: Mapped[int] = mapped_column()
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(String(500))
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", order_by="JournalLine.id"
    )


class JournalLine(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit >= 0 AND credit >= 0", name="ck_line_non_negative"),
        CheckConstraint("NOT (debit > 0 AND credit > 0)", name="ck_line_one_side"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    debit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    credit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    memo: Mapped[str | None] = mapped_column(String(300), nullable=True)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()


class PartnerKind(str, Enum):
    CUSTOMER = "customer"
    SUPPLIER = "supplier"


class InvoiceKind(str, Enum):
    SALES = "sales"
    PURCHASE = "purchase"


class InvoiceStatus(str, Enum):
    DRAFT = "draft"
    POSTED = "posted"


class Partner(Base):
    """A customer or a supplier."""

    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[PartnerKind] = mapped_column(SAEnum(PartnerKind, native_enum=False, length=20))
    name: Mapped[str] = mapped_column(String(200))
    tax_number: Mapped[str | None] = mapped_column(String(50), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("company_id", "kind", "number", name="uq_invoice_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    kind: Mapped[InvoiceKind] = mapped_column(SAEnum(InvoiceKind, native_enum=False, length=20))
    status: Mapped[InvoiceStatus] = mapped_column(
        SAEnum(InvoiceStatus, native_enum=False, length=20), default=InvoiceStatus.DRAFT
    )
    number: Mapped[int | None] = mapped_column(nullable=True)  # assigned when posted
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"))
    invoice_date: Mapped[date] = mapped_column(Date, index=True)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    tax_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"))
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"), nullable=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    partner: Mapped[Partner] = relationship()
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", order_by="InvoiceLine.id"
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    description: Mapped[str] = mapped_column(String(300))
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 3))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    net: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    tax: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2))

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
    account: Mapped[Account] = relationship()
