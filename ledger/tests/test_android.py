import re
import tempfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from ledger import android, security, shortcut
from ledger.models import Device, Message

from .helpers import BaseTest, blu, make_device, make_user

UA_ANDROID = ("Mozilla/5.0 (Linux; Android 14; K) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Mobile Safari/537.36")
UA_APP = "smsledger-android/1.0.7 (Android 14)"
CORE = Path(settings.BASE_DIR) / "android/app/src/main/java/app/smsledger/Core.java"


def java_string(name: str) -> str:
    """A String constant from the app's source, unescaped."""
    m = re.search(rf'{name} = "((?:[^"\\]|\\.)*)";', CORE.read_text(encoding="utf-8"))
    return m.group(1).encode().decode("unicode_escape").encode("latin-1").decode("utf-8")


class TestAndroidSetup(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.login(self.user)

    def test_android_gets_its_own_guide(self):
        page = self.client.get("/setup/", HTTP_USER_AGENT=UA_ANDROID).content.decode()
        self.assertIn('href="/android.apk"', page)
        self.assertIn("Allow restricted settings", page)
        self.assertNotIn("Shortcuts", page)
        self.assertNotIn('class="qr"', page)

    def test_connect_hands_the_key_to_the_app(self):
        r = self.client.post("/setup/", {"name": ""}, HTTP_USER_AGENT=UA_ANDROID)
        device = Device.objects.get(user=self.user)
        self.assertEqual(device.name, "Android")
        url = r.context["connect_url"]
        self.assertTrue(url.startswith("intent://connect?"))
        self.assertIn(f";package={android.PACKAGE};", url)
        q = parse_qs(urlsplit(url.split("#", 1)[0]).query)
        self.assertEqual(q["server"], ["http://testserver/"])
        self.assertEqual(security.sha256(q["key"][0]), device.token_hash)
        fallback = unquote(re.search(r"S\.browser_fallback_url=([^;]+)", url).group(1))
        self.assertEqual(fallback, "http://testserver/setup/#step-1")  # not the Play Store
        self.assertEqual(r.context["start_step"], 2)
        self.assertIn('data-device-status="/setup/devices/', r.content.decode())

    def test_iphone_guide_is_unchanged(self):
        ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X)"
        r = self.client.post("/setup/", {"name": ""}, HTTP_USER_AGENT=ua)
        self.assertTrue(r.context["connect_url"].startswith("shortcuts://run-shortcut?"))
        self.assertTemplateUsed(r, "ledger/setup.html")

    def test_finished_setup_opens_on_the_last_step(self):
        d, token = make_device(self.user, "Android")
        self.post_sms(token, {"sms": blu(1_000, balance=5_000)})
        r = self.client.get("/setup/", HTTP_USER_AGENT=UA_ANDROID)
        self.assertEqual(r.context["start_step"], 5)

    def test_old_sms_are_sent_from_the_app(self):
        page = self.client.get("/import/", HTTP_USER_AGENT=UA_ANDROID).content.decode()
        self.assertIn("رایانه لازم نیست", page)
        self.assertNotIn("sql-wasm", page)

    def test_quiet_phone_advice_fits_android(self):
        d, _ = make_device(self.user, "Android")
        Device.objects.filter(pk=d.pk).update(created_at=timezone.now() - timedelta(days=10))
        page = self.client.get("/").content.decode()
        self.assertIn("چند روز است پیامکی نرسیده", page)
        self.assertIn("«وصل است»", page)
        self.assertNotIn("Automation", page.split("چند روز است")[1][:600])

    def test_join_page_on_android_has_no_qr(self):
        self.client.logout()
        admin = make_user("boss", is_staff=True, totp_secret=security.totp_new_secret())
        self.login(admin)
        self.client.post("/staff/invites/", {"note": ""})
        path = self.client.get("/staff/").context["new_link"].split("testserver", 1)[1]
        self.client.logout()
        page = self.client.get(path, HTTP_USER_AGENT=UA_ANDROID).content.decode()
        self.assertNotIn('class="qr', page)


class TestAndroidApi(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.device, self.token = make_device(self.user, "Android")

    def test_connect_names_the_account(self):
        r = self.client.post("/ingest?source=connect", HTTP_AUTHORIZATION=f"Bearer {self.token}",
                             HTTP_USER_AGENT=UA_APP, content_type="application/json", data="{}")
        self.assertEqual(r.json()["status"], "connected")
        self.assertEqual(r.json()["account"], "ali")
        self.assertIn("این گوشی", r.json()["message"])
        self.assertNotIn("اتوماسیون", r.json()["message"])
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_used_at)

    def test_the_apps_batch_keeps_arrival_times(self):
        at = timezone.now() - timedelta(days=40)
        body = {"items": [{"text": blu(2_000, balance=9_000), "at": int(at.timestamp() * 1000)}],
                "source": "android-import"}
        r = self.post_sms(self.token, body)
        self.assertEqual(r.json(), {"status": "ok", "count": {"received": 1}})  # sealed until the next login
        msg = Message.objects.get()
        self.assertEqual(msg.source, "android-import")
        self.assertAlmostEqual(msg.received_at.timestamp(), at.timestamp(), delta=1)
        again = self.post_sms(self.token, {"items": body["items"], "source": "android"})
        self.assertEqual(again.json()["count"], {"duplicate": 1})

    def test_config_is_public(self):
        r = self.client.get("/android/config.json")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["otp"], shortcut.OTP_PATTERN)
        self.assertEqual([p["key"] for p in data["periods"]], ["month", "3months", "year", "all"])
        self.assertEqual(data["periods"][-1]["start"], 0)
        self.assertTrue(data["periods"][0]["label"])

    def test_app_filters_match_the_server(self):
        self.assertEqual(java_string("OTP_PATTERN"), shortcut.OTP_PATTERN)

    def test_apk_from_github_until_the_admin_hosts_it(self):
        with tempfile.TemporaryDirectory() as d, override_settings(ANDROID_APK_DIR=Path(d)):
            r = self.client.get("/android.apk")
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r["Location"], settings.ANDROID_APK_URL)
            (Path(d) / "smsledger.apk").write_bytes(b"PK\x03\x04apk")
            r = self.client.get("/android.apk")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r["Content-Type"], "application/vnd.android.package-archive")
            self.assertIn('filename="smsledger.apk"', r["Content-Disposition"])
            self.assertEqual(b"".join(r.streaming_content), b"PK\x03\x04apk")
