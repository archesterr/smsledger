"""The iPhone Shortcut "SMS to Ledger", generated as a .shortcut file.

It holds no secret, so one copy serves everyone: the device key lives in a file on the phone
(Shortcuts/smsledger/key.txt), written by the setup page's "Connect" button, which runs the
shortcut with "Bearer <key>" as its input. One shortcut, three jobs, picked by its input:

  "Bearer sml_…"   Connect: save the key, say hello to the server, show its answer.
  an SMS            (Message automation) drop OTPs, append to the queue file, send it.
  nothing           (nightly automation, or run by hand) send the queue file, then empty it.

iOS 15+ only imports shortcut files signed by an Apple ID, and only a Mac (`shortcuts sign`)
or an iPhone sharing it can sign one. So the admin signs this file once, shares its iCloud link
as SHORTCUT_URL, and everyone else installs it with one tap. The file format follows what the
Shortcuts app writes; the action and parameter names were checked against the open-source
Cherri compiler's output.
"""
import plistlib
import uuid
from urllib.parse import quote

NAME = "SMS to Ledger"
KEY_FILE = "smsledger/key.txt"
QUEUE_FILE = "smsledger/queue.txt"
KEY_PREFIX = "Bearer "
# One-time passwords and verification codes never leave the phone (the server drops them too).
OTP_PATTERN = r"رمز(?!\s*ارز)|یک.?بار|کد.?(تایید|تأیید|ورود|فعال|پویا)|OTP"

CLIENT_VERSION = 900  # iOS 16
OBJ = "￼"  # where a variable sits inside a text field
# If conditions (WFCondition)
BEGINS_WITH, HAS_ANY_VALUE = 8, 100
IF, OTHERWISE, END_IF = 0, 1, 2
_NS = uuid.UUID("0b5f0d52-51ab-4c55-8f0e-4d3c2a7e9a61")


def _uid(label: str) -> str:
    # stable UUIDs: the same server always gets byte-identical files
    return str(uuid.uuid5(_NS, label)).upper()


INPUT = {"Type": "ExtensionInput"}  # "Shortcut Input"


def _out(label: str, name: str) -> dict:
    return {"Type": "ActionOutput", "OutputUUID": _uid(label), "OutputName": name}


def _var(ref: dict) -> dict:
    return {"Value": ref, "WFSerializationType": "WFTextTokenAttachment"}


def _text(*parts) -> dict:
    """A text field mixing literal strings and variables."""
    s, attachments = "", {}
    for p in parts:
        if isinstance(p, dict):
            attachments[f"{{{len(s.encode('utf-16-le')) // 2}, 1}}"] = p
            s += OBJ
        else:
            s += p
    value = {"string": s, "attachmentsByRange": attachments} if attachments else {"string": s}
    return {"Value": value, "WFSerializationType": "WFTextTokenString"}


def _dict(**items) -> dict:
    return {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": 0, "WFKey": _text(k), "WFValue": v} for k, v in items.items()
    ]}, "WFSerializationType": "WFDictionaryFieldValue"}


class _Actions(list):
    def add(self, ident: str, label: str | None = None, name: str | None = None, **params):
        if label:
            params["UUID"] = _uid(label)
            if name:
                params["CustomOutputName"] = name
        self.append({"WFWorkflowActionIdentifier": f"is.workflow.actions.{ident}",
                     "WFWorkflowActionParameters": params})

    def if_(self, group: str, ref: dict, condition: int, value: str | None = None):
        params = {"GroupingIdentifier": _uid(group), "WFControlFlowMode": IF, "WFCondition": condition,
                  "WFInput": {"Type": "Variable", "Variable": _var(ref)}}
        if value is not None:
            params["WFConditionalActionString"] = value
        self.add("conditional", **params)

    def otherwise(self, group: str):
        self.add("conditional", GroupingIdentifier=_uid(group), WFControlFlowMode=OTHERWISE)

    def end_if(self, group: str):
        self.add("conditional", label=f"{group}:end", GroupingIdentifier=_uid(group), WFControlFlowMode=END_IF)

    def get_file(self, label: str, name: str, path: str, error_if_missing: bool):
        self.add("documentpicker.open", label, name, WFGetFilePath=path, WFShowFilePicker=False,
                 WFFileErrorIfNotFound=error_if_missing)

    def save_file(self, ref: dict, path: str):
        self.add("documentpicker.save", WFInput=_var(ref), WFAskWhereToSave=False, WFFileDestinationPath=path,
                 WFSaveFileOverwrite=True)

    def post(self, label: str, name: str, url: str, auth: dict, **body):
        self.add("downloadurl", label, name, WFURL=url, WFHTTPMethod="POST", ShowHeaders=True,
                 WFHTTPHeaders=_dict(Authorization=_text(auth)), **body)


def actions(ingest_url: str) -> list[dict]:
    a = _Actions()
    key, queue = _out("key", "File"), _out("queue", "Queue")

    # 1. Connect: the setup page runs the shortcut with "Bearer <device key>"
    a.if_("connect", INPUT, BEGINS_WITH, KEY_PREFIX)
    a.save_file(INPUT, KEY_FILE)
    a.post("hello", "Server Reply", f"{ingest_url}?source=connect", INPUT, WFHTTPBodyType="JSON", WFJSONValues=_dict())
    a.add("getvalueforkey", "hello-message", "Message", WFInput=_var(_out("hello", "Server Reply")),
          WFGetDictionaryValueType="Value", WFDictionaryKey="message")
    a.add("showresult", Text=_text(_out("hello-message", "Message")))
    a.add("exit")
    a.end_if("connect")

    a.get_file("key", "File", KEY_FILE, error_if_missing=True)  # not connected yet: stop with an error

    # 2. An SMS from the Message automation
    a.if_("sms", INPUT, HAS_ANY_VALUE)
    a.add("text.match", "otp", "Matches", text=_text(INPUT), WFMatchTextPattern=OTP_PATTERN,
          WFMatchTextCaseSensitive=False)
    a.if_("otp", _out("otp", "Matches"), HAS_ANY_VALUE)
    a.add("exit")
    a.end_if("otp")
    # queued first: if there's no internet the request below fails, and the nightly run sends it
    a.add("gettext", "entry", "Queue Entry", WFTextActionText=_text("\n", INPUT, "\n---"))
    a.add("file.append", WFInput=_var(_out("entry", "Queue Entry")), WFFilePath=QUEUE_FILE,
          WFAppendFileWriteMode="Append")
    a.post("send", "Server Reply", f"{ingest_url}?source=iphone", key, WFHTTPBodyType="JSON",
           WFJSONValues=_dict(sms=_text(INPUT)))

    # 3. No input: the nightly automation (or a manual run) sends the whole queue
    a.otherwise("sms")
    a.get_file("queue", "Queue", QUEUE_FILE, error_if_missing=False)
    a.if_("queued", queue, HAS_ANY_VALUE)
    a.post("sync", "Server Reply", f"{ingest_url}?split=1&source=queue", key, WFHTTPBodyType="File",
           WFRequestVariable=_var(queue))
    a.add("getvalueforkey", "sync-status", "Status", WFInput=_var(_out("sync", "Server Reply")),
          WFGetDictionaryValueType="Value", WFDictionaryKey="status")
    # errors never carry "status" (views/api.py), so a failed sync keeps the queue
    a.if_("synced", _out("sync-status", "Status"), HAS_ANY_VALUE)
    a.add("gettext", "cleared", "Empty Queue", WFTextActionText="---")
    a.save_file(_out("cleared", "Empty Queue"), QUEUE_FILE)
    a.end_if("synced")
    a.end_if("queued")
    a.end_if("sms")
    return a


def build(ingest_url: str) -> bytes:
    """The unsigned .shortcut file (an XML plist), for `shortcuts sign` on a Mac."""
    return plistlib.dumps({
        "WFWorkflowClientVersion": str(CLIENT_VERSION),
        "WFWorkflowMinimumClientVersion": CLIENT_VERSION,
        "WFWorkflowMinimumClientVersionString": str(CLIENT_VERSION),
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": 431817727, "WFWorkflowIconGlyphNumber": 59414},  # teal, bubble
        "WFWorkflowImportQuestions": [],
        "WFWorkflowTypes": [],
        "WFQuickActionSurfaces": [],
        "WFWorkflowInputContentItemClasses": ["WFStringContentItem"],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowHasShortcutInputVariables": True,
        "WFWorkflowHasOutputFallback": False,
        "WFWorkflowActions": actions(ingest_url),
    }, sort_keys=True)


def connect_url(token: str) -> str:
    """Opens Shortcuts and runs the shortcut with the key as its input (Apple's URL scheme)."""
    return (f"shortcuts://run-shortcut?name={quote(NAME)}&input=text"
            f"&text={quote(KEY_PREFIX + token, safe='')}")
