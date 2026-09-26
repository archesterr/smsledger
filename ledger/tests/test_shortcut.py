import json
import plistlib
import re
from urllib.parse import parse_qs, unquote, urlsplit

from django.test import override_settings

from ledger import security, shortcut, vault
from ledger.models import Device, Message

from .helpers import BaseTest, blu, login_client, make_device, make_user

UA_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_7_16 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"


class Offline(Exception):
    pass


class Phone:
    """Runs the generated Shortcut the way the Shortcuts app would, against the test server:
    files live in a dict, "Get Contents of URL" goes through the Django test client. It checks
    our wiring (branches, variables, queue handling), not Apple's implementation."""

    def __init__(self, client, ingest_url="http://testserver/ingest"):
        self.client, self.files, self.shown, self.offline = client, {}, [], False
        self.actions = plistlib.loads(shortcut.build(ingest_url))["WFWorkflowActions"]

    def value(self, v):
        if isinstance(v, str) or v is None:
            return v
        kind = v.get("WFSerializationType")
        if kind == "WFTextTokenAttachment":
            return self.ref(v["Value"])
        if kind == "WFTextTokenString":
            s, attachments = v["Value"]["string"], v["Value"].get("attachmentsByRange", {})
            out, units = [], 0
            for ch in s:
                if ch == shortcut.OBJ:
                    out.append(str(self.ref(attachments[f"{{{units}, 1}}"])))
                else:
                    out.append(ch)
                units += len(ch.encode("utf-16-le")) // 2
            return "".join(out)
        if kind == "WFDictionaryFieldValue":
            return {self.value(i["WFKey"]): self.value(i["WFValue"]) for i in v["Value"]["WFDictionaryFieldValueItems"]}
        raise AssertionError(v)

    def ref(self, ref):
        if ref["Type"] == "ExtensionInput":
            return self.input
        return self.outputs[ref["OutputUUID"]]

    @staticmethod
    def has_value(v):
        return v not in (None, "", [], {})

    def run(self, text=None):
        self.input, self.outputs = text or None, {}
        acts, pc = self.actions, 0
        while pc < len(acts):
            ident = acts[pc]["WFWorkflowActionIdentifier"].removeprefix("is.workflow.actions.")
            p = acts[pc]["WFWorkflowActionParameters"]
            out = None
            if ident == "conditional":
                group, mode = p["GroupingIdentifier"], p["WFControlFlowMode"]
                if mode == shortcut.IF:
                    subject = self.value(p["WFInput"]["Variable"])
                    if p["WFCondition"] == shortcut.BEGINS_WITH:
                        ok = str(subject or "").startswith(p["WFConditionalActionString"])
                    else:
                        assert p["WFCondition"] == shortcut.HAS_ANY_VALUE
                        ok = self.has_value(subject)
                    if not ok:  # to this group's Otherwise, or past its End If
                        pc = next(i for i in range(pc + 1, len(acts))
                                  if acts[i]["WFWorkflowActionParameters"].get("GroupingIdentifier") == group)
                elif mode == shortcut.OTHERWISE:  # the If branch ran: skip to End If
                    pc = next(i for i in range(pc + 1, len(acts))
                              if acts[i]["WFWorkflowActionParameters"].get("GroupingIdentifier") == group)
            elif ident == "exit":
                return
            elif ident == "documentpicker.save":
                assert p["WFAskWhereToSave"] is False and p["WFSaveFileOverwrite"] is True
                self.files[p["WFFileDestinationPath"]] = str(self.value(p["WFInput"]))
            elif ident == "documentpicker.open":
                assert p["WFShowFilePicker"] is False
                out = self.files.get(p["WFGetFilePath"])
                if out is None and p["WFFileErrorIfNotFound"]:
                    raise FileNotFoundError(p["WFGetFilePath"])
            elif ident == "file.append":
                path = p["WFFilePath"]
                self.files[path] = self.files.get(path, "") + str(self.value(p["WFInput"]))
            elif ident == "gettext":
                out = self.value(p["WFTextActionText"])
            elif ident == "text.match":
                flags = 0 if p["WFMatchTextCaseSensitive"] else re.I
                out = [m.group(0) for m in re.finditer(p["WFMatchTextPattern"], self.value(p["text"]), flags)]
            elif ident == "downloadurl":
                out = self.post(p)
            elif ident == "getvalueforkey":
                out = (self.value(p["WFInput"]) or {}).get(p["WFDictionaryKey"])
            elif ident == "showresult":
                self.shown.append(self.value(p["Text"]))
            else:
                raise AssertionError(f"unknown action {ident}")
            if "UUID" in p:
                self.outputs[p["UUID"]] = out
            pc += 1

    def post(self, p):
        if self.offline:
            raise Offline  # the Shortcuts app stops the whole run with an error
        assert p["WFHTTPMethod"] == "POST"
        url = urlsplit(p["WFURL"])
        headers = self.value(p["WFHTTPHeaders"])
        path = f"{url.path}?{url.query}"
        if p["WFHTTPBodyType"] == "JSON":
            body = self.value(p["WFJSONValues"]) if "WFJSONValues" in p else {}
            r = self.client.post(path, data=json.dumps(body), content_type="application/json",
                                 HTTP_AUTHORIZATION=headers["Authorization"])
        else:
            r = self.client.post(path, data=self.value(p["WFRequestVariable"]) or "", content_type="text/plain",
                                 HTTP_AUTHORIZATION=headers["Authorization"])
        return r.json()


class TestShortcutFile(BaseTest):
    def test_is_a_well_formed_plist_without_secrets(self):
        data = shortcut.build("https://tx.example.ir/ingest")
        self.assertEqual(data, shortcut.build("https://tx.example.ir/ingest"))  # reproducible
        wf = plistlib.loads(data)
        self.assertEqual(wf["WFWorkflowMinimumClientVersion"], 900)
        self.assertEqual(wf["WFWorkflowInputContentItemClasses"], ["WFStringContentItem"])
        self.assertNotIn(b"sml_", data)
        self.assertNotIn(b"Bearer sml", data)
        urls = [a["WFWorkflowActionParameters"]["WFURL"] for a in wf["WFWorkflowActions"]
                if a["WFWorkflowActionIdentifier"].endswith("downloadurl")]
        self.assertEqual(urls, ["https://tx.example.ir/ingest?source=connect", "https://tx.example.ir/ingest?source=iphone",
                                "https://tx.example.ir/ingest?split=1&source=queue"])

        # every If has an End If after it, and every variable points at an earlier action
        seen, open_groups = set(), []
        for a in wf["WFWorkflowActions"]:
            self.assertTrue(a["WFWorkflowActionIdentifier"].startswith("is.workflow.actions."))
            p = a["WFWorkflowActionParameters"]
            for ref in re.findall(r"'OutputUUID': '([0-9A-F-]+)'", repr(p)):
                self.assertIn(ref, seen)
            if a["WFWorkflowActionIdentifier"].endswith("conditional"):
                mode, group = p["WFControlFlowMode"], p["GroupingIdentifier"]
                if mode == shortcut.IF:
                    open_groups.append(group)
                elif mode == shortcut.OTHERWISE:
                    self.assertEqual(open_groups[-1], group)
                else:
                    self.assertEqual(open_groups.pop(), group)
            if "UUID" in p:
                seen.add(p["UUID"])
        self.assertEqual(open_groups, [])

    def test_variable_positions_count_utf16_units(self):
        t = shortcut._text("😀 ", shortcut.INPUT, " ریال ", shortcut.INPUT)
        self.assertEqual(set(t["Value"]["attachmentsByRange"]), {"{3, 1}", "{10, 1}"})
        self.assertEqual(shortcut._text("plain"),
                         {"Value": {"string": "plain"}, "WFSerializationType": "WFTextTokenString"})

    def test_otp_pattern(self):
        pattern = re.compile(shortcut.OTP_PATTERN, re.I)
        for otp in ("بانک ملت\nرمز پویا: 12345678", "کد تایید شما 1234", "رمز یکبار مصرف", "Your OTP is 99"):
            self.assertTrue(pattern.search(otp), otp)
        for sms in (blu(1_000, balance=5_000), "خرید رمز ارز\nمانده: 1,000",
                    "بانك ملي ايران\nانتقال:1,000-\nمانده:5,000"):
            self.assertFalse(pattern.search(sms), sms)

    def test_connect_url(self):
        url = shortcut.connect_url("sml_a+b/c")
        self.assertTrue(url.startswith("shortcuts://run-shortcut?"))
        q = parse_qs(urlsplit(url).query)
        self.assertEqual((q["name"], q["input"], q["text"]), (["SMS to Ledger"], ["text"], ["Bearer sml_a+b/c"]))
        self.assertNotIn("+", unquote(url.split("text=")[0]))


class TestShortcutRuns(BaseTest):
    """The generated Shortcut, end to end against /ingest."""

    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.device, self.token = make_device(self.user)
        vault.keyring().clear()
        self.phone = Phone(self.client)
        self.phone.run(f"Bearer {self.token}")  # what the Connect button does

    def queue(self):
        return self.phone.files.get(shortcut.QUEUE_FILE, "")

    def test_connect_saves_the_key_and_says_hello(self):
        self.assertEqual(self.phone.files[shortcut.KEY_FILE], f"Bearer {self.token}")
        self.assertEqual(len(self.phone.shown), 1)
        self.assertIn("✅", self.phone.shown[0])
        self.assertIn("«ali»", self.phone.shown[0])
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_used_at)
        self.assertFalse(Message.objects.exists())
        self.assertEqual(self.queue(), "")

    def test_revoked_key_explains_itself(self):
        phone = Phone(self.client)
        phone.run("Bearer sml_not-a-real-key")
        self.assertIn("اتصال این آیفون", phone.shown[0])

    def test_sms_is_queued_and_sent(self):
        self.phone.run(blu(1_000_000, balance=2_887_139))
        self.assertEqual(Message.objects.get(user=self.user).status, Message.PENDING)
        self.assertIn("1,000,000", self.queue())
        self.assertEqual(self.phone.shown, [self.phone.shown[0]])  # silent, no result sheet

    def test_otp_never_leaves_the_phone(self):
        self.phone.run("بانک ملت\nرمز پویا: 12345678\nمبلغ: 1,000,000 ریال")
        self.assertEqual(self.queue(), "")
        self.assertFalse(Message.objects.exists())

    def test_offline_sms_goes_with_the_nightly_run(self):
        self.phone.run(blu(100, "OUT", 900, time="10:00"))
        self.phone.offline = True
        with self.assertRaises(Offline):
            self.phone.run(blu(50, "IN", 950, time="11:00"))
        self.assertEqual(Message.objects.count(), 1)
        self.phone.offline = False
        self.phone.run()  # the 03:00 automation passes no input
        self.assertEqual(Message.objects.count(), 2)  # the first one again is a duplicate
        self.assertEqual(self.queue(), "---")
        self.phone.run()  # nothing new: still fine
        self.assertEqual((Message.objects.count(), self.queue()), (2, "---"))

    def test_failed_sync_keeps_the_queue(self):
        self.phone.offline = True
        with self.assertRaises(Offline):
            self.phone.run(blu(100, balance=900))
        self.phone.offline = False
        self.device.revoked_at = self.device.created_at
        self.device.save()
        self.phone.run()
        self.assertIn("100", self.queue())  # 401 has no "status": the queue stays
        self.assertFalse(Message.objects.exists())

    def test_not_connected_yet_is_an_error(self):
        phone = Phone(self.client)
        with self.assertRaises(FileNotFoundError):
            phone.run(blu(100, balance=900))


class TestSetupPage(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        login_client(self.client, self.user)

    def test_connect_button_creates_a_key_for_this_phone(self):
        used, _ = make_device(self.user, "old phone")
        Device.objects.filter(pk=used.pk).update(last_used_at=used.created_at)
        forgotten, forgotten_token = make_device(self.user, "tapped earlier")

        r = self.client.post("/setup/", {}, HTTP_USER_AGENT=UA_IPHONE)
        d = r.context["new_device"]
        self.assertEqual(d.name, "iPhone · iOS 16")
        page = r.content.decode()
        self.assertIn('href="shortcuts://run-shortcut?name=SMS%20to%20Ledger&amp;input=text&amp;text=Bearer%20sml_',
                      page)
        self.assertIn(f'data-device-status="/setup/devices/{d.pk}/status/"', page)
        forgotten.refresh_from_db()
        used.refresh_from_db()
        self.assertIsNotNone(forgotten.revoked_at)  # never used: tidied away
        self.assertIsNone(used.revoked_at)

        status = f"/setup/devices/{d.pk}/status/"
        self.assertEqual(self.client.get(status).json(), {"connected": False})
        r = self.client.post("/ingest?source=connect", data="{}", content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {r.context['new_token']}")
        self.assertEqual(r.json()["status"], "connected")
        self.assertEqual(self.client.get(status).json(), {"connected": True})
        self.assertEqual(self.post_sms(forgotten_token, {"sms": "x"}).status_code, 401)

    def test_status_is_private(self):
        other = make_user("sara")
        d, _ = make_device(other)
        self.assertEqual(self.client.get(f"/setup/devices/{d.pk}/status/").status_code, 404)

    def test_install_button_only_with_a_link(self):
        page = self.client.get("/setup/", HTTP_USER_AGENT=UA_IPHONE).content.decode()
        self.assertNotIn("نصب میان‌بر «", page)
        self.assertIn("ساختن دستی میان‌بر", page)
        self.assertNotIn('class="qr"', page)
        link = "https://www.icloud.com/shortcuts/0123456789abcdef"
        with override_settings(SHORTCUT_URL=link):
            page = self.client.get("/setup/", HTTP_USER_AGENT=UA_IPHONE).content.decode()
        self.assertIn(f'href="{link}"', page)
        self.assertIn("نصب میان‌بر «<bdi>SMS to Ledger</bdi>»", page)

    def test_on_a_computer_it_shows_a_qr_to_open_it_on_the_phone(self):
        page = self.client.get("/setup/", HTTP_USER_AGENT="Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0)")
        self.assertIn('class="qr"', page.content.decode())

    def test_shortcut_file_download(self):
        r = self.client.get("/setup/shortcut/")
        self.assertEqual(r["Content-Disposition"], 'attachment; filename="SMS to Ledger.shortcut"')
        wf = plistlib.loads(r.content)
        self.assertIn("http://testserver/ingest?source=iphone", repr(wf))
        self.client.logout()
        self.assertEqual(self.client.get("/setup/shortcut/").status_code, 302)

    def test_staff_page_has_the_admin_steps(self):
        self.user.is_staff, self.user.totp_secret = True, security.totp_new_secret()
        self.user.save(update_fields=["is_staff", "totp_secret"])
        page = self.client.get("/staff/").content.decode()
        self.assertIn("shortcuts sign --mode anyone", page)
        self.assertIn("Copy iCloud Link", page)


class TestIngestGuards(BaseTest):
    def setUp(self):
        super().setUp()
        self.user = make_user()
        self.device, self.token = make_device(self.user)

    def test_key_sent_as_an_sms_is_not_stored(self):
        # a Connect tap reaching an old hand-made shortcut that posts its input as the SMS
        r = self.post_sms(self.token, {"sms": "Bearer sml_abcdefghijklmnop"})
        self.assertEqual(r.json(), {"status": "ignored", "reason": "key"})
        self.assertFalse(Message.objects.exists())

    def test_errors_explain_themselves_but_never_carry_status(self):
        r = self.client.post("/ingest?source=connect", data="{}", content_type="application/json",
                             HTTP_AUTHORIZATION="Bearer sml_nope")
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("status", r.json())
        self.assertIn("message", r.json())
