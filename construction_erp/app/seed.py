"""Create tables and a demo company with a construction-oriented chart of accounts.

Run once, after `alembic upgrade head`:  python -m app.seed
"""

import os
from datetime import date

from sqlalchemy import select

from . import models  # noqa: F401  (registers tables on Base.metadata)
from .db import SessionLocal
from .models import Account, AccountType, Company, FiscalPeriod, User
from .security import hash_password

# (code, Arabic name, English name, type, parent code or None, postable)
CHART = [
    ("1", "الأصول", "Assets", AccountType.ASSET, None, False),
    ("11", "الأصول المتداولة", "Current assets", AccountType.ASSET, "1", False),
    ("1101", "الصندوق", "Cash on hand", AccountType.ASSET, "11", True),
    ("1102", "البنك", "Bank", AccountType.ASSET, "11", True),
    ("1103", "العملاء (المدينون)", "Trade receivables", AccountType.ASSET, "11", True),
    ("1104", "مستخلصات مستحقة من العملاء", "Certified progress billings receivable", AccountType.ASSET, "11", True),
    ("1105", "أعمال تحت التنفيذ", "Contract work in progress", AccountType.ASSET, "11", True),
    ("1106", "محتجزات مدينة لدى العملاء", "Retention receivable", AccountType.ASSET, "11", True),
    ("1107", "المخزون", "Inventory", AccountType.ASSET, "11", True),
    ("1108", "ضريبة مدخلات", "Input VAT", AccountType.ASSET, "11", True),
    ("1109", "دفعات مقدمة للموردين", "Advances to suppliers", AccountType.ASSET, "11", True),
    ("12", "الأصول غير المتداولة", "Non-current assets", AccountType.ASSET, "1", False),
    ("1201", "المعدات والآلات", "Plant and equipment", AccountType.ASSET, "12", True),
    ("1202", "مجمع إهلاك المعدات", "Accumulated depreciation - plant", AccountType.ASSET, "12", True),
    ("2", "الالتزامات", "Liabilities", AccountType.LIABILITY, None, False),
    ("21", "الالتزامات المتداولة", "Current liabilities", AccountType.LIABILITY, "2", False),
    ("2101", "الموردون (الدائنون)", "Trade payables", AccountType.LIABILITY, "21", True),
    ("2102", "محتجزات دائنة للمقاولين من الباطن", "Retention payable to subcontractors", AccountType.LIABILITY, "21", True),
    ("2103", "مستخلصات مقبوضة مقدمًا", "Advance billings (contract liability)", AccountType.LIABILITY, "21", True),
    ("2104", "ضريبة مخرجات", "Output VAT", AccountType.LIABILITY, "21", True),
    ("2105", "الزكاة المستحقة", "Zakat payable", AccountType.LIABILITY, "21", True),
    ("3", "حقوق الملكية", "Equity", AccountType.EQUITY, None, False),
    ("3101", "رأس المال", "Share capital", AccountType.EQUITY, "3", True),
    ("3102", "الأرباح المبقاة", "Retained earnings", AccountType.EQUITY, "3", True),
    ("4", "الإيرادات", "Revenue", AccountType.REVENUE, None, False),
    ("4101", "إيراد عقود المقاولات", "Contract revenue", AccountType.REVENUE, "4", True),
    ("5", "المصروفات", "Expenses", AccountType.EXPENSE, None, False),
    ("5101", "تكلفة المواد", "Materials cost", AccountType.EXPENSE, "5", True),
    ("5102", "تكلفة العمالة", "Labour cost", AccountType.EXPENSE, "5", True),
    ("5103", "تكلفة المقاولين من الباطن", "Subcontractor cost", AccountType.EXPENSE, "5", True),
    ("5104", "تكلفة المعدات", "Equipment cost", AccountType.EXPENSE, "5", True),
    ("5105", "مصروفات عمومية وإدارية", "General and administrative expenses", AccountType.EXPENSE, "5", True),
    ("5106", "مصروف إهلاك", "Depreciation expense", AccountType.EXPENSE, "5", True),
]


def seed(company_name: str, admin_username: str, admin_password: str) -> None:
    # Schema is managed by Alembic: run `alembic upgrade head` before seeding.
    with SessionLocal() as session:
        if session.scalar(select(Company).where(Company.name == company_name)):
            print("الشركة موجودة بالفعل، لم يتم أي تغيير.")
            return

        company = Company(name=company_name, currency=os.environ.get("CERP_CURRENCY", "SAR"))
        session.add(company)
        session.flush()

        session.add(
            User(
                company_id=company.id,
                username=admin_username,
                full_name="مدير النظام",
                password_hash=hash_password(admin_password),
                is_admin=True,
            )
        )

        by_code: dict[str, Account] = {}
        for code, name_ar, name_en, acc_type, parent_code, postable in CHART:
            account = Account(
                company_id=company.id,
                code=code,
                name_ar=name_ar,
                name_en=name_en,
                type=acc_type,
                parent=by_code.get(parent_code) if parent_code else None,
                is_postable=postable,
            )
            session.add(account)
            by_code[code] = account

        year = date.today().year
        session.add(
            FiscalPeriod(
                company_id=company.id,
                name=f"السنة المالية {year}",
                start_date=date(year, 1, 1),
                end_date=date(year, 12, 31),
            )
        )
        session.commit()
        print(f"تم إنشاء الشركة '{company_name}' ودليل الحسابات والمستخدم '{admin_username}'.")


if __name__ == "__main__":
    seed(
        company_name=os.environ.get("CERP_COMPANY", "شركة المقاولات التجريبية"),
        admin_username=os.environ.get("CERP_ADMIN_USER", "admin"),
        admin_password=os.environ["CERP_ADMIN_PASSWORD"],
    )
