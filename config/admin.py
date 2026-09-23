from django.contrib import admin
from django.contrib.admin.apps import AdminConfig


class TwoFactorAdminSite(admin.AdminSite):
    site_header = site_title = "smsledger admin"

    def has_permission(self, request):
        # same rule as the staff pages: no admin without 2FA, whatever the password
        return super().has_permission(request) and getattr(request.user, "has_2fa", False)


class LedgerAdminConfig(AdminConfig):
    default_site = "config.admin.TwoFactorAdminSite"
