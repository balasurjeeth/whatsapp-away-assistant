import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";

const EMPTY_SETTINGS = {
  reason: "",
  returnAt: "",
  urgentContact: "",
  timeZone: "",
  vipFriends: [],
  quietHours: { start: "off", end: "off" },
  active: false,
  mode: "approval",
  sessionId: null,
};

export class Store {
  constructor(directory) {
    this.directory = path.resolve(directory);
    this.file = path.join(this.directory, "assistant.json");
    this.state = null;
    this.writeQueue = Promise.resolve();
  }

  async load() {
    await mkdir(this.directory, { recursive: true });
    try {
      this.state = JSON.parse(await readFile(this.file, "utf8"));
      this.state.settings = {
        ...EMPTY_SETTINGS,
        ...this.state.settings,
        quietHours: { ...EMPTY_SETTINGS.quietHours, ...this.state.settings.quietHours },
      };
      this.state.entries = Array.isArray(this.state.entries) ? this.state.entries : [];
      this.state.processedIds = Array.isArray(this.state.processedIds) ? this.state.processedIds : [];
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      this.state = { settings: { ...EMPTY_SETTINGS }, entries: [], processedIds: [] };
      await this.save();
    }
    return this;
  }

  async save() {
    const snapshot = JSON.stringify(this.state, null, 2);
    const operation = this.writeQueue.catch(() => {}).then(async () => {
      const temporary = `${this.file}.${randomUUID()}.tmp`;
      await writeFile(temporary, snapshot, { mode: 0o600 });
      await rename(temporary, this.file);
    });
    this.writeQueue = operation;
    return operation;
  }

  async updateSettings(settings) {
    this.state.settings = settings;
    await this.save();
  }

  async addEntry(entry, messageId) {
    this.state.entries.unshift(entry);
    this.state.entries = this.state.entries.slice(0, 100);
    if (messageId) {
      this.state.processedIds.push(messageId);
      this.state.processedIds = this.state.processedIds.slice(-500);
    }
    await this.save();
  }

  hasProcessed(messageId) {
    return this.state.processedIds.includes(messageId);
  }

  async markProcessed(messageId) {
    if (this.hasProcessed(messageId)) return;
    this.state.processedIds.push(messageId);
    this.state.processedIds = this.state.processedIds.slice(-500);
    await this.save();
  }

  hasContactInSession(sender, sessionId) {
    return this.state.entries.some(
      (entry) =>
        entry.sessionId === sessionId &&
        entry.sender === sender &&
        ["draft", "sending", "sent", "send_failed"].includes(entry.status),
    );
  }
}
