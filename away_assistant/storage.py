import json
import os
import tempfile
import threading
from copy import deepcopy
from pathlib import Path


EMPTY_SETTINGS = {
    "reason": "",
    "returnAt": "",
    "urgentContact": "",
    "timeZone": "",
    "vipFriends": [],
    "quietHours": {"start": "off", "end": "off"},
    "active": False,
    "mode": "approval",
    "sessionId": None,
}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.file = self.directory / "assistant.json"
        self.lock = threading.RLock()
        self.state = None

    def load(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.directory, 0o700)
        try:
            with self.file.open(encoding="utf-8") as data_file:
                self.state = json.load(data_file)
            if os.name != "nt":
                os.chmod(self.file, 0o600)
            settings = self.state.get("settings") or {}
            self.state["settings"] = {
                **EMPTY_SETTINGS,
                **settings,
                "quietHours": {**EMPTY_SETTINGS["quietHours"], **(settings.get("quietHours") or {})},
            }
            if not isinstance(self.state.get("entries"), list):
                self.state["entries"] = []
            if not isinstance(self.state.get("processedIds"), list):
                self.state["processedIds"] = []
        except FileNotFoundError:
            self.state = {"settings": deepcopy(EMPTY_SETTINGS), "entries": [], "processedIds": []}
            self.save()
        return self

    def save(self):
        with self.lock:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".assistant-", suffix=".tmp", dir=self.directory)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    json.dump(self.state, output, ensure_ascii=False, indent=2)
                    output.flush()
                    os.fsync(output.fileno())
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                os.replace(temporary, self.file)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.state))

    def update_settings(self, settings):
        with self.lock:
            self.state["settings"] = settings
            self.save()

    def add_entry(self, entry, message_id=None):
        with self.lock:
            self.state["entries"].insert(0, entry)
            self.state["entries"] = self.state["entries"][:100]
            if message_id:
                self.state["processedIds"].append(message_id)
                self.state["processedIds"] = self.state["processedIds"][-500:]
            self.save()

    def has_processed(self, message_id):
        with self.lock:
            return message_id in self.state["processedIds"]

    def mark_processed(self, message_id):
        with self.lock:
            if message_id in self.state["processedIds"]:
                return
            self.state["processedIds"].append(message_id)
            self.state["processedIds"] = self.state["processedIds"][-500:]
            self.save()

    def has_contact_in_session(self, sender, session_id):
        with self.lock:
            return any(
                entry.get("sessionId") == session_id
                and entry.get("sender") == sender
                and entry.get("status") in {"draft", "sending", "sent", "send_failed"}
                for entry in self.state["entries"]
            )

    def find_entry(self, entry_id):
        with self.lock:
            return next((entry for entry in self.state["entries"] if entry.get("id") == entry_id), None)
