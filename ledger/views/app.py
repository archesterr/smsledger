import csv
import json
from collections import Counter
from datetime import UTC, datetime, timedelta

from django.contrib import messages
from django.core.exceptions import RequestDataTooBig
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import RestrictedError
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import audit, charts, ingest, jalali, money, parsers, reports, rules, shortcut
from ..forms import (
    AccountForm,
    CategoryForm,
    FilterForm,
    ImportForm,
    ManualTxForm,
    RuleForm,
    ShareForm,
    TxEditForm,
    user_categories,
)
from ..models import IN, OUT, Account, Budget, Category, Message, Rule, SupportSample, Transaction

PAGE = 50


def back_or(request, default: str):
    """Where to go after a POST: the form's "back" field if it's a local URL, else `default`."""
    back = request.POST.get("back", "")
    if back and url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()},
                                                require_https=request.is_secure()):
        return redirect(back)
    return redirect(default)


def month_param(request) -> tuple[int, int]:
    try:
        return jalali.parse_month(request.GET.get("m", ""))
    except ValueError:
        return jalali.today()[:2]


def month_nav(jy: int, jm: int) -> dict:
    py, pm = jalali.add_months(jy, jm, -1)
    ny, nm = jalali.add_months(jy, jm, 1)
    ty, tm = jalali.today()[:2]
    return {"jy": jy, "jm": jm, "prev": reports.month_key(py, pm),
            "next": reports.month_key(ny, nm) if (ny, nm) <= (ty, tm) else None}


# ---- home -------------------------------------------------------------------
def home(request):
    u = request.user
    jy, jm = month_param(request)
    prev = request.session.get("prev_login")
    return render(request, "ledger/home.html", {
        "failed_logins": audit.failures_since(u, datetime.fromisoformat(prev)) if prev else 0,
        "nav": "home", **month_nav(jy, jm),
        "totals": reports.month_totals(u, jy, jm),
        "budgets": reports.budgets(u, jy, jm)[:5],
        "balances": reports.balances(u),
        "health": reports.health(u),
        "recent": Transaction.objects.filter(user=u).select_related("category", "account")[:8],
    })


def more(request):
    return render(request, "ledger/more.html", {"nav": "more"})


# ---- inbox: fast categorization ------------------------------------------------
def inbox(request):
    u = request.user
    txs = list(Transaction.objects.filter(user=u, category__isnull=True).select_related("account")[:30])
    cats = list(user_categories(u))
    by_id = {c.pk: c for c in cats}
    recent = {IN: rules.recent_top(u, IN), OUT: rules.recent_top(u, OUT)}
    kinds = {OUT: (Category.EXPENSE, Category.TRANSFER), IN: (Category.INCOME, Category.TRANSFER)}
    history = rules.History(u) if txs else None
    items = []
    for t in txs:
        sug = [by_id[i] for i in history.suggest(t, recent[t.direction]) if i in by_id]
        rest = [c for c in cats if c.kind in kinds[t.direction] and c not in sug]
        items.append({"tx": t, "suggested": sug, "others": rest})
    total = Transaction.objects.filter(user=u, category__isnull=True).count()
    return render(request, "ledger/inbox.html", {"nav": "inbox", "items": items, "total": total})


@require_POST
def tx_set_category(request, pk):
    t = get_object_or_404(Transaction, pk=pk, user=request.user)
    cat = get_object_or_404(Category, pk=request.POST.get("category"), user=request.user)
    t.category, t.category_by = cat, Transaction.BY_USER
    t.save(update_fields=["category", "category_by", "updated_at"])
    if request.headers.get("Accept", "").startswith("application/json"):
        left = Transaction.objects.filter(user=request.user, category__isnull=True).count()
        return JsonResponse({"ok": True, "left": left})
    return back_or(request, "inbox")


# ---- transactions ----------------------------------------------------------------
def _text(t: Transaction) -> str:
    parts = [t.note, t.title, t.counterparty, t.category.name if t.category_id else "",
             t.message.raw if t.message_id else ""]
    return parsers.normalize("\n".join(parts)).casefold()


def filtered(request) -> tuple[FilterForm, list[Transaction]]:
    """SQL narrows by what's stored in the clear (dates, direction, category, account, gaps);
    amount and text conditions run on the decrypted rows."""
    u = request.user
    form = FilterForm(u, request.GET or None)
    qs = Transaction.objects.filter(user=u).select_related("category", "account", "message")
    checks = []
    if form.is_bound and form.is_valid():
        f = form.cleaned_data
        if f["month"]:
            try:
                start, end = jalali.month_range(*jalali.parse_month(f["month"]))
                qs = qs.filter(occurred_at__gte=start, occurred_at__lt=end)
            except ValueError:
                pass
        if f["date_from"]:
            qs = qs.filter(occurred_at__gte=jalali.day_start(*f["date_from"]))
        if f["date_to"]:
            qs = qs.filter(occurred_at__lt=jalali.day_start(*f["date_to"]) + timedelta(days=1))
        if f["direction"]:
            qs = qs.filter(direction=f["direction"])
        if f["category"] == "none":
            qs = qs.filter(category__isnull=True)
        elif f["category"].isdigit():
            qs = qs.filter(category_id=int(f["category"]))
        if f["account"]:
            qs = qs.filter(account=f["account"])
        if f["amount_min"]:
            checks.append(lambda t, v=f["amount_min"]: t.amount >= v)
        if f["amount_max"]:
            checks.append(lambda t, v=f["amount_max"]: t.amount <= v)
        if f["gaps"]:
            qs = qs.filter(has_gap=True)
        q = parsers.normalize(f["q"] or "").casefold()
        if q:
            checks.append(lambda t: q in _text(t))
    return form, [t for t in qs if all(c(t) for c in checks)]


def tx_list(request):
    form, txs = filtered(request)
    sum_in = sum(t.amount for t in txs if t.direction == IN)
    sum_out = sum(t.amount for t in txs if t.direction == OUT)
    page = Paginator(txs, PAGE).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "ledger/tx_list.html", {
        "nav": "tx", "form": form, "page": page, "sum_in": sum_in, "sum_out": sum_out,
        "query": params.urlencode(), "categories": user_categories(request.user),
        "filtered": any(v for k, v in request.GET.items() if k != "page"),
    })


def csv_safe(value) -> str:
    """Stop spreadsheet formula injection: SMS text is attacker-influenced (anyone can text you)."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def csv_response(txs, filename: str) -> HttpResponse:
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.write("﻿")  # BOM: Excel then reads the file as UTF-8 (Persian text)
    w = csv.writer(resp)
    w.writerow(["date", "time", "account", "direction", "amount_rial", "balance_rial", "category",
                "note", "title", "counterparty", "source", "missed_before_rial", "sms"])
    rows = txs.order_by("occurred_at", "id").iterator(chunk_size=500) if hasattr(txs, "order_by") \
        else sorted(txs, key=lambda t: (t.occurred_at, t.pk))
    for t in rows:
        w.writerow([
            jalali.fmt(*jalali.of(t.occurred_at)), t.occurred_at.astimezone(jalali.TEHRAN).strftime("%H:%M"),
            csv_safe(t.account.name), t.direction, t.amount, t.balance if t.balance is not None else "",
            csv_safe(t.category.name if t.category else ""), csv_safe(t.note), csv_safe(t.title),
            csv_safe(t.counterparty), t.source, t.gap_amount if t.gap_amount is not None else "",
            csv_safe(t.message.raw if t.message_id else ""),
        ])
    return resp


def tx_export(request):
    _, txs = filtered(request)
    return csv_response(txs, "transactions.csv")


def tx_new(request):
    u = request.user
    initial = {"date": money.fa_digits(jalali.fmt(*jalali.today())), "account": Account.objects.filter(
        user=u, kind=Account.CASH, archived=False).first()}
    form = ManualTxForm(u, request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        with transaction.atomic():
            t = Transaction(user=u, account=d["account"], source=Transaction.MANUAL, direction=d["direction"],
                            amount=d["amount"], occurred_at=form.occurred_at(), category=d["category"],
                            category_by=Transaction.BY_USER if d["category"] else "", note=d["note"])
            rules.categorize(t)
            t.save()
            ingest.recompute_gaps(t.account)
        messages.success(request, "ثبت شد.")
        return redirect("tx_list")
    return render(request, "ledger/tx_form.html", {"nav": "tx", "form": form})


def tx_detail(request, pk):
    u = request.user
    t = get_object_or_404(Transaction.objects.select_related("category", "account", "message"), pk=pk, user=u)
    manual = t.source == Transaction.MANUAL
    if manual:
        form = ManualTxForm(u, request.POST or None, initial={
            "direction": t.direction, "amount": t.amount, "account": t.account, "category": t.category,
            "note": t.note, "date": money.fa_digits(jalali.fmt(*jalali.of(t.occurred_at))),
            "time": t.occurred_at.astimezone(jalali.TEHRAN).strftime("%H:%M"),
        })
    else:
        form = TxEditForm(u, request.POST or None, initial={"category": t.category, "note": t.note})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        old_account = t.account
        if d["category"] != t.category:
            t.category_by = Transaction.BY_USER if d["category"] else ""
        t.category, t.note = d["category"], d["note"]
        if manual:
            t.direction, t.amount, t.account = d["direction"], d["amount"], d["account"]
            t.occurred_at = form.occurred_at()
        with transaction.atomic():
            t.save()
            if manual:
                ingest.recompute_gaps(t.account)
                if old_account.pk != t.account.pk:
                    ingest.recompute_gaps(old_account)
        if not manual and d.get("make_rule") and t.category:
            r = Rule.objects.create(user=u, category=t.category, direction=t.direction, account=t.account,
                                    text=t.counterparty or "", amount_min=None if t.counterparty else t.amount,
                                    amount_max=None if t.counterparty else t.amount)
            messages.success(request, "ذخیره شد و قانون جدید ساخته شد. می‌توانید آن را دقیق‌تر کنید.")
            return redirect("rule_edit", pk=r.pk)
        messages.success(request, "ذخیره شد.")
        return back_or(request, "tx_list")
    return render(request, "ledger/tx_detail.html", {"nav": "tx", "t": t, "form": form, "manual": manual})


@require_POST
def tx_delete(request, pk):
    t = get_object_or_404(Transaction, pk=pk, user=request.user)
    acc = t.account
    with transaction.atomic():
        if t.message_id:
            # keep the message (as "ignored") so re-sending the same SMS stays a duplicate
            Message.objects.filter(pk=t.message_id).update(status=Message.IGNORED)
        t.delete()
        ingest.recompute_gaps(acc)
    messages.success(request, "حذف شد.")
    return redirect("tx_list")


# ---- reports & budgets -------------------------------------------------------------
def reports_view(request):
    u = request.user
    jy, jm = month_param(request)
    year = reports.trend(u, 12, (jy, jm))
    table = [{**r, "net": r["income"] - r["expense"], "key": reports.month_key(r["jy"], r["jm"]),
              "title": money.fa_digits(jalali.month_title(r["jy"], r["jm"]))} for r in reversed(year)]
    groups = {}
    for direction in (OUT, IN):
        rows = reports.by_category(u, jy, jm, direction)
        top = max([r["total"] for r in rows] or [0])  # bars are relative to the largest category
        groups[direction] = [{**r, "width": charts.width_class(r["total"], top)} for r in rows]
    return render(request, "ledger/reports.html", {
        "nav": "reports", **month_nav(jy, jm),
        "totals": reports.month_totals(u, jy, jm),
        "expenses": groups[OUT], "incomes": groups[IN],
        "chart": charts.trend_chart(year[-charts.MONTHS:], u.unit, (jy, jm)),
        "table": table,
        "budgets": reports.budgets(u, jy, jm),
    })


def budgets_view(request):
    u = request.user
    cats = list(Category.objects.filter(user=u, kind=Category.EXPENSE, archived=False).select_related("budget"))
    errors = {}
    if request.method == "POST":
        parsed = {}
        for c in cats:
            raw = request.POST.get(f"b{c.pk}", "").strip()
            if not raw:
                parsed[c.pk] = None
                continue
            try:
                parsed[c.pk] = money.parse_amount(raw, u.unit, allow_zero=True) or None
            except ValueError:
                errors[c.pk] = raw
        if not errors:
            with transaction.atomic():
                for c in cats:
                    if parsed[c.pk] is None:
                        Budget.objects.filter(category=c).delete()
                    else:
                        Budget.objects.update_or_create(category=c, defaults={"user": u, "amount": parsed[c.pk]})
            messages.success(request, "بودجه‌ها ذخیره شد.")
            return redirect("budgets")
    jy, jm = jalali.today()[:2]
    status = {r["category"].pk: r for r in reports.budgets(u, jy, jm)}
    rows = []
    for c in cats:
        b = getattr(c, "budget", None)
        rows.append({"c": c, "value": errors.get(c.pk) or money.format_input(b.amount if b else None, u.unit),
                     "error": c.pk in errors, "status": status.get(c.pk)})
    return render(request, "ledger/budgets.html", {"nav": "more", "rows": rows, "jy": jy, "jm": jm})


# ---- categories -----------------------------------------------------------------
def categories(request):
    cats = Category.objects.filter(user=request.user)
    return render(request, "ledger/categories.html", {
        "nav": "more", "groups": [(label, [c for c in cats if c.kind == kind]) for kind, label in Category.KINDS],
    })


def category_edit(request, pk=None):
    u = request.user
    c = get_object_or_404(Category, pk=pk, user=u) if pk else Category(user=u)
    form = CategoryForm(u, request.POST or None, instance=c)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "ذخیره شد.")
        return redirect("categories")
    used = Transaction.objects.filter(user=u, category=c).count() if pk else 0
    return render(request, "ledger/category_form.html", {"nav": "more", "form": form, "c": c, "used": used})


@require_POST
def category_delete(request, pk):
    c = get_object_or_404(Category, pk=pk, user=request.user)
    with transaction.atomic():
        Transaction.objects.filter(user=request.user, category=c).update(category_by="")
        c.delete()  # its transactions become uncategorized and show up in the inbox again
    messages.success(request, "دسته حذف شد.")
    return redirect("categories")


# ---- rules ----------------------------------------------------------------------
def rules_list(request):
    rs = Rule.objects.filter(user=request.user).select_related("category", "account")
    return render(request, "ledger/rules.html", {"nav": "more", "rules": rs})


def rule_edit(request, pk=None):
    u = request.user
    r = get_object_or_404(Rule, pk=pk, user=u) if pk else Rule(user=u)
    initial = {}
    if not pk:  # prefill from ?text=&direction=&category=
        initial = {k: request.GET[k] for k in ("text", "direction") if request.GET.get(k)}
        if request.GET.get("category", "").isdigit():
            initial["category"] = Category.objects.filter(user=u, pk=int(request.GET["category"])).first()
    form = RuleForm(u, request.POST or None, instance=r, initial=initial)
    if request.method == "POST" and form.is_valid():
        form.save()
        n = rules.apply_to_uncategorized(u)
        messages.success(request, f"قانون ذخیره شد. {money.fa_digits(n)} تراکنش بدون دسته، دسته گرفت.")
        return redirect("rules")
    return render(request, "ledger/rule_form.html", {"nav": "more", "form": form, "r": r})


@require_POST
def rule_delete(request, pk):
    get_object_or_404(Rule, pk=pk, user=request.user).delete()
    messages.success(request, "قانون حذف شد.")
    return redirect("rules")


@require_POST
def rules_apply(request):
    n = rules.apply_to_uncategorized(request.user)
    messages.success(request, f"{money.fa_digits(n)} تراکنش دسته گرفت.")
    return redirect("rules")


# ---- accounts -----------------------------------------------------------------------
def accounts(request):
    u = request.user
    bal = {b["account"].pk: b for b in reports.balances(u)}
    accs = [{"a": a, "bal": bal.get(a.pk), "n": a.transactions.count()} for a in Account.objects.filter(user=u)]
    return render(request, "ledger/accounts.html", {"nav": "more", "accounts": accs})


def account_edit(request, pk=None):
    u = request.user
    a = get_object_or_404(Account, pk=pk, user=u) if pk else Account(user=u, kind=Account.CASH)
    form = AccountForm(request.POST or None, instance=a)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "ذخیره شد.")
        return redirect("accounts")
    return render(request, "ledger/account_form.html", {"nav": "more", "form": form, "a": a})


@require_POST
def account_delete(request, pk):
    a = get_object_or_404(Account, pk=pk, user=request.user)
    try:
        a.delete()
        messages.success(request, "حساب حذف شد.")
    except RestrictedError:
        messages.error(request, "این حساب تراکنش دارد؛ به جای حذف، بایگانی‌اش کنید.")
    return redirect("accounts")


# ---- import & unparsed messages --------------------------------------------------------
def import_periods() -> list[dict]:
    """The backup reader's choices, on Shamsi month boundaries. start: ms since epoch (JS time)."""
    jy, jm, _ = jalali.today()

    def since(y, m, with_year=False):
        label = f"از ۱ {jalali.MONTHS[m - 1]}" + (f" {y}" if with_year else "")
        return {"hint": money.fa_digits(label), "start": int(jalali.day_start(y, m, 1).timestamp() * 1000)}

    return [
        {"key": "month", "label": "ماه گذشته", **since(*jalali.add_months(jy, jm, -1))},
        {"key": "3months", "label": "سه ماه اخیر", **since(*jalali.add_months(jy, jm, -3))},
        {"key": "year", "label": "یک سال اخیر", **since(jy - 1, jm, with_year=True)},
        {"key": "all", "label": "همه", "hint": "کل تاریخچه", "start": 0},
    ]


def import_sms(request):
    form = ImportForm(request.POST or None)
    result = None
    if request.method == "POST" and form.is_valid():
        texts = ingest.split_batch(form.cleaned_data["text"])
        res = ingest.ingest(request.user, texts, "paste")
        result = {k: sum(r["status"] == k for r in res) for k in ("created", "duplicate", "unparsed", "ignored")}
        form = ImportForm()
    return render(request, "ledger/import.html", {
        "nav": "more", "form": form, "result": result, "periods": import_periods(),
        "otp_pattern": shortcut.OTP_PATTERN,
    })


BACKUP_BATCH = 500
EARLIEST_SMS = datetime(2000, 1, 1, tzinfo=UTC)


@require_POST
def import_batch(request):
    """The import page's backup reader posts the bank SMS the user picked, a batch at a time:
    {"items": [{"text": "...", "at": <ms since epoch>}]}. The backup itself never leaves the
    browser; "at" gives each SMS its real arrival time (the year, for banks that send none)."""
    try:
        items = json.loads(request.body or b"{}").get("items")
    except (ValueError, AttributeError, RequestDataTooBig):
        items = None
    if not isinstance(items, list) or not 0 < len(items) <= BACKUP_BATCH or \
            not all(isinstance(i, dict) for i in items):
        return JsonResponse({"error": "bad request"}, status=400)
    latest = timezone.now() + timedelta(days=1)
    texts, times = [], []
    for item in items:
        text, at = item.get("text"), item.get("at")
        texts.append(text if isinstance(text, str) else "")
        when = None
        if isinstance(at, int | float) and not isinstance(at, bool):
            try:
                when = datetime.fromtimestamp(at / 1000, tz=UTC)
            except (OverflowError, OSError, ValueError):
                pass
        times.append(when if when and EARLIEST_SMS <= when <= latest else None)
    results = ingest.ingest(request.user, texts, "backup", times=times)
    return JsonResponse({"count": dict(Counter(r["status"] for r in results))})


def messages_list(request):
    qs = Message.objects.filter(user=request.user, status=Message.UNPARSED).order_by("-received_at")
    return render(request, "ledger/messages.html", {"nav": "more", "items": qs[:100], "total": qs.count()})


@require_POST
def message_delete(request, pk):
    get_object_or_404(Message, pk=pk, user=request.user, status=Message.UNPARSED).delete()
    messages.success(request, "پیامک حذف شد.")
    return redirect("messages")


def message_share(request, pk):
    m = get_object_or_404(Message, pk=pk, user=request.user, status=Message.UNPARSED)
    form = ShareForm(request.POST or None, initial={"text": m.raw})
    if request.method == "POST" and form.is_valid():
        SupportSample.objects.create(user=request.user, text=form.cleaned_data["text"])
        messages.success(request, "برای مدیر ارسال شد. بعد از اضافه شدن قالب این بانک، این پیامک خودکار ثبت می‌شود.")
        return redirect("messages")
    return render(request, "ledger/message_share.html", {"nav": "more", "form": form, "m": m})
