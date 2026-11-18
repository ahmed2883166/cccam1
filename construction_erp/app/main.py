from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .accounting import LineInput, PostingError, post_entry
from .chart import (
    TYPE_LABELS_AR,
    ChartError,
    balances_by_account,
    build_tree,
    create_account,
    filter_flat,
    set_account_active,
)
from .config import settings
from .db import get_session
from .invoicing import (
    KIND_LABELS_AR,
    STATUS_LABELS_AR,
    InvoiceError,
    LineDraft,
    create_credit_note,
    create_draft,
    credited_quantity,
    parse_decimal,
    post_invoice,
)
from .models import (
    Account,
    AccountType,
    Invoice,
    InvoiceKind,
    InvoiceStatus,
    JournalEntry,
    JournalLine,
    Partner,
    PartnerKind,
    Payment,
    PaymentKind,
    PaymentMethod,
    User,
)
from .payments import (
    METHOD_LABELS_AR,
    PAYMENT_LABELS_AR,
    PaymentError,
    advance_remaining,
    apply_advance,
    open_advances,
    open_invoices,
    record_payment,
)
from .reports import AGING_BUCKETS, aging_report, aging_totals, vat_summary
from .security import verify_password

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Construction ERP", docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie=settings.session_cookie)


def current_user(request: Request, session: Session = Depends(get_session)) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = session.get(User, user_id)
    return user if user and user.is_active else None


def require_user(user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise _Redirect("/login")
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="هذه الصفحة للمدير فقط.")
    return user


class _Redirect(Exception):
    def __init__(self, location: str):
        self.location = location


@app.exception_handler(_Redirect)
async def _redirect_handler(request: Request, exc: _Redirect):
    return RedirectResponse(exc.location, status_code=303)


def _render(request: Request, name: str, **context) -> HTMLResponse:
    context.setdefault("currency", settings.default_currency)
    return templates.TemplateResponse(request, name, context)


def _to_decimal(raw: str) -> Decimal:
    try:
        return Decimal(raw.strip() or "0")
    except (InvalidOperation, AttributeError):
        raise PostingError("مبلغ غير صحيح.")


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return _render(request, "login.html", error=None)


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    user = session.scalar(select(User).where(User.username == username))
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        return _render(request, "login.html", error="اسم المستخدم أو كلمة المرور غير صحيحة.")
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    totals = session.execute(
        select(func.coalesce(func.sum(JournalLine.debit), 0), func.coalesce(func.sum(JournalLine.credit), 0))
        .join(Account)
        .where(Account.company_id == user.company_id)
    ).one()
    account_count = session.scalar(select(func.count(Account.id)).where(Account.company_id == user.company_id))
    return _render(
        request,
        "dashboard.html",
        user=user,
        total_debit=totals[0],
        total_credit=totals[1],
        account_count=account_count,
    )


@app.get("/accounts", response_class=HTMLResponse)
def accounts(
    request: Request,
    q: str = Query(""),
    account_type: str = Query("", alias="type"),
    created: str = Query(""),
    error: str = Query(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    all_accounts = session.scalars(select(Account).where(Account.company_id == user.company_id)).all()
    balances = balances_by_account(session, user.company_id)
    is_filtered = bool(q.strip() or account_type)
    rows = filter_flat(all_accounts, balances, q, account_type) if is_filtered else build_tree(all_accounts, balances)
    context = dict(
        user=user,
        rows=rows,
        q=q,
        account_type=account_type,
        is_filtered=is_filtered,
        account_types=list(AccountType),
        type_labels=TYPE_LABELS_AR,
        created=created,
        error=error,
    )
    if request.headers.get("HX-Request"):
        return _render(request, "_accounts_table.html", **context)
    return _render(request, "accounts.html", **context)


def _account_form_context(session: Session, user: User, error: str | None = None, form: dict | None = None) -> dict:
    parents = session.scalars(
        select(Account)
        .where(Account.company_id == user.company_id, Account.is_postable.is_(False), Account.is_active.is_(True))
        .order_by(Account.code)
    ).all()
    return dict(
        user=user,
        parents=parents,
        account_types=list(AccountType),
        type_labels=TYPE_LABELS_AR,
        error=error,
        form=form or {"is_postable": True},
    )


@app.get("/accounts/new", response_class=HTMLResponse)
def account_new_form(request: Request, user: User = Depends(require_admin), session: Session = Depends(get_session)):
    return _render(request, "account_form.html", **_account_form_context(session, user))


@app.post("/accounts/new", response_class=HTMLResponse)
async def account_new_post(
    request: Request,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    form = await request.form()
    values = {k: str(form.get(k, "")).strip() for k in ("code", "name_ar", "name_en", "account_type", "parent_id")}
    values["is_postable"] = form.get("is_postable") == "on"
    try:
        create_account(
            session,
            company_id=user.company_id,
            code=values["code"],
            name_ar=values["name_ar"],
            name_en=values["name_en"],
            account_type=AccountType(values["account_type"]),
            parent_id=int(values["parent_id"]) if values["parent_id"] else None,
            is_postable=values["is_postable"],
        )
        session.commit()
    except (ChartError, ValueError) as exc:
        session.rollback()
        message = str(exc) if isinstance(exc, ChartError) else "نوع الحساب غير صحيح."
        return _render(
            request,
            "account_form.html",
            **_account_form_context(session, user, error=message, form=values),
        )
    return RedirectResponse("/accounts?created=1", status_code=303)


@app.post("/accounts/{account_id}/status")
def account_toggle_status(
    account_id: int,
    user: User = Depends(require_admin),
    session: Session = Depends(get_session),
):
    account = session.scalar(select(Account).where(Account.id == account_id, Account.company_id == user.company_id))
    if account is None:
        raise HTTPException(status_code=404, detail="الحساب غير موجود.")
    try:
        set_account_active(session, company_id=user.company_id, account_id=account_id, active=not account.is_active)
        session.commit()
    except ChartError as exc:
        session.rollback()
        return RedirectResponse(f"/accounts?error={quote(str(exc))}", status_code=303)
    return RedirectResponse("/accounts", status_code=303)


@app.get("/journal", response_class=HTMLResponse)
def journal_form(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    accounts_list = session.scalars(
        select(Account).where(
            Account.company_id == user.company_id, Account.is_postable.is_(True), Account.is_active.is_(True)
        ).order_by(Account.code)
    ).all()
    return _render(
        request, "journal.html", user=user, accounts=accounts_list, error=None, today=date.today().isoformat()
    )


@app.post("/journal", response_class=HTMLResponse)
async def journal_post(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    form = await request.form()
    try:
        entry_date = date.fromisoformat(str(form.get("entry_date", "")))
        description = str(form.get("description", ""))
        account_ids = form.getlist("account_id")
        debits = form.getlist("debit")
        credits = form.getlist("credit")
        lines = [
            LineInput(
                account_id=int(acc),
                debit=_to_decimal(dr),
                credit=_to_decimal(cr),
            )
            for acc, dr, cr in zip(account_ids, debits, credits)
            if acc
        ]
        entry = post_entry(
            session,
            company_id=user.company_id,
            user_id=user.id,
            entry_date=entry_date,
            description=description,
            lines=lines,
        )
        session.commit()
        return RedirectResponse(f"/journal?saved={entry.number}", status_code=303)
    except (PostingError, ValueError) as exc:
        session.rollback()
        accounts_list = session.scalars(
            select(Account).where(Account.company_id == user.company_id).order_by(Account.code)
        ).all()
        return _render(
            request,
            "journal.html",
            user=user,
            accounts=accounts_list,
            error=str(exc),
            today=date.today().isoformat(),
        )


@app.get("/trial-balance", response_class=HTMLResponse)
def trial_balance(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    rows = session.execute(
        select(
            Account.code,
            Account.name_ar,
            Account.type,
            func.coalesce(func.sum(JournalLine.debit), 0),
            func.coalesce(func.sum(JournalLine.credit), 0),
        )
        .outerjoin(JournalLine, JournalLine.account_id == Account.id)
        .where(Account.company_id == user.company_id)
        .group_by(Account.id, Account.code, Account.name_ar, Account.type)
        .order_by(Account.code)
    ).all()
    total_debit = sum((r[3] for r in rows), Decimal("0"))
    total_credit = sum((r[4] for r in rows), Decimal("0"))
    return _render(
        request,
        "trial_balance.html",
        user=user,
        rows=rows,
        total_debit=total_debit,
        total_credit=total_credit,
        balanced=total_debit == total_credit,
    )


# ---------- Partners (customers and suppliers) ----------

PARTNER_KIND_LABELS = {PartnerKind.CUSTOMER: "عميل", PartnerKind.SUPPLIER: "مورد"}


def _parse_kind(raw: str, enum_cls, default=None):
    try:
        return enum_cls(raw)
    except ValueError:
        if default is not None:
            return default
        raise HTTPException(status_code=400, detail="نوع غير صحيح.")


@app.get("/partners", response_class=HTMLResponse)
def partners_list(
    request: Request,
    kind: str = Query(""),
    created: str = Query(""),
    error: str = Query(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    query = select(Partner).where(Partner.company_id == user.company_id).order_by(Partner.name)
    if kind:
        query = query.where(Partner.kind == _parse_kind(kind, PartnerKind))
    return _render(
        request,
        "partners.html",
        user=user,
        partners=session.scalars(query).all(),
        kind=kind,
        kinds=list(PartnerKind),
        kind_labels=PARTNER_KIND_LABELS,
        created=created,
        error=error,
    )


@app.post("/partners/new")
async def partner_create(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    form = await request.form()
    try:
        kind = _parse_kind(str(form.get("kind", "")), PartnerKind, default=None)
        if kind is None:
            raise InvoiceError("نوع الطرف غير صحيح.")
        name = str(form.get("name", "")).strip()
        if not name:
            raise InvoiceError("اسم الطرف مطلوب.")
        session.add(
            Partner(
                company_id=user.company_id,
                kind=kind,
                name=name,
                tax_number=str(form.get("tax_number", "")).strip() or None,
                phone=str(form.get("phone", "")).strip() or None,
                is_active=True,
            )
        )
        session.commit()
    except (InvoiceError, HTTPException) as exc:
        session.rollback()
        message = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return RedirectResponse(f"/partners?error={quote(message)}", status_code=303)
    return RedirectResponse("/partners?created=1", status_code=303)


# ---------- Sales and purchase invoices ----------

def _line_accounts(session: Session, user: User, kind: InvoiceKind) -> list[Account]:
    types = [AccountType.REVENUE] if kind == InvoiceKind.SALES else [AccountType.EXPENSE, AccountType.ASSET]
    return session.scalars(
        select(Account)
        .where(
            Account.company_id == user.company_id,
            Account.is_postable.is_(True),
            Account.is_active.is_(True),
            Account.type.in_(types),
        )
        .order_by(Account.code)
    ).all()


def _active_partners(session: Session, user: User, kind: InvoiceKind) -> list[Partner]:
    partner_kind = PartnerKind.CUSTOMER if kind == InvoiceKind.SALES else PartnerKind.SUPPLIER
    return session.scalars(
        select(Partner)
        .where(Partner.company_id == user.company_id, Partner.kind == partner_kind, Partner.is_active.is_(True))
        .order_by(Partner.name)
    ).all()


@app.get("/invoices", response_class=HTMLResponse)
def invoices_list(
    request: Request,
    kind: str = Query("sales"),
    created: str = Query(""),
    error: str = Query(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    invoice_kind = _parse_kind(kind, InvoiceKind)
    invoices = session.scalars(
        select(Invoice)
        .where(Invoice.company_id == user.company_id, Invoice.kind == invoice_kind)
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    ).all()
    return _render(
        request,
        "invoices.html",
        user=user,
        kind=invoice_kind,
        kinds=list(InvoiceKind),
        kind_labels=KIND_LABELS_AR,
        status_labels=STATUS_LABELS_AR,
        invoices=invoices,
        created=created,
        error=error,
    )


def _invoice_form_context(session: Session, user: User, kind: InvoiceKind, **extra) -> dict:
    context = dict(
        user=user,
        kind=kind,
        kind_labels=KIND_LABELS_AR,
        partners=_active_partners(session, user, kind),
        accounts=_line_accounts(session, user, kind),
        invoice_date=date.today().isoformat(),
        rows=None,
        error=None,
        partner_id="",
    )
    context.update(extra)
    return context


@app.get("/invoices/new", response_class=HTMLResponse)
def invoice_new_form(
    request: Request,
    kind: str = Query("sales"),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    invoice_kind = _parse_kind(kind, InvoiceKind)
    return _render(request, "invoice_form.html", **_invoice_form_context(session, user, invoice_kind))


@app.post("/invoices/new", response_class=HTMLResponse)
async def invoice_create(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    form = await request.form()
    invoice_kind = _parse_kind(str(form.get("kind", "sales")), InvoiceKind)
    account_ids = form.getlist("account_id")
    descriptions = form.getlist("description")
    quantities = form.getlist("quantity")
    prices = form.getlist("unit_price")
    tax_rates = form.getlist("tax_rate")
    rows = [
        {
            "account_id": acc,
            "description": desc,
            "quantity": qty,
            "unit_price": price,
            "tax_rate": tax,
        }
        for acc, desc, qty, price, tax in zip(account_ids, descriptions, quantities, prices, tax_rates)
        if acc or desc or qty or price
    ]
    try:
        partner_id = int(str(form.get("partner_id", "")) or 0)
        invoice_date = date.fromisoformat(str(form.get("invoice_date", "")))
        lines = [
            LineDraft(
                account_id=int(row["account_id"] or 0),
                description=row["description"],
                quantity=parse_decimal(row["quantity"], "الكمية"),
                unit_price=parse_decimal(row["unit_price"], "السعر"),
                tax_rate=parse_decimal(row["tax_rate"] or "0", "الضريبة"),
            )
            for row in rows
        ]
        invoice = create_draft(
            session,
            company_id=user.company_id,
            user_id=user.id,
            kind=invoice_kind,
            partner_id=partner_id,
            invoice_date=invoice_date,
            lines=lines,
        )
        session.commit()
        return RedirectResponse(f"/invoices/{invoice.id}", status_code=303)
    except (InvoiceError, ValueError) as exc:
        session.rollback()
        message = str(exc) if isinstance(exc, InvoiceError) else "بيانات الفاتورة غير صحيحة."
        return _render(
            request,
            "invoice_form.html",
            **_invoice_form_context(
                session,
                user,
                invoice_kind,
                error=message,
                rows=rows,
                partner_id=str(form.get("partner_id", "")),
                invoice_date=str(form.get("invoice_date", "")),
            ),
        )


@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(
    invoice_id: int,
    request: Request,
    posted: str = Query(""),
    error: str = Query(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == user.company_id))
    if invoice is None:
        raise HTTPException(status_code=404, detail="الفاتورة غير موجودة.")
    entry = session.get(JournalEntry, invoice.journal_entry_id) if invoice.journal_entry_id else None
    original = session.get(Invoice, invoice.credit_for_id) if invoice.credit_for_id else None
    credit_notes = session.scalars(
        select(Invoice)
        .where(Invoice.credit_for_id == invoice.id, Invoice.status == InvoiceStatus.POSTED)
        .order_by(Invoice.number)
    ).all()
    line_remaining = {l.id: l.quantity - credited_quantity(session, l.id) for l in invoice.lines}
    return _render(
        request,
        "invoice_detail.html",
        user=user,
        invoice=invoice,
        kind_labels=KIND_LABELS_AR,
        status_labels=STATUS_LABELS_AR,
        entry=entry,
        posted=posted,
        error=error,
        is_draft=invoice.status == InvoiceStatus.DRAFT,
        original=original,
        credit_notes=credit_notes,
        line_remaining=line_remaining,
        can_credit=invoice.status == InvoiceStatus.POSTED and not invoice.is_credit,
        today=date.today().isoformat(),
    )


@app.post("/invoices/{invoice_id}/post")
def invoice_post(
    invoice_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    try:
        post_invoice(session, company_id=user.company_id, user_id=user.id, invoice_id=invoice_id)
        session.commit()
    except (InvoiceError, PostingError) as exc:
        session.rollback()
        return RedirectResponse(f"/invoices/{invoice_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/invoices/{invoice_id}?posted=1", status_code=303)


@app.post("/invoices/{invoice_id}/delete")
def invoice_delete_draft(
    invoice_id: int,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    invoice = session.scalar(select(Invoice).where(Invoice.id == invoice_id, Invoice.company_id == user.company_id))
    if invoice is None:
        raise HTTPException(status_code=404, detail="الفاتورة غير موجودة.")
    if invoice.status != InvoiceStatus.DRAFT:
        return RedirectResponse(f"/invoices/{invoice_id}?error={quote('لا يمكن حذف فاتورة مرحّلة.')}", status_code=303)
    kind = invoice.kind.value
    session.delete(invoice)
    session.commit()
    return RedirectResponse(f"/invoices?kind={kind}", status_code=303)


# ---------- Collections and disbursements ----------

def _payment_form_context(session: Session, user: User, kind: PaymentKind, partner_id: int | None, **extra) -> dict:
    partner_kind = PartnerKind.CUSTOMER if kind == PaymentKind.RECEIPT else PartnerKind.SUPPLIER
    partners = session.scalars(
        select(Partner)
        .where(Partner.company_id == user.company_id, Partner.kind == partner_kind, Partner.is_active.is_(True))
        .order_by(Partner.name)
    ).all()
    context = dict(
        user=user,
        kind=kind,
        partners=partners,
        partner_id=str(partner_id or ""),
        items=open_invoices(session, user.company_id, kind, partner_id) if partner_id else [],
        method_labels=METHOD_LABELS_AR,
        methods=list(PaymentMethod),
        payment_date=date.today().isoformat(),
        label=PAYMENT_LABELS_AR[kind],
        error=None,
        form={},
    )
    context.update(extra)
    return context


@app.get("/payments", response_class=HTMLResponse)
def payments_list(
    request: Request,
    kind: str = Query("receipt"),
    created: str = Query(""),
    error: str = Query(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    payment_kind = _parse_kind(kind, PaymentKind)
    rows = session.scalars(
        select(Payment)
        .where(Payment.company_id == user.company_id, Payment.kind == payment_kind)
        .order_by(Payment.payment_date.desc(), Payment.id.desc())
    ).all()
    return _render(
        request,
        "payments.html",
        user=user,
        kind=payment_kind,
        kinds=list(PaymentKind),
        labels=PAYMENT_LABELS_AR,
        method_labels=METHOD_LABELS_AR,
        payments=rows,
        created=created,
        error=error,
    )


@app.get("/payments/new", response_class=HTMLResponse)
def payment_new_form(
    request: Request,
    kind: str = Query("receipt"),
    partner_id: str = Query(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    payment_kind = _parse_kind(kind, PaymentKind)
    pid = int(partner_id) if partner_id.isdigit() else None
    return _render(request, "payment_form.html", **_payment_form_context(session, user, payment_kind, pid))


@app.get("/payments/open-invoices", response_class=HTMLResponse)
def payment_open_invoices(
    request: Request,
    kind: str = Query("receipt"),
    partner_id: str = Query(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    payment_kind = _parse_kind(kind, PaymentKind)
    pid = int(partner_id) if partner_id.isdigit() else None
    context = _payment_form_context(session, user, payment_kind, pid)
    return _render(request, "_open_invoices.html", **{k: context[k] for k in ("items", "kind", "user")})


@app.post("/payments/new", response_class=HTMLResponse)
async def payment_create(
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    form = await request.form()
    payment_kind = _parse_kind(str(form.get("kind", "receipt")), PaymentKind)
    partner_raw = str(form.get("partner_id", ""))
    try:
        partner_id = int(partner_raw)
        payment_date = date.fromisoformat(str(form.get("payment_date", "")))
        method = _parse_kind(str(form.get("method", "")), PaymentMethod)
        amount = parse_decimal(str(form.get("amount", "")), "المبلغ").quantize(Decimal("0.01"))
        invoice_ids = form.getlist("invoice_id")
        allocs = form.getlist("alloc")
        allocations = []
        for inv_id, raw in zip(invoice_ids, allocs):
            raw = (raw or "").strip()
            if not raw:
                continue
            value = parse_decimal(raw, "مبلغ التسوية").quantize(Decimal("0.01"))
            allocations.append((int(inv_id), value))
        payment = record_payment(
            session,
            company_id=user.company_id,
            user_id=user.id,
            kind=payment_kind,
            partner_id=partner_id,
            payment_date=payment_date,
            method=method,
            amount=amount,
            allocations=allocations,
        )
        session.commit()
        return RedirectResponse(f"/payments/{payment.id}", status_code=303)
    except (PaymentError, InvoiceError, ValueError) as exc:
        session.rollback()
        message = str(exc) if isinstance(exc, (PaymentError, InvoiceError)) else "بيانات السند غير صحيحة."
        pid = int(partner_raw) if partner_raw.isdigit() else None
        return _render(
            request,
            "payment_form.html",
            **_payment_form_context(
                session,
                user,
                payment_kind,
                pid,
                error=message,
                payment_date=str(form.get("payment_date", "")),
                amount=str(form.get("amount", "")),
                method_value=str(form.get("method", "")),
            ),
        )


@app.get("/payments/{payment_id}", response_class=HTMLResponse)
def payment_detail(
    payment_id: int,
    request: Request,
    applied: str = Query(""),
    error: str = Query(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    payment = session.scalar(select(Payment).where(Payment.id == payment_id, Payment.company_id == user.company_id))
    if payment is None:
        raise HTTPException(status_code=404, detail="السند غير موجود.")
    entry = session.get(JournalEntry, payment.journal_entry_id) if payment.journal_entry_id else None
    remaining = advance_remaining(session, payment)
    open_items = (
        open_invoices(session, user.company_id, payment.kind, partner_id=payment.partner_id) if remaining > 0 else []
    )
    return _render(
        request,
        "payment_detail.html",
        user=user,
        payment=payment,
        labels=PAYMENT_LABELS_AR,
        method_labels=METHOD_LABELS_AR,
        entry=entry,
        remaining_advance=remaining,
        open_items=open_items,
        applied=applied,
        error=error,
        today=date.today().isoformat(),
    )


@app.get("/balances", response_class=HTMLResponse)
def balances(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    sections = []
    for kind, title in ((PaymentKind.RECEIPT, "أرصدة العملاء (المستحق لنا)"), (PaymentKind.PAYMENT, "أرصدة الموردين (المستحق علينا)")):
        items = open_invoices(session, user.company_id, kind)
        by_partner: dict[str, dict] = {}
        for item in items:
            group = by_partner.setdefault(
                item.invoice.partner.name, {"name": item.invoice.partner.name, "invoices": [], "total": Decimal("0")}
            )
            group["invoices"].append(item)
            group["total"] += item.outstanding
        sections.append(
            dict(title=title, groups=sorted(by_partner.values(), key=lambda g: g["name"]),
                 grand_total=sum((g["total"] for g in by_partner.values()), Decimal("0")))
        )
    advances = []
    for kind in (PaymentKind.RECEIPT, PaymentKind.PAYMENT):
        advances += [(kind, payment, remaining) for payment, remaining in open_advances(session, user.company_id, kind)]
    return _render(
        request,
        "balances.html",
        user=user,
        sections=sections,
        advances=advances,
        kind_labels=KIND_LABELS_AR,
        payment_labels=PAYMENT_LABELS_AR,
    )


def _parse_date(raw: str, field: str) -> date:
    try:
        return date.fromisoformat((raw or "").strip())
    except ValueError:
        raise InvoiceError(f"{field} غير صحيح.")


@app.post("/invoices/{invoice_id}/credit")
async def invoice_credit_note(
    invoice_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    form = await request.form()
    try:
        quantities: dict[int, Decimal] = {}
        for key, raw in form.items():
            if not key.startswith("qty_"):
                continue
            value = parse_decimal(str(raw), "الكمية") if str(raw).strip() else Decimal("0")
            if value < 0:
                raise InvoiceError("الكمية المرتجعة مش ممكن تكون سالبة.")
            if value > 0:
                quantities[int(key[4:])] = value
        credit_date = _parse_date(str(form.get("credit_date", "")), "تاريخ الإشعار")
        credit = create_credit_note(
            session,
            company_id=user.company_id,
            user_id=user.id,
            original_id=invoice_id,
            credit_quantities=quantities,
            credit_date=credit_date,
        )
        session.commit()
    except (InvoiceError, PostingError, ValueError) as exc:
        session.rollback()
        return RedirectResponse(f"/invoices/{invoice_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/invoices/{credit.id}?posted=1", status_code=303)


@app.post("/payments/{payment_id}/apply")
async def payment_apply_advance(
    payment_id: int,
    request: Request,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    form = await request.form()
    try:
        invoice_id = int(str(form.get("invoice_id", "")).strip() or 0)
        amount = parse_decimal(str(form.get("amount", "")), "المبلغ")
        apply_date = _parse_date(str(form.get("apply_date", "")), "التاريخ")
        apply_advance(
            session,
            company_id=user.company_id,
            user_id=user.id,
            payment_id=payment_id,
            invoice_id=invoice_id,
            amount=amount,
            apply_date=apply_date,
        )
        session.commit()
    except (InvoiceError, PaymentError, PostingError, ValueError) as exc:
        session.rollback()
        return RedirectResponse(f"/payments/{payment_id}?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"/payments/{payment_id}?applied=1", status_code=303)


AGING_KIND_LABELS = {
    PaymentKind.RECEIPT: "أعمار ذمم العملاء",
    PaymentKind.PAYMENT: "أعمار ذمم الموردين",
}


@app.get("/reports/aging", response_class=HTMLResponse)
def aging_page(
    request: Request,
    kind: str = Query("receipt"),
    as_of: str = Query(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    kind_enum = _parse_kind(kind, PaymentKind, default=PaymentKind.RECEIPT)
    try:
        when = date.fromisoformat(as_of) if as_of else date.today()
    except ValueError:
        when = date.today()
    rows = aging_report(session, user.company_id, kind_enum, when)
    totals = aging_totals(rows)
    return _render(
        request,
        "reports_aging.html",
        user=user,
        kind=kind_enum,
        kind_labels=AGING_KIND_LABELS,
        rows=rows,
        totals=totals,
        grand=sum(totals, Decimal("0")),
        buckets=AGING_BUCKETS,
        as_of=when.isoformat(),
    )


@app.get("/reports/vat", response_class=HTMLResponse)
def vat_page(
    request: Request,
    start: str = Query(""),
    end: str = Query(""),
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    today = date.today()
    error = ""
    try:
        start_d = date.fromisoformat(start) if start else date(today.year, today.month, 1)
        end_d = date.fromisoformat(end) if end else today
    except ValueError:
        start_d, end_d = date(today.year, today.month, 1), today
        error = "صيغة التاريخ غير صحيحة."
    if start_d > end_d:
        error = "تاريخ البداية بعد تاريخ النهاية."
    summary = vat_summary(session, user.company_id, start_d, end_d)
    return _render(
        request,
        "reports_vat.html",
        user=user,
        s=summary,
        start=start_d.isoformat(),
        end=end_d.isoformat(),
        error=error,
    )
