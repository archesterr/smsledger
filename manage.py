#!/usr/bin/env python
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.exceptions import ImproperlyConfigured

    try:
        # Django swallows settings errors here and later fails with a misleading
        # "Models aren't loaded yet": surface the real cause (e.g. SECRET_KEY unset).
        from django.conf import settings

        settings.INSTALLED_APPS  # noqa: B018
    except ImproperlyConfigured as e:
        sys.exit(f"configuration error: {e}")

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
