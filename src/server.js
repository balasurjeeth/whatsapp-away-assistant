import { createHmac, randomUUID, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createDecision, validateSettings } from "./assistant.js";
import { Store } from "./storage.js";

const root = path.dirname(fileURLToPath(import.meta.url));
const publicDir = path.resolve(root, "..", "public");
const host = process.env.HOST || "127.0.0.1";
const port = Number(process.env.PORT || 3000);
const store = await new Store(process.env.DATA_DIR || "./data").load();
const MAX_BODY_BYTES = 1_000_000;

function hasLiveCredentials() {
  return Boolean(
    process.env.WHATSAPP_ACCESS_TOKEN &&
      process.env.WHATSAPP_PHONE_NUMBER_ID &&
      process.env.WHATSAPP_API_VERSION &&
    process.env.WHATSAPP_VERIFY_TOKEN &&
    process.env.WHATSAPP_APP_SECRET,
  );
}

function sendJson(response, status, data) {
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(JSON.stringify(data));
}

async function readJson(request) {
  const chunks = [];
  let length = 0;
  for await (const chunk of request) {
    length += chunk.length;
    if (length > MAX_BODY_BYTES) throw Object.assign(new Error("Body too large"), { status: 413 });
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw Object.assign(new Error("Invalid JSON"), { status: 400 });
  }
}

function isAuthorized(request) {
  const password = process.env.APP_PASSWORD;
  if (!password) return ["127.0.0.1", "localhost", "::1"].includes(host);
  const match = (request.headers.authorization || "").match(/^Basic\s+(.+)$/i);
  if (!match) return false;
  let supplied;
  try {
    supplied = Buffer.from(match[1], "base64").toString("utf8").split(":").slice(1).join(":");
  } catch {
    return false;
  }
  const a = Buffer.from(supplied);
  const b = Buffer.from(password);
  return a.length === b.length && timingSafeEqual(a, b);
}

function validWebhookSignature(body, signature) {
  const secret = process.env.WHATSAPP_APP_SECRET;
  if (!secret || !signature || !signature.startsWith("sha256=")) return false;
  const expected = `sha256=${createHmac("sha256", secret).update(body).digest("hex")}`;
  const actualBuffer = Buffer.from(signature);
  const expectedBuffer = Buffer.from(expected);
  return actualBuffer.length === expectedBuffer.length && timingSafeEqual(actualBuffer, expectedBuffer);
}

function safeEqualText(actual, expected) {
  const actualBuffer = Buffer.from(actual);
  const expectedBuffer = Buffer.from(expected);
  return actualBuffer.length === expectedBuffer.length && timingSafeEqual(actualBuffer, expectedBuffer);
}

function normalizeWebhook(payload) {
  const messages = [];
  for (const entry of payload.entry || []) {
    for (const change of entry.changes || []) {
      const value = change.value || {};
      const names = new Map((value.contacts || []).map((contact) => [
        contact.wa_id,
        contact.profile?.name || "",
      ]));
      for (const message of value.messages || []) {
        messages.push({
          id: String(message.id || ""),
          from: String(message.from || ""),
          name: names.get(String(message.from || "")) || "",
          text: message.text?.body || message.button?.text || message.interactive?.button_reply?.title || message.interactive?.list_reply?.title || "",
          unclassifiable: !["text", "button", "interactive"].includes(message.type),
          isGroup: Boolean(message.is_group || String(message.from || "").endsWith("@g.us")),
        });
      }
    }
  }
  return messages;
}

async function sendWhatsAppMessage(recipient, text) {
  if (!hasLiveCredentials()) throw new Error("WhatsApp sending is not configured.");
  const response = await fetch(
    `https://graph.facebook.com/${process.env.WHATSAPP_API_VERSION}/${process.env.WHATSAPP_PHONE_NUMBER_ID}/messages`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${process.env.WHATSAPP_ACCESS_TOKEN}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        messaging_product: "whatsapp",
        recipient_type: "individual",
        to: recipient,
        type: "text",
        text: { body: text },
      }),
      signal: AbortSignal.timeout(10_000),
    },
  );
  if (!response.ok) throw new Error("WhatsApp send request failed.");
}

function makeEntry({ message, decision, sessionId, status = "draft" }) {
  return {
    id: randomUUID(),
    createdAt: new Date().toISOString(),
    sessionId,
    sender: message.from,
    name: message.name || "Friend",
    category: decision.category,
    status,
    reason: decision.reason,
    reply: decision.reply || "",
  };
}

async function processMessage(message, { simulate = false } = {}) {
  const { settings } = store.state;
  const messageId = message.id || (simulate ? randomUUID() : "");
  if (!message.from || !messageId) return;
  if ((!settings.active || !settings.sessionId) && !simulate) {
    await store.markProcessed(messageId);
    return;
  }
  if (!simulate && store.hasProcessed(messageId)) return;
  const decision = createDecision({ message, settings });
  if (decision.action === "skip" || decision.action === "manual") {
    await store.addEntry(makeEntry({
      message,
      decision,
      sessionId: settings.sessionId,
      status: decision.action === "manual" ? "manual" : "ignored",
    }), simulate ? null : messageId);
    return;
  }
  if (!simulate && store.hasContactInSession(message.from, settings.sessionId)) {
    await store.addEntry(makeEntry({
      message,
      decision: { ...decision, reason: "already_replied" },
      sessionId: settings.sessionId,
      status: "ignored",
    }), simulate ? null : messageId);
    return;
  }

  const automatic = settings.active && !simulate && settings.mode === "automatic";
  const entry = makeEntry({
    message,
    decision,
    sessionId: settings.sessionId,
    status: automatic ? "sending" : "draft",
  });
  await store.addEntry(entry, simulate ? null : messageId);
  if (!automatic) return;

  try {
    await sendWhatsAppMessage(message.from, decision.reply);
    entry.status = "sent";
  } catch {
    entry.status = "send_failed";
    entry.reason = "send_failed";
  }
  await store.save();
}

function sanitizeSettings(input, oldSettings) {
  return {
    reason: String(input.reason || ""),
    returnAt: String(input.returnAt || "").trim(),
    urgentContact: String(input.urgentContact || "").trim(),
    timeZone: String(input.timeZone || ""),
    vipFriends: Array.isArray(input.vipFriends)
      ? input.vipFriends.map((friend) => String(friend).trim()).filter(Boolean)
      : String(input.vipFriends || "").split(",").map((friend) => friend.trim()).filter(Boolean),
    quietHours: {
      start: String(input.quietHours?.start || "off"),
      end: String(input.quietHours?.end || "off"),
    },
    active: Boolean(input.active),
    mode: input.mode === "automatic" ? "automatic" : "approval",
    sessionId: oldSettings.sessionId,
  };
}

async function route(request, response) {
  const url = new URL(request.url, `http://${request.headers.host || "localhost"}`);
  if (url.pathname === "/webhook" && request.method === "GET") {
    const mode = url.searchParams.get("hub.mode");
    const token = url.searchParams.get("hub.verify_token");
    const challenge = url.searchParams.get("hub.challenge");
    if (
      mode === "subscribe" &&
      process.env.WHATSAPP_VERIFY_TOKEN &&
      safeEqualText(token || "", process.env.WHATSAPP_VERIFY_TOKEN)
    ) {
      response.writeHead(200, { "content-type": "text/plain" });
      response.end(challenge || "");
    } else {
      response.writeHead(403);
      response.end("Webhook verification failed");
    }
    return;
  }

  if (url.pathname === "/webhook" && request.method === "POST") {
    const chunks = [];
    let length = 0;
    for await (const chunk of request) {
      length += chunk.length;
      if (length > MAX_BODY_BYTES) {
        response.writeHead(413);
        response.end();
        return;
      }
      chunks.push(chunk);
    }
    const raw = Buffer.concat(chunks);
    if (!validWebhookSignature(raw, request.headers["x-hub-signature-256"])) {
      response.writeHead(401);
      response.end();
      return;
    }
    const payload = JSON.parse(raw.toString("utf8"));
    for (const message of normalizeWebhook(payload)) await processMessage(message);
    response.writeHead(200, { "content-type": "text/plain" });
    response.end("EVENT_RECEIVED");
    return;
  }

  if (!isAuthorized(request)) {
    response.writeHead(process.env.APP_PASSWORD ? 401 : 403, {
      "www-authenticate": "Basic realm=\"Away Assistant\"",
    });
    response.end("Authentication required");
    return;
  }

  if (url.pathname === "/api/state" && request.method === "GET") {
    sendJson(response, 200, {
      settings: store.state.settings,
      entries: store.state.entries,
      connected: hasLiveCredentials(),
    });
    return;
  }

  if (url.pathname === "/api/settings" && request.method === "PUT") {
    const input = await readJson(request);
    const next = sanitizeSettings(input, store.state.settings);
    const errors = validateSettings(next);
    if (next.active && errors.length) {
      sendJson(response, 400, { errors });
      return;
    }
    if (next.active && next.mode === "automatic" && !input.confirmAutomatic) {
      sendJson(response, 400, { errors: ["Confirm automatic live sending before activation."] });
      return;
    }
    if (next.active && next.mode === "automatic" && !hasLiveCredentials()) {
      sendJson(response, 400, { errors: ["Automatic sending requires configured Meta Cloud API credentials."] });
      return;
    }
    if (next.active && !store.state.settings.active) next.sessionId = randomUUID();
    if (!next.active) next.sessionId = null;
    await store.updateSettings(next);
    sendJson(response, 200, { settings: next });
    return;
  }

  if (url.pathname === "/api/simulate" && request.method === "POST") {
    const input = await readJson(request);
    const message = {
      id: randomUUID(),
      from: String(input.from || "15555550123"),
      name: String(input.name || "Test friend"),
      text: String(input.text || ""),
      isGroup: Boolean(input.isGroup),
    };
    await processMessage(message, { simulate: true });
    sendJson(response, 201, { received: true });
    return;
  }

  const approvalMatch = url.pathname.match(/^\/api\/entries\/([0-9a-f-]+)\/approve$/i);
  if (approvalMatch && request.method === "POST") {
    const entry = store.state.entries.find((item) => item.id === approvalMatch[1]);
    if (!entry || entry.status !== "draft") {
      sendJson(response, 404, { error: "Draft not found." });
      return;
    }
    const settings = store.state.settings;
    if (!settings.active || !settings.sessionId || entry.sessionId !== settings.sessionId) {
      sendJson(response, 409, { error: "Activate away mode in approval mode before sending." });
      return;
    }
    if (settings.mode !== "approval") {
      sendJson(response, 409, { error: "Manual approval is available only in approval mode." });
      return;
    }
    if (!hasLiveCredentials()) {
      sendJson(response, 400, { error: "Sending is unavailable until Meta Cloud API credentials are configured." });
      return;
    }
    entry.status = "sending";
    await store.save();
    try {
      await sendWhatsAppMessage(entry.sender, entry.reply);
      entry.status = "sent";
      await store.save();
      sendJson(response, 200, { status: entry.status });
    } catch {
      entry.status = "send_failed";
      entry.reason = "send_failed";
      await store.save();
      sendJson(response, 502, { error: "WhatsApp could not send this reply. Review the connection and try again." });
    }
    return;
  }

  if (url.pathname.startsWith("/api/")) {
    sendJson(response, 404, { error: "Not found." });
    return;
  }

  if (request.method !== "GET" || url.pathname.includes("..")) {
    response.writeHead(404);
    response.end();
    return;
  }
  const requested = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
  try {
    const file = requested === "README.md"
      ? await readFile(path.resolve(root, "..", "README.md"))
      : await readFile(path.join(publicDir, requested));
    const extension = path.extname(requested);
    const types = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".md": "text/markdown" };
    response.writeHead(200, {
      "content-type": `${types[extension] || "application/octet-stream"}; charset=utf-8`,
      "x-content-type-options": "nosniff",
      "content-security-policy": "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; frame-ancestors 'none'",
      "referrer-policy": "no-referrer",
    });
    response.end(file);
  } catch {
    response.writeHead(404);
    response.end("Not found");
  }
}

if (!(process.env.APP_PASSWORD) && !["127.0.0.1", "localhost", "::1"].includes(host)) {
  throw new Error("Set APP_PASSWORD before binding the dashboard to a non-local interface.");
}

createServer((request, response) => {
  route(request, response).catch((error) => {
    sendJson(response, error.status || 500, {
      error: error.status ? error.message : "The request could not be completed.",
    });
  });
}).listen(port, host, () => {
  console.log(`WhatsApp Away Assistant listening on http://${host}:${port}`);
});
