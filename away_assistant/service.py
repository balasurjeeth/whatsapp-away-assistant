import hashlib
import hmac
import json
import logging
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .assistant import create_decision, validate_settings


LOGGER = logging.getLogger(__name__)


def has_live_credentials(environ=None):
    env = environ if environ is not None else os.environ
    names = (
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_API_VERSION",
        "WHATSAPP_VERIFY_TOKEN",
        "WHATSAPP_APP_SECRET",
    )
    return all(env.get(name) for name in names)


def normalize_webhook(payload):
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            names = {
                str(contact.get("wa_id", "")): (contact.get("profile") or {}).get("name", "")
                for contact in value.get("contacts", [])
            }
            for message in value.get("messages", []):
                message_type = message.get("type")
                interactive = message.get("interactive") or {}
                text = (
                    (message.get("text") or {}).get("body")
                    or (message.get("button") or {}).get("text")
                    or (interactive.get("button_reply") or {}).get("title")
                    or (interactive.get("list_reply") or {}).get("title")
                    or ""
                )
                sender = str(message.get("from", ""))
                messages.append(
                    {
                        "id": str(message.get("id", "")),
                        "from": sender,
                        "name": names.get(sender, ""),
                        "text": text,
                        "unclassifiable": message_type not in {"text", "button", "interactive"},
                        "mediaKind": (
                            "voice_note"
                            if message_type == "audio" and (message.get("audio") or {}).get("voice")
                            else ""
                        ),
                        "isGroup": bool(message.get("is_group")) or sender.endswith("@g.us"),
                    }
                )
    return messages


class AssistantService:
    def __init__(self, store, environ=None):
        self.store = store
        self.environ = environ if environ is not None else os.environ
        self.settings_lock = threading.RLock()
        self._keyed_locks = {}
        self._keyed_locks_guard = threading.Lock()

    @contextmanager
    def _lock_for(self, key):
        with self._keyed_locks_guard:
            record = self._keyed_locks.get(key)
            if record is None:
                record = [threading.Lock(), 0]
                self._keyed_locks[key] = record
            record[1] += 1
        try:
            record[0].acquire()
            yield
        finally:
            record[0].release()
            with self._keyed_locks_guard:
                record[1] -= 1
                if record[1] == 0:
                    self._keyed_locks.pop(key, None)

    def connected(self):
        return has_live_credentials(self.environ)

    def verify_webhook(self, raw_body, signature):
        secret = self.environ.get("WHATSAPP_APP_SECRET", "")
        if not secret or not signature or not signature.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    def send_whatsapp_message(self, recipient, text):
        if not self.connected():
            raise RuntimeError("WhatsApp sending is not configured.")
        version = urllib.parse.quote(self.environ["WHATSAPP_API_VERSION"], safe=".")
        phone_number_id = urllib.parse.quote(self.environ["WHATSAPP_PHONE_NUMBER_ID"], safe="")
        url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
        body = json.dumps(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "text",
                "text": {"body": text},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.environ['WHATSAPP_ACCESS_TOKEN']}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError("WhatsApp send request failed.")
        except (urllib.error.URLError, TimeoutError) as error:
            raise RuntimeError("WhatsApp send request failed.") from error

    @staticmethod
    def make_entry(message, decision, session_id, status="draft"):
        return {
            "id": str(uuid.uuid4()),
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "sessionId": session_id,
            "sender": message["from"],
            "name": message.get("name") or "Friend",
            "category": decision["category"],
            "contactKnown": decision.get("contactKnown", True),
            "status": status,
            "reason": decision["reason"],
            "mediaKind": "voice_note" if decision["category"] == "voice_note" else "",
            "reply": decision.get("reply", ""),
        }

    def process_message(self, message, simulate=False):
        sender = str(message.get("from", ""))
        if not sender:
            return
        with self._lock_for(("sender", sender)):
            message_id = message.get("id") or (str(uuid.uuid4()) if simulate else "")
            if not message_id:
                return
            if not simulate and self.store.has_processed(message_id):
                return

            automatic = False
            with self.settings_lock:
                settings = dict(self.store.state["settings"])
                settings["quietHours"] = dict(settings["quietHours"])
                settings["vipFriends"] = list(settings["vipFriends"])
                if (not settings["active"] or not settings["sessionId"]) and not simulate:
                    self.store.mark_processed(message_id)
                    return

                decision = create_decision(message, settings)
                if decision["action"] in {"skip", "manual"}:
                    status = "manual" if decision["action"] == "manual" else "ignored"
                    entry = self.make_entry(message, decision, settings["sessionId"], status)
                    self.store.add_entry(entry, None if simulate else message_id)
                    return
                if not simulate and self.store.has_contact_in_session(message["from"], settings["sessionId"]):
                    duplicate_decision = {**decision, "reason": "already_replied"}
                    entry = self.make_entry(message, duplicate_decision, settings["sessionId"], "ignored")
                    self.store.add_entry(entry, message_id)
                    return

                automatic = (
                    settings["active"]
                    and not simulate
                    and settings["mode"] == "automatic"
                    and decision.get("contactKnown", True)
                )
                entry = self.make_entry(
                    message,
                    decision,
                    settings["sessionId"],
                    "sending" if automatic else "draft",
                )
                self.store.add_entry(entry, None if simulate else message_id)
            if not automatic:
                return
            try:
                self.send_whatsapp_message(message["from"], decision["reply"])
                entry["status"] = "sent"
            except Exception:
                LOGGER.exception("Automatic WhatsApp send failed.")
                entry["status"] = "send_failed"
                entry["reason"] = "send_failed"
            self.store.save()

    def save_settings(self, data):
        with self.settings_lock:
            old_settings = self.store.state["settings"]
            ended_session_id = None
            quiet_hours = data.get("quietHours")
            if not isinstance(quiet_hours, dict):
                quiet_hours = {}
            vip_friends = data.get("vipFriends", [])
            if isinstance(vip_friends, list):
                vip_friends = [str(friend).strip() for friend in vip_friends if str(friend).strip()]
            else:
                vip_friends = [friend.strip() for friend in str(vip_friends).split(",") if friend.strip()]
            settings = {
                "reason": str(data.get("reason") or ""),
                "returnAt": str(data.get("returnAt") or "").strip(),
                "urgentContact": str(data.get("urgentContact") or "").strip(),
                "timeZone": str(data.get("timeZone") or "").strip(),
                "vipFriends": vip_friends,
                "quietHours": {
                    "start": str(quiet_hours.get("start") or "off"),
                    "end": str(quiet_hours.get("end") or "off"),
                },
                "active": bool(data.get("active")),
                "mode": "automatic" if data.get("mode") == "automatic" else "approval",
                "sessionId": old_settings.get("sessionId"),
            }
            errors = validate_settings(settings)
            if settings["active"] and errors:
                return None, errors
            if settings["active"] and settings["mode"] == "automatic" and not data.get("confirmAutomatic"):
                return None, ["Confirm automatic live sending before activation."]
            if settings["active"] and settings["mode"] == "automatic" and not self.connected():
                return None, ["Automatic sending requires configured Meta Cloud API credentials."]
            if settings["active"] and not old_settings["active"]:
                settings["sessionId"] = str(uuid.uuid4())
            if not settings["active"]:
                if old_settings["active"]:
                    ended_session_id = old_settings.get("sessionId")
                settings["sessionId"] = None
            self.store.update_settings(settings, ended_session_id)
            return settings, []

    def approve_draft(self, entry_id):
        entry = self.store.find_entry(entry_id)
        if not entry:
            return 404, {"error": "Draft not found."}
        with self._lock_for(("sender", entry["sender"])):
            entry = self.store.find_entry(entry_id)
            if not entry or entry["status"] != "draft":
                return 404, {"error": "Draft not found."}
            with self.settings_lock:
                settings = dict(self.store.state["settings"])
            if not settings["active"] or not settings["sessionId"] or entry["sessionId"] != settings["sessionId"]:
                return 409, {"error": "Activate away mode in approval mode before sending."}
            if settings["mode"] != "approval" and (
                settings["mode"] != "automatic" or entry.get("contactKnown", True)
            ):
                return 409, {"error": "Manual approval is available only in approval mode."}
            if not self.connected():
                return 400, {"error": "Sending is unavailable until Meta Cloud API credentials are configured."}
            entry["status"] = "sending"
            self.store.save()
            try:
                self.send_whatsapp_message(entry["sender"], entry["reply"])
                entry["status"] = "sent"
                self.store.save()
                return 200, {"status": entry["status"]}
            except Exception:
                LOGGER.exception("Approved WhatsApp send failed.")
                entry["status"] = "send_failed"
                entry["reason"] = "send_failed"
                self.store.save()
                return 502, {"error": "WhatsApp could not send this reply. Review the connection and try again."}

    def complete_follow_up(self, entry_id):
        entry = self.store.find_entry(entry_id)
        if not entry:
            return 404, {"error": "Follow-up reminder not found."}
        with self._lock_for(("sender", entry["sender"])):
            if not self.store.complete_follow_up(entry_id):
                return 404, {"error": "Follow-up reminder not found."}
        return 200, {"status": "done"}
