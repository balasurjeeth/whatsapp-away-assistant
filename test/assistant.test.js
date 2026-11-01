import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { classifyMessage, createDecision, isQuietTime, makeReply, validateSettings } from "../src/assistant.js";
import { Store } from "../src/storage.js";

const settings = {
  reason: "busy",
  returnAt: "after 6 PM",
  urgentContact: "Alex",
  timeZone: "America/New_York",
  vipFriends: ["Maya", "+15550100"],
  quietHours: { start: "22:00", end: "07:00" },
};

test("payment and UPI classification takes priority over urgent wording", () => {
  for (const text of ["urgent, send me money", "hospital bill payment", "UPI help needed", "Can you pay for dinner?", "Please send ₹500"]) {
    assert.equal(classifyMessage(text), "money");
  }
});

test("urgent words classify without returning message content", () => {
  assert.equal(classifyMessage("There was an accident, please help"), "urgent");
});

test("quiet hours handle overnight periods", () => {
  assert.equal(isQuietTime(new Date("2026-01-02T03:00:00Z"), settings.quietHours, settings.timeZone), true);
  assert.equal(isQuietTime(new Date("2026-01-02T08:00:00Z"), settings.quietHours, settings.timeZone), true);
  assert.equal(isQuietTime(new Date("2026-01-02T17:00:00Z"), settings.quietHours, settings.timeZone), false);
});

test("money messages are manually flagged and group chats are skipped", () => {
  assert.equal(createDecision({ message: { from: "1", text: "urgent UPI transfer", isGroup: false }, settings }).action, "manual");
  assert.equal(createDecision({ message: { from: "1", text: "hello", isGroup: true }, settings }).action, "skip");
  assert.equal(createDecision({ message: { from: "1", text: "", unclassifiable: true }, settings }).action, "manual");
});

test("activity persists a draft but never the incoming message content", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "away-assistant-"));
  try {
    const store = await new Store(directory).load();
    const entry = {
      id: "draft-1",
      sessionId: "session-1",
      sender: "15550100",
      name: "Friend",
      category: "normal",
      status: "draft",
      reply: "A prepared reply",
    };
    await Promise.all([
      store.addEntry(entry, "message-id"),
      store.addEntry({ ...entry, id: "draft-2", sender: "15550101" }),
      store.addEntry({ ...entry, id: "draft-3", sender: "15550102" }),
    ]);
    const file = await readFile(path.join(directory, "assistant.json"), "utf8");
    assert.doesNotMatch(file, /private incoming message content/i);
    assert.equal(JSON.parse(file).entries.length, 3);
    assert.equal(store.hasContactInSession("15550100", "session-1"), true);
    assert.equal(store.hasContactInSession("15550199", "session-1"), false);
    assert.equal(store.hasProcessed("message-id"), true);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test("VIP, driving, return-time, and urgent replies are short and avoid promises", () => {
  const cases = [
    [makeReply({ category: "urgent", settings, sender: "1" }), "Alex"],
    [makeReply({ category: "normal", settings, sender: "15550100" }), "ring me"],
    [makeReply({ category: "normal", settings: { ...settings, reason: "driving" }, sender: "1" }), "on the road"],
    [makeReply({ category: "normal", settings, sender: "1" }), "after 6 PM"],
  ];
  for (const [reply, expected] of cases) {
    assert.match(reply, new RegExp(expected, "i"));
    assert.ok(reply.split(/[.!?]+/).filter(Boolean).length <= 2);
    assert.doesNotMatch(reply, /\b(?:I'll|I will|letting .* know)\b/i);
  }
});

test("away settings must be complete before activation", () => {
  assert.deepEqual(validateSettings({ ...settings, vipFriends: [], quietHours: { start: "off", end: "off" } }), [
    "Add at least one VIP friend, or enter 'none'.",
  ]);
  assert.deepEqual(validateSettings({ ...settings, vipFriends: ["none"], quietHours: { start: "off", end: "off" } }), []);
});
