"""Admin is for user/invite/device management only. Financial models (transactions, messages,
categories, rules, budgets) are deliberately not registered: the operator has no screen for
browsing friends' money."""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from . import vault
from .models import Device, Invite, SupportSample, User


@admin.register(User)
class LedgerUserAdmin(UserAdmin):
    list_display = ("username", "is_active", "is_staff", "encryption", "date_joined", "last_login")
    fieldsets = (
        (None, {"fields": ("username", "password", "encryption")}),
        ("Status", {"fields": ("is_active", "is_staff", "is_superuser")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    readonly_fields = ("last_login", "date_joined", "encryption")

    @admin.display(description="Data encryption")
    def encryption(self, obj):
        return {
            vault.PROTECTED: "Protected. Setting a new password here LOCKS this user's data until they enter "
                             "their recovery key (you can't unlock it for them).",
            vault.UNPROTECTED: "Waiting for the user's first login since encryption was added.",
            vault.LOCKED: "Locked: the user must enter their recovery key (or start over).",
        }.get(obj.vault_state, "No keys yet.")


@admin.register(Invite)
class InviteAdmin(admin.ModelAdmin):
    list_display = ("note", "created_at", "expires_at", "used_by", "used_at")
    readonly_fields = ("code_hash", "created_by", "used_by", "used_at")

    def has_add_permission(self, request):  # codes are generated: staff page or `manage.py invite`
        return False


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ("user", "name", "token_prefix", "created_at", "last_used_at", "revoked_at")
    readonly_fields = ("user", "token_hash", "token_prefix", "created_at", "last_used_at")

    def has_add_permission(self, request):  # tokens are generated on the user's setup page
        return False


@admin.register(SupportSample)
class SupportSampleAdmin(admin.ModelAdmin):
    list_display = ("user", "created_at", "resolved")
