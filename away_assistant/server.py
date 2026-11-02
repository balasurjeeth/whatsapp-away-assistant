import base64
import binascii
import json
import logging
import os
import re
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .service import AssistantService, normalize_webhook
from .storage import Store


ROOT = Path(__file__).resolve().parent.parent
PUBLIC_DIR = ROOT / "public"
MAX_BODY_BYTES = 1_000_000
ENTRY_ID_PATTERN = re.compile(r"^[0-9a-f-]{36}$", re.IGNORECASE)
LOGGER = logging.getLogger(__name__)


def load_dotenv(path=ROOT / ".env"):
    """Load simple KEY=VALUE lines without overriding the process environment."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


def is_loopback_host(host):
    return host in {"127.0.0.1", "localhost", "::1"}


def make_handler(service, bind_host, environ):
    class RequestHandler(BaseHTTPRequestHandler):
        server_version = "AwayAssistant/1.0"
        sys_version = ""

        def log_message(self, format_string, *args):
            LOGGER.info("%s - %s", self.address_string(), format_string % args)

        def _send(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; style-src 'self'; "
                "script-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, status, data):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send(status, body)

        def _read_body(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("Invalid content length.") from error
            if length < 0:
                raise ValueError("Invalid content length.")
            if length > MAX_BODY_BYTES:
                raise OverflowError("Body too large.")
            return self.rfile.read(length)

        def _read_json(self):
            raw = self._read_body()
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("Invalid JSON.") from error
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            return data

        def _authorized(self):
            password = environ.get("APP_PASSWORD")
            if not password:
                return is_loopback_host(bind_host)
            header = self.headers.get("Authorization", "")
            scheme, separator, encoded = header.partition(" ")
            if not separator or scheme.casefold() != "basic":
                return False
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                return False
            _, delimiter, supplied = decoded.partition(":")
            if not delimiter:
                return False
            return hmac.compare_digest(supplied.encode("utf-8"), password.encode("utf-8"))

        def _require_auth(self):
            if self._authorized():
                return True
            status = 401 if environ.get("APP_PASSWORD") else 403
            self._send(
                status,
                b"Authentication required",
                "text/plain; charset=utf-8",
                {"WWW-Authenticate": 'Basic realm="Away Assistant"'},
            )
            return False

        def _guard(self, handler):
            try:
                handler()
            except Exception:
                self._handle_exception()

        def do_GET(self):
            self._guard(self._do_get)

        def _do_get(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/webhook":
                self._get_webhook(parse_qs(parsed.query))
                return
            if not self._require_auth():
                return
            if parsed.path == "/api/state":
                snapshot = service.store.snapshot()
                self._send_json(
                    200,
                    {
                        "settings": snapshot["settings"],
                        "entries": snapshot["entries"],
                        "connected": service.connected(),
                    },
                )
                return
            self._serve_static(parsed.path)

        def _get_webhook(self, query):
            mode = (query.get("hub.mode") or [""])[0]
            token = (query.get("hub.verify_token") or [""])[0]
            challenge = (query.get("hub.challenge") or [""])[0]
            expected = environ.get("WHATSAPP_VERIFY_TOKEN", "")
            if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
                self._send(200, challenge.encode("utf-8"), "text/plain; charset=utf-8")
            else:
                self._send(403, b"Webhook verification failed", "text/plain; charset=utf-8")

        def do_PUT(self):
            self._guard(self._do_put)

        def _do_put(self):
            if not self._require_auth():
                return
            if urlsplit(self.path).path != "/api/settings":
                self._send_json(404, {"error": "Not found."})
                return
            try:
                data = self._read_json()
            except OverflowError:
                self._send_json(413, {"error": "Body too large."})
                return
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            settings, errors = service.save_settings(data)
            if errors:
                self._send_json(400, {"errors": errors})
                return
            self._send_json(200, {"settings": settings})

        def do_POST(self):
            self._guard(self._do_post)

        def _do_post(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/webhook":
                self._post_webhook()
                return
            if not self._require_auth():
                return
            if parsed.path == "/api/simulate":
                self._post_simulate()
                return
            match = re.fullmatch(r"/api/entries/([^/]+)/approve", parsed.path)
            if match:
                entry_id = unquote(match.group(1))
                if not ENTRY_ID_PATTERN.fullmatch(entry_id):
                    self._send_json(404, {"error": "Draft not found."})
                    return
                status, result = service.approve_draft(entry_id)
                self._send_json(status, result)
                return
            match = re.fullmatch(r"/api/entries/([^/]+)/follow-up/done", parsed.path)
            if match:
                entry_id = unquote(match.group(1))
                if not ENTRY_ID_PATTERN.fullmatch(entry_id):
                    self._send_json(404, {"error": "Follow-up reminder not found."})
                    return
                status, result = service.complete_follow_up(entry_id)
                self._send_json(status, result)
                return
            self._send_json(404, {"error": "Not found."})

        def _post_webhook(self):
            try:
                raw = self._read_body()
            except OverflowError:
                self._send(413)
                return
            except ValueError:
                self._send(400)
                return
            if not service.verify_webhook(raw, self.headers.get("X-Hub-Signature-256", "")):
                self._send(401)
                return
            try:
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object.")
                messages = normalize_webhook(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                self._send(400)
                return
            for message in messages:
                service.process_message(message)
            self._send(200, b"EVENT_RECEIVED", "text/plain; charset=utf-8")

        def _post_simulate(self):
            try:
                data = self._read_json()
            except OverflowError:
                self._send_json(413, {"error": "Body too large."})
                return
            except ValueError as error:
                self._send_json(400, {"error": str(error)})
                return
            message = {
                "id": "",
                "from": str(data.get("from") or "15555550123"),
                "name": str(data.get("name") or "Test friend"),
                "text": str(data.get("text") or ""),
                "isGroup": bool(data.get("isGroup")),
            }
            service.process_message(message, simulate=True)
            self._send_json(201, {"received": True})

        def _serve_static(self, request_path):
            if self.command not in {"GET", "HEAD"} or ".." in request_path.split("/"):
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            allowed = {
                "/": (PUBLIC_DIR / "index.html", "text/html; charset=utf-8"),
                "/index.html": (PUBLIC_DIR / "index.html", "text/html; charset=utf-8"),
                "/app.js": (PUBLIC_DIR / "app.js", "text/javascript; charset=utf-8"),
                "/styles.css": (PUBLIC_DIR / "styles.css", "text/css; charset=utf-8"),
                "/README.md": (ROOT / "README.md", "text/markdown; charset=utf-8"),
            }
            target = allowed.get(request_path)
            if not target:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            file_path, content_type = target
            try:
                contents = file_path.read_bytes()
            except OSError:
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            self._send(200, contents, content_type)

        def do_HEAD(self):
            self._guard(self._do_get)

        def _handle_exception(self):
            LOGGER.exception("Request could not be completed.")
            if not self.wfile.closed:
                try:
                    self._send_json(500, {"error": "The request could not be completed."})
                except OSError:
                    pass

    return RequestHandler


class AwayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def create_server(host=None, port=None, data_dir=None, environ=None):
    env = environ if environ is not None else os.environ
    bind_host = host or env.get("HOST", "127.0.0.1")
    bind_port = int(port if port is not None else env.get("PORT", "3000"))
    if not env.get("APP_PASSWORD") and not is_loopback_host(bind_host):
        raise RuntimeError("Set APP_PASSWORD before binding the dashboard to a non-local interface.")
    directory = data_dir or env.get("DATA_DIR", str(ROOT / "data"))
    store = Store(directory).load()
    service = AssistantService(store, env)
    return AwayHTTPServer((bind_host, bind_port), make_handler(service, bind_host, env))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    server = create_server()
    host, port = server.server_address[:2]
    print(f"WhatsApp Away Assistant listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
