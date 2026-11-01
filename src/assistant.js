const MONEY_PATTERN =
  /\b(?:money|cash|payment|payments?|paid|paying|pay|transfer|lend|borrow|owe|repay|upi|gpay|google\s*pay|phonepe|paytm|venmo|zelle|paypal|cash\s*app|bank\s+transfer|account\s+number|send\s+me|refund|invoice|bill|loan|debt|credit\s+card|debit\s+card)\b|(?:₹|\$|€|£)\s*\d|\b\d[\d,]*(?:\.\d{1,2})?\s*(?:inr|usd|rs\.?|rupees?|dollars?|euros?|pounds?)\b/i;
const URGENT_PATTERN =
  /\b(?:urgent|emergency|hospital|accident|help|ambulance|injured|bleeding|fire|police)\b/i;

export function classifyMessage(text = "") {
  const content = String(text);
  if (MONEY_PATTERN.test(content)) return "money";
  if (URGENT_PATTERN.test(content)) return "urgent";
  return "normal";
}

export function isQuietTime(now, quietHours, timeZone) {
  if (!quietHours || quietHours.start === "off" || quietHours.end === "off") return false;
  const [sh, sm] = quietHours.start.split(":").map(Number);
  const [eh, em] = quietHours.end.split(":").map(Number);
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(now);
  const current = Number(parts.find((part) => part.type === "hour").value) * 60 +
    Number(parts.find((part) => part.type === "minute").value);
  const start = sh * 60 + sm;
  const end = eh * 60 + em;
  return start === end ? true : start < end ? current >= start && current < end : current >= start || current < end;
}

function isVip(sender, name, vipFriends) {
  const senderKey = String(sender).replace(/\D/g, "");
  const nameKey = String(name || "").trim().toLocaleLowerCase();
  return vipFriends.some((friend) => {
    const value = friend.trim();
    if (!value) return false;
    const digits = value.replace(/\D/g, "");
    if (digits.length >= 5 && senderKey.endsWith(digits)) return true;
    return value.toLocaleLowerCase() === nameKey && nameKey.length > 0;
  });
}

export function makeReply({ category, settings, sender, name }) {
  if (category === "urgent") {
    return `This sounds urgent. Please call ${settings.urgentContact} if it can't wait.`;
  }
  if (settings.reason === "driving") {
    return "I'm on the road right now, so I can't reply. Thanks for understanding!";
  }
  if (isVip(sender, name, settings.vipFriends)) {
    return "Hey, I'm caught up with something at the moment. If it's urgent, ring me.";
  }
  if (settings.returnAt.trim()) {
    return `Hey, I'm tied up until ${settings.returnAt.trim()}. Thanks for your patience!`;
  }
  return "Hey! I'm a bit busy right now. I've seen your message, and thanks for understanding!";
}

export function createDecision({ message, settings, now = new Date() }) {
  const category = classifyMessage(message.text);
  if (message.isGroup) return { action: "skip", reason: "group", category };
  if (category === "money") return { action: "manual", reason: "money", category };
  if (message.unclassifiable) return { action: "manual", reason: "unclassified", category: "unclassified" };
  if (isQuietTime(now, settings.quietHours, settings.timeZone)) {
    return { action: "skip", reason: "quiet_hours", category };
  }
  return {
    action: "draft",
    reason: category,
    category,
    reply: makeReply({ category, settings, sender: message.from, name: message.name }),
  };
}

export function validateSettings(settings) {
  const errors = [];
  const reasons = ["busy", "driving", "in a meeting", "resting", "travelling"];
  if (!settings || !reasons.includes(settings.reason)) errors.push("Choose an away reason.");
  if (!String(settings?.returnAt || "").trim()) errors.push("Add a return time or day.");
  if (!String(settings?.urgentContact || "").trim()) errors.push("Add an urgent contact.");
  if (!Array.isArray(settings?.vipFriends) || settings.vipFriends.length === 0) {
    errors.push("Add at least one VIP friend, or enter 'none'.");
  }
  const quiet = settings?.quietHours;
  const isOff = quiet?.start === "off" && quiet?.end === "off";
  const validTime = (value) => /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
  if (!String(settings?.timeZone || "").trim()) {
    errors.push("Set a valid time zone.");
  } else {
    try {
      new Intl.DateTimeFormat("en-US", { timeZone: settings.timeZone });
    } catch {
      errors.push("Set a valid time zone.");
    }
  }
  if (!isOff && (!validTime(quiet?.start) || !validTime(quiet?.end))) {
    errors.push("Set both quiet-hour times (24-hour HH:MM), or set both to 'off'.");
  }
  return errors;
}
