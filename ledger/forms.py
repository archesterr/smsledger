from __future__ import annotations

import re
from datetime import datetime

from django import forms
from django.contrib.auth.forms import UserCreationForm

from . import jalali, money
from .models import DIRECTIONS, Account, Category, Rule, User

TIME_RE = re.compile(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*$")


class AmountField(forms.CharField):
    """Typed in the user's unit (Persian or Latin digits, separators allowed), cleaned to rial."""

    def __init__(self, *args, allow_zero=False, **kwargs):
        self.unit, self.allow_zero = "toman", allow_zero
        kwargs.setdefault("widget", forms.TextInput(attrs={"inputmode": "decimal", "autocomplete": "off",
                                                           "dir": "ltr", "class": "num-input"}))
        super().__init__(*args, **kwargs)

    def to_python(self, value):
        value = super().to_python(value)
        if value in self.empty_values:
            return None
        try:
            return money.parse_amount(value, self.unit, self.allow_zero)
        except ValueError:
            raise forms.ValidationError("مبلغ معتبر نیست.") from None


class JalaliDateField(forms.CharField):
    widget = forms.TextInput(attrs={"inputmode": "numeric", "dir": "ltr", "placeholder": "1405/07/01",
                                    "class": "num-input", "autocomplete": "off"})

    def to_python(self, value):
        value = super().to_python(value)
        if value in self.empty_values:
            return None
        try:
            return jalali.parse(value)
        except ValueError:
            raise forms.ValidationError("تاریخ را به شکل ۱۴۰۵/۰۷/۰۱ وارد کنید.") from None


class UnitForm:
    """Mixin: tell AmountFields the user's unit, and show rial initial values in that unit."""

    def setup_units(self, unit: str):
        for name, field in self.fields.items():
            if isinstance(field, AmountField):
                field.unit = unit
                field.label = f"{field.label} ({money.UNIT_LABELS[unit]})"
                v = self.initial.get(name)
                if isinstance(v, int):
                    self.initial[name] = money.format_input(v, unit)


def user_categories(user, kinds=None):
    qs = Category.objects.filter(user=user, archived=False)
    return qs.filter(kind__in=kinds) if kinds else qs


def user_accounts(user):
    return Account.objects.filter(user=user, archived=False)


# ---- auth -------------------------------------------------------------------
class LoginForm(forms.Form):
    username = forms.CharField(label="نام کاربری", max_length=150,
                               widget=forms.TextInput(attrs={"autocomplete": "username", "autofocus": True,
                                                             "autocapitalize": "none", "dir": "ltr"}))
    password = forms.CharField(label="رمز عبور", strip=False,
                               widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "dir": "ltr"}))


class CodeForm(forms.Form):
    code = forms.CharField(label="کد", max_length=20,
                           widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric",
                                                         "autofocus": True, "dir": "ltr", "class": "num-input"}))


class SignupForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update({"autocapitalize": "none", "dir": "ltr"})
        self.fields["username"].help_text = "حروف انگلیسی یا فارسی، عدد و . _ - مجاز است."


class UnitPrefForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["unit"]
        labels = {"unit": "واحد نمایش مبلغ"}
        widgets = {"unit": forms.RadioSelect}


class PasswordConfirmForm(forms.Form):
    password = forms.CharField(label="رمز عبور فعلی", strip=False,
                               widget=forms.PasswordInput(attrs={"autocomplete": "current-password", "dir": "ltr"}))

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_password(self):
        p = self.cleaned_data["password"]
        if not self.user.check_password(p):
            raise forms.ValidationError("رمز عبور درست نیست.")
        return p


class DeviceForm(forms.Form):
    name = forms.CharField(label="نام دستگاه", max_length=60, initial="iPhone")


# ---- money ------------------------------------------------------------------
class ManualTxForm(UnitForm, forms.Form):
    direction = forms.ChoiceField(label="نوع", choices=DIRECTIONS, initial="OUT", widget=forms.RadioSelect)
    amount = AmountField(label="مبلغ")
    account = forms.ModelChoiceField(label="حساب", queryset=Account.objects.none(), empty_label=None)
    date = JalaliDateField(label="تاریخ")
    time = forms.CharField(label="ساعت", initial="12:00",
                           widget=forms.TextInput(attrs={"inputmode": "numeric", "dir": "ltr", "class": "num-input"}))
    category = forms.ModelChoiceField(label="دسته", queryset=Category.objects.none(), required=False,
                                      empty_label="— بدون دسته —")
    note = forms.CharField(label="توضیح", max_length=300, required=False)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = user_accounts(user)
        self.fields["category"].queryset = user_categories(user)
        self.setup_units(user.unit)

    def clean_time(self):
        m = TIME_RE.match((self.cleaned_data["time"] or "").translate(money.TO_EN))
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise forms.ValidationError("ساعت را به شکل ۱۴:۳۰ وارد کنید.")
        return int(m.group(1)), int(m.group(2))

    def occurred_at(self) -> datetime:
        jy, jm, jd = self.cleaned_data["date"]
        hh, mm = self.cleaned_data["time"]
        return jalali.day_start(jy, jm, jd).replace(hour=hh, minute=mm)


class TxEditForm(forms.Form):
    category = forms.ModelChoiceField(label="دسته", queryset=Category.objects.none(), required=False,
                                      empty_label="— بدون دسته —")
    note = forms.CharField(label="توضیح", max_length=300, required=False)
    make_rule = forms.BooleanField(label="تراکنش‌های مشابه بعدی هم خودکار همین دسته را بگیرند", required=False)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = user_categories(user)


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ["icon", "name", "kind", "archived"]
        labels = {"icon": "ایموجی", "name": "نام", "kind": "نوع", "archived": "بایگانی (در لیست‌ها نشان داده نشود)"}

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.fields["icon"].required = False
        self.fields["icon"].widget.attrs["class"] = "emoji-input"

    def clean(self):
        data = super().clean()
        dup = Category.objects.filter(user=self.user, kind=data.get("kind"), name=data.get("name"))
        if self.instance.pk:
            dup = dup.exclude(pk=self.instance.pk)
        if dup.exists():
            raise forms.ValidationError("دسته‌ای با همین نام وجود دارد.")
        return data


class RuleForm(UnitForm, forms.ModelForm):
    amount_min = AmountField(label="حداقل مبلغ", required=False)
    amount_max = AmountField(label="حداکثر مبلغ", required=False)

    class Meta:
        model = Rule
        fields = ["category", "direction", "account", "text", "amount_min", "amount_max", "priority", "enabled"]
        labels = {
            "category": "این دسته را بده", "direction": "فقط برای", "account": "فقط در حساب",
            "text": "اگر متن پیامک یا توضیح شامل این بود", "priority": "اولویت (عدد کمتر = زودتر)",
            "enabled": "فعال",
        }

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = user_categories(user)
        self.fields["category"].empty_label = None
        self.fields["account"].queryset = user_accounts(user)
        self.fields["account"].empty_label = "همه حساب‌ها"
        self.fields["direction"].choices = [("", "دریافت و پرداخت"), *DIRECTIONS]
        self.setup_units(user.unit)

    def clean(self):
        data = super().clean()
        lo, hi = data.get("amount_min"), data.get("amount_max")
        if lo is not None and hi is not None and lo > hi:
            raise forms.ValidationError("حداقل مبلغ از حداکثر بیشتر است.")
        if not any([data.get("direction"), data.get("account"), data.get("text"), lo, hi]):
            raise forms.ValidationError("حداقل یک شرط لازم است؛ وگرنه همه تراکنش‌ها این دسته را می‌گیرند.")
        return data


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ["name", "kind", "archived"]
        labels = {"name": "نام", "kind": "نوع", "archived": "بایگانی"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.bank:  # SMS accounts stay "bank"
            del self.fields["kind"]


class ImportForm(forms.Form):
    text = forms.CharField(
        label="متن پیامک‌ها", max_length=1_000_000,
        widget=forms.Textarea(attrs={"rows": 10, "dir": "rtl", "placeholder": "پیامک را اینجا بچسبانید"}),
    )


class ShareForm(forms.Form):
    text = forms.CharField(label="متن (اطلاعات شخصی را پاک یا با * عوض کنید)", max_length=2000,
                           widget=forms.Textarea(attrs={"rows": 8}))


class FilterForm(forms.Form):
    q = forms.CharField(label="جستجو", required=False)
    month = forms.CharField(label="ماه", required=False)
    date_from = JalaliDateField(label="از تاریخ", required=False)
    date_to = JalaliDateField(label="تا تاریخ", required=False)
    direction = forms.ChoiceField(label="نوع", required=False, choices=[("", "همه"), *DIRECTIONS])
    category = forms.CharField(label="دسته", required=False)  # id, or "none" = uncategorized
    account = forms.ModelChoiceField(label="حساب", queryset=Account.objects.none(), required=False,
                                     empty_label="همه حساب‌ها")
    amount_min = AmountField(label="حداقل مبلغ", required=False)
    amount_max = AmountField(label="حداکثر مبلغ", required=False)
    gaps = forms.BooleanField(label="فقط جاهایی که پیامکی جا افتاده", required=False)

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["account"].queryset = Account.objects.filter(user=user)
        for f in ("amount_min", "amount_max"):
            self.fields[f].unit = user.unit
