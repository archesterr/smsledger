"""Admin is for user/invite/device management only. Financial models (transactions, messages,
categories, rules, budgets) are deliberately not registered: the operator has no screen for
browsing friends' money."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Device, Invite, SupportSample, User


@admin.register(User)
class LedgerUserAdmin(UserAdmin):
    list_display = ("username", "is_active", "is_staff", "date_joined", "last_login")
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Status", {"fields": ("is_active", "is_staff", "is_superuser")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    readonly_fields = ("last_login", "date_joined")


@admin.register(Invite)
class InviteAdmin(admin.ModelAdmin):
    list_display = ("note", "created_at", "expires_at", "used_by", "used_at")
    readonly_fields = ("code_hash", "created_by", "used_by", "used_at")


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "name", "token_prefix", "created_at", "last_used_at", "revoked_at")
    readonly_fields = ("user", "token_hash", "token_prefix", "created_at", "last_used_at")


@admin.register(SupportSample)
class SupportSampleAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at", "resolved")
