from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .accounting import LineInput, PostingError, post_entry
from .config import settings
from .db import get_session
from .models import Account, JournalLine, User
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
def accounts(request: Request, user: User = Depends(require_user), session: Session = Depends(get_session)):
    rows = session.scalars(
        select(Account).where(Account.company_id == user.company_id).order_by(Account.code)
    ).all()
    return _render(request, "accounts.html", user=user, accounts=rows)


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
