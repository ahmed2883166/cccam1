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
from .models import Account, AccountType, JournalLine, User
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
