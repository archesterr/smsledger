from django.contrib import admin
from django.urls import include, path

from ledger.views.auth import admin_login

urlpatterns = [
    path("admin/login/", admin_login),  # before admin.site.urls: admin must go through our 2FA login
    path("admin/", admin.site.urls),
    path("", include("ledger.urls")),
]
