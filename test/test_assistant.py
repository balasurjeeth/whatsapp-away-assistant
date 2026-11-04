import hashlib
import hmac
import json
import re
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from away_assistant.assistant import classify_message, create_decision, is_quiet_time, make_reply, validate_settings
from away_assistant.server import create_server
from away_assistant.service import AssistantService, normalize_webhook
from away_assistant.storage import Store


SETTINGS = {
    "reason": "busy",
    "returnAt": "after 6 PM",
    "urgentContact": "Alex",
    "timeZone": "America/New_York",
    "vipFriends": ["Maya", "+15550100"],
    "quietHours": {"start": "22:00", "end": "07:00"},
    "active": False,
    "mode": "approval",
    "sessionId": None,
}


class AssistantRulesTests(unittest.TestCase):
    def test_money_detection_precedes_urgent_detection(self):
        examples = (
            "urgent, send me money",
            "hospital bill payment",
            "UPI help needed",
            "Can you pay for dinner?",
            "Please send ₹500",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(classify_message(text), "money")

    def test_urgent_keywords_classify_without_retaining_text(self):
        self.assertEqual(classify_message("There was an accident, please help"), "urgent")

    def test_quiet_hours_use_configured_timezone_and_cross_midnight(self):
        self.assertTrue(is_quiet_time(datetime(2026, 1, 2, 3, tzinfo=timezone.utc), SETTINGS["quietHours"], SETTINGS["timeZone"]))
        self.assertTrue(is_quiet_time(datetime(2026, 1, 2, 8, tzinfo=timezone.utc), SETTINGS["quietHours"], SETTINGS["timeZone"]))
        self.assertFalse(is_quiet_time(datetime(2026, 1, 2, 17, tzinfo=timezone.utc), SETTINGS["quietHours"], SETTINGS["timeZone"]))

    def test_group_payment_and_unclassifiable_messages_never_get_a_reply(self):
        group = create_decision({"from": "1", "text": "hello", "isGroup": True}, SETTINGS)
        payment = create_decision({"from": "1", "text": "urgent UPI transfer"}, SETTINGS)
        media = create_decision({"from": "1", "text": "", "unclassifiable": True}, SETTINGS)
        voice_note = create_decision(
            {"from": "1", "text": "", "unclassifiable": True, "mediaKind": "voice_note"},
            SETTINGS,
        )
        self.assertEqual(group["action"], "skip")
        self.assertEqual(payment["action"], "manual")
        self.assertEqual(media["action"], "manual")
        self.assertEqual(voice_note, {"action": "manual", "reason": "voice_note_manual", "category": "voice_note"})
        self.assertNotIn("reply", payment)
        self.assertNotIn("reply", media)
        self.assertNotIn("reply", voice_note)

    def test_replies_are_short_and_avoid_promises(self):
        cases = (
            (make_reply("urgent", SETTINGS, "1"), "Alex"),
            (make_reply("normal", SETTINGS, "15550100"), "ring me"),
            (make_reply("normal", {**SETTINGS, "reason": "driving"}, "1"), "on the road"),
            (make_reply("normal", SETTINGS, "1"), "after 6 PM"),
        )
        for reply, expected in cases:
            with self.subTest(reply=reply):
                self.assertIn(expected.casefold(), reply.casefold())
                without_emoji = re.sub(r"[🙂📞🚗💛]", "", reply)
                self.assertLessEqual(len(re.findall(r"[.!?](?=\s|$)", without_emoji)), 2)
                self.assertNotRegex(reply, r"\b(?:I'll|I will|letting .* know)\b")
                self.assertEqual(sum(reply.count(emoji) for emoji in ("🙂", "📞", "🚗", "💛")), 1)

    def test_required_settings_and_timezone_are_validated(self):
        incomplete = {**SETTINGS, "vipFriends": [], "timeZone": "invalid/zone"}
        errors = validate_settings(incomplete)
        self.assertIn("Add at least one VIP friend, or enter 'none'.", errors)
        self.assertIn("Set a valid time zone.", errors)
        self.assertEqual(validate_settings({**SETTINGS, "vipFriends": ["none"], "quietHours": {"start": "off", "end": "off"}}), [])


class StorageAndServiceTests(unittest.TestCase):
    def test_storage_never_receives_incoming_message_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory).load()
            entry = {
                "id": "draft",
                "sessionId": "session",
                "sender": "15550100",
                "name": "Friend",
                "category": "normal",
                "status": "draft",
                "reply": "A prepared reply",
            }
            store.add_entry(entry, "meta-message-id")
            stored = (Path(directory) / "assistant.json").read_text(encoding="utf-8")
            self.assertNotIn("private incoming message content", stored)
            self.assertTrue(store.has_contact_in_session("15550100", "session"))
            self.assertTrue(store.has_processed("meta-message-id"))

    def test_one_draft_per_friend_in_an_away_session(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory).load()
            service = AssistantService(store, {})
            settings, errors = service.save_settings(
                {
                    **SETTINGS,
                    "quietHours": {"start": "off", "end": "off"},
                    "active": True,
                }
            )
            self.assertFalse(errors)
            self.assertIsNotNone(settings["sessionId"])
            service.process_message({"id": "message-1", "from": "15550100", "text": "hello"})
            service.process_message({"id": "message-2", "from": "15550100", "text": "another message"})
            entries = store.snapshot()["entries"]
            self.assertEqual(sum(entry["status"] == "draft" for entry in entries), 1)
            self.assertTrue(any(entry["reason"] == "already_replied" for entry in entries))

    def test_automatic_replies_for_two_friends_can_send_concurrently(self):
        with tempfile.TemporaryDirectory() as directory:
            credentials = {
                "WHATSAPP_ACCESS_TOKEN": "test-token",
                "WHATSAPP_PHONE_NUMBER_ID": "test-phone",
                "WHATSAPP_API_VERSION": "v99.0",
                "WHATSAPP_VERIFY_TOKEN": "test-verify",
                "WHATSAPP_APP_SECRET": "test-secret",
            }
            store = Store(directory).load()
            service = AssistantService(store, credentials)
            settings, errors = service.save_settings(
                {
                    **SETTINGS,
                    "quietHours": {"start": "off", "end": "off"},
                    "active": True,
                    "mode": "automatic",
                    "confirmAutomatic": True,
                }
            )
            self.assertFalse(errors)
            self.assertTrue(settings["sessionId"])

            both_sends_started = threading.Barrier(2)
            messages = [
                {"id": "message-a", "from": "15550100", "text": "hello"},
                {"id": "message-b", "from": "15550200", "text": "hello"},
            ]
            errors_from_threads = []

            def send_stub(_recipient, _text):
                both_sends_started.wait(timeout=2)

            def process(message):
                try:
                    service.process_message(message)
                except Exception as error:
                    errors_from_threads.append(error)

            with patch.object(service, "send_whatsapp_message", side_effect=send_stub):
                threads = [threading.Thread(target=process, args=(message,)) for message in messages]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)

            self.assertFalse(any(thread.is_alive() for thread in threads), "Both message threads should finish.")
            self.assertEqual(errors_from_threads, [])
            entries = store.snapshot()["entries"]
            self.assertEqual({entry["status"] for entry in entries}, {"sent"})
            self.assertEqual({entry["sender"] for entry in entries}, {"15550100", "15550200"})

    def test_simultaneous_messages_from_one_friend_still_create_one_draft(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory).load()
            service = AssistantService(store, {})
            settings, errors = service.save_settings(
                {
                    **SETTINGS,
                    "quietHours": {"start": "off", "end": "off"},
                    "active": True,
                }
            )
            self.assertFalse(errors)
            start_together = threading.Barrier(3)
            messages = [
                {"id": "message-a", "from": "15550100", "text": "first"},
                {"id": "message-b", "from": "15550100", "text": "second"},
            ]
            errors_from_threads = []

            def process(message):
                try:
                    start_together.wait(timeout=2)
                    service.process_message(message)
                except Exception as error:
                    errors_from_threads.append(error)

            threads = [threading.Thread(target=process, args=(message,)) for message in messages]
            for thread in threads:
                thread.start()
            start_together.wait(timeout=2)
            for thread in threads:
                thread.join(timeout=3)

            self.assertFalse(any(thread.is_alive() for thread in threads), "Both message threads should finish.")
            self.assertEqual(errors_from_threads, [])
            entries = store.snapshot()["entries"]
            self.assertEqual(sum(entry["status"] == "draft" for entry in entries), 1)
            self.assertEqual(sum(entry["reason"] == "already_replied" for entry in entries), 1)
            self.assertTrue(settings["sessionId"])

    def test_returning_creates_follow_ups_for_unhandled_away_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory).load()
            service = AssistantService(store, {})
            settings, errors = service.save_settings(
                {
                    **SETTINGS,
                    "quietHours": {"start": "off", "end": "off"},
                    "active": True,
                }
            )
            self.assertFalse(errors)
            session_id = settings["sessionId"]
            service.process_message({"id": "normal-1", "from": "15550100", "text": "hello"})
            service.process_message({"id": "money-1", "from": "15550200", "text": "urgent UPI transfer"})
            service.process_message({"id": "repeat-1", "from": "15550100", "text": "another message"})
            service.process_message({"id": "group-1", "from": "15550300", "text": "hello", "isGroup": True})

            _, errors = service.save_settings({**settings, "active": False})
            self.assertFalse(errors)
            entries = store.snapshot()["entries"]
            reminders = [entry for entry in entries if entry.get("followUp") == "pending"]
            self.assertEqual(len(reminders), 3)
            self.assertEqual(
                {entry["reason"] for entry in reminders},
                {"normal", "money", "already_replied"},
            )
            self.assertFalse(any(entry.get("reason") == "group" for entry in reminders))
            self.assertTrue(all(entry["sessionId"] == session_id for entry in reminders))

            reminder_id = reminders[0]["id"]
            status, result = service.complete_follow_up(reminder_id)
            self.assertEqual((status, result["status"]), (200, "done"))
            self.assertEqual(store.find_entry(reminder_id)["followUp"], "done")
            self.assertEqual(service.complete_follow_up(reminder_id)[0], 404)

    def test_voice_notes_are_kept_private_and_reminded_after_away_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory).load()
            service = AssistantService(store, {})
            settings, errors = service.save_settings(
                {
                    **SETTINGS,
                    "quietHours": {"start": "off", "end": "off"},
                    "active": True,
                }
            )
            self.assertFalse(errors)
            payload = {
                "entry": [{
                    "changes": [{
                        "value": {
                            "contacts": [{"wa_id": "15550100", "profile": {"name": "Maya"}}],
                            "messages": [{
                                "id": "voice-message-id",
                                "from": "15550100",
                                "type": "audio",
                                "audio": {
                                    "id": "private-media-id",
                                    "mime_type": "audio/ogg",
                                    "voice": True,
                                },
                            }],
                        }
                    }]
                }]
            }
            [message] = normalize_webhook(payload)
            self.assertEqual(message["mediaKind"], "voice_note")
            service.process_message(message)
            entry = store.snapshot()["entries"][0]
            self.assertEqual(entry["category"], "voice_note")
            self.assertEqual(entry["status"], "manual")
            self.assertFalse(entry["reply"])

            _, errors = service.save_settings({**settings, "active": False})
            self.assertFalse(errors)
            entry = store.snapshot()["entries"][0]
            self.assertEqual(entry["followUp"], "pending")
            saved = (Path(directory) / "assistant.json").read_text(encoding="utf-8")
            self.assertNotIn("private-media-id", saved)
            self.assertNotIn("audio/ogg", saved)
            self.assertNotIn('"voice": true', saved)
            self.assertNotIn("audio marker private content", saved)

    def test_webhook_normalizer_does_not_store_or_echo_message_content(self):
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "contacts": [{"wa_id": "15550100", "profile": {"name": "Maya"}}],
                        "messages": [{"id": "wamid", "from": "15550100", "type": "text", "text": {"body": "secret text"}}],
                    }
                }]
            }]
        }
        normalized = normalize_webhook(payload)
        self.assertEqual(normalized[0]["name"], "Maya")
        self.assertEqual(normalized[0]["text"], "secret text")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory).load()
            service = AssistantService(store, {})
            service.process_message(normalized[0], simulate=True)
            saved = (Path(directory) / "assistant.json").read_text(encoding="utf-8")
            self.assertNotIn("secret text", saved)


class HTTPIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.environment = {
            "WHATSAPP_APP_SECRET": "test-app-secret",
            "WHATSAPP_VERIFY_TOKEN": "test-verify-token",
        }
        self.server = create_server("127.0.0.1", 0, self.directory.name, self.environment)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.directory.cleanup()

    def request(self, path, method="GET", data=None, headers=None, raw_body=None):
        body = raw_body if raw_body is not None else json.dumps(data).encode("utf-8") if data is not None else None
        request = Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, response.read(), response.headers
        except HTTPError as error:
            result = error.code, error.read(), error.headers
            error.close()
            return result

    def test_dashboard_defaults_and_activation_requires_complete_settings(self):
        status, page, headers = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn(b"WhatsApp Away Assistant", page)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        status, body, _ = self.request("/api/state")
        state = json.loads(body)
        self.assertEqual(status, 200)
        self.assertFalse(state["settings"]["active"])
        self.assertEqual(state["settings"]["mode"], "approval")
        self.assertFalse(state["connected"])
        status, body, _ = self.request("/api/settings", "PUT", {"active": True})
        self.assertEqual(status, 400)
        self.assertTrue(json.loads(body)["errors"])

    def test_credentials_gate_automatic_and_manual_sending(self):
        valid = {**SETTINGS, "vipFriends": ["none"], "quietHours": {"start": "off", "end": "off"}}
        status, _, _ = self.request("/api/settings", "PUT", valid)
        self.assertEqual(status, 200)
        automatic = {**valid, "active": True, "mode": "automatic", "confirmAutomatic": True}
        status, body, _ = self.request("/api/settings", "PUT", automatic)
        self.assertEqual(status, 400)
        self.assertIn("credentials", json.loads(body)["errors"][0])

        status, _, _ = self.request("/api/settings", "PUT", {**valid, "active": True})
        self.assertEqual(status, 200)
        status, _, _ = self.request(
            "/api/simulate",
            "POST",
            {"from": "15550100", "name": "Friend", "text": "private sample text"},
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201)
        _, state_body, _ = self.request("/api/state")
        entry = json.loads(state_body)["entries"][0]
        self.assertEqual(entry["status"], "draft")
        status, body, _ = self.request(
            f"/api/entries/{entry['id']}/approve",
            "POST",
            {},
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("credentials", json.loads(body)["error"])
        persisted = (Path(self.directory.name) / "assistant.json").read_text(encoding="utf-8")
        self.assertNotIn("private sample text", persisted)

    def test_follow_up_can_be_marked_handled_through_dashboard_api(self):
        valid = {**SETTINGS, "vipFriends": ["none"], "quietHours": {"start": "off", "end": "off"}}
        self.assertEqual(self.request("/api/settings", "PUT", {**valid, "active": True})[0], 200)
        self.assertEqual(
            self.request(
                "/api/simulate",
                "POST",
                {"from": "15550100", "name": "Friend", "text": "hello"},
                {"Content-Type": "application/json"},
            )[0],
            201,
        )
        self.assertEqual(self.request("/api/settings", "PUT", {**valid, "active": False})[0], 200)
        _, state_body, _ = self.request("/api/state")
        entry = json.loads(state_body)["entries"][0]
        self.assertEqual(entry["followUp"], "pending")
        status, body, _ = self.request(
            f"/api/entries/{entry['id']}/follow-up/done",
            "POST",
            {},
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "done")
        _, state_body, _ = self.request("/api/state")
        self.assertEqual(json.loads(state_body)["entries"][0]["followUp"], "done")

    def test_webhook_requires_signature_and_verifies_meta_callback(self):
        status, body, _ = self.request(
            "/webhook?hub.mode=subscribe&hub.verify_token=test-verify-token&hub.challenge=abc123"
        )
        self.assertEqual((status, body), (200, b"abc123"))
        status, _, _ = self.request(
            "/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=abc123"
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "/webhook", "POST", {}, {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 401)
        payload = json.dumps({"entry": []}).encode("utf-8")
        signature = "sha256=" + hmac.new(
            self.environment["WHATSAPP_APP_SECRET"].encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
        status, body, _ = self.request(
            "/webhook",
            "POST",
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature},
            raw_body=payload,
        )
        self.assertEqual((status, body), (200, b"EVENT_RECEIVED"))

    def test_public_bind_requires_dashboard_password(self):
        with self.assertRaisesRegex(RuntimeError, "APP_PASSWORD"):
            create_server("0.0.0.0", 0, self.directory.name, {})


if __name__ == "__main__":
    unittest.main()
