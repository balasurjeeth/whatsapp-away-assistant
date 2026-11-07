const $ = (selector) => document.querySelector(selector);
const form = $("#settingsForm");
let state;

function showNotice(message) {
  const notice = $("#notice");
  notice.textContent = message;
  notice.hidden = false;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function render() {
  const { settings, entries, connected } = state;
  const timeZone = settings.timeZone || Intl.DateTimeFormat().resolvedOptions().timeZone;
  $("#timeZone").value = timeZone;
  $("#timeZoneHint").textContent = `Your local time (${timeZone}); set both to off to disable`;
  $("#connectionText").textContent = connected ? "Meta credentials configured" : "Not connected to WhatsApp";
  $(".connection").classList.toggle("connected", connected);
  $("#reason").value = settings.reason;
  $("#returnAt").value = settings.returnAt;
  $("#urgentContact").value = settings.urgentContact;
  $("#vipFriends").value = settings.vipFriends.join(", ");
  $("#quietStart").value = settings.quietHours.start;
  $("#quietEnd").value = settings.quietHours.end;
  document.querySelector(`input[name="mode"][value="${settings.mode}"]`).checked = true;
  $("#confirmRow").hidden = settings.mode !== "automatic";
  $("#activateButton").textContent = settings.active ? "Turn away mode off" : "Switch away mode on";
  if (settings.active) $("#activateButton").textContent = "I'm free now · turn off";
  $("#modeTitle").textContent = settings.active
    ? settings.mode === "automatic" ? "Automatic replies are on" : "Draft & approve is on"
    : "Draft mode is ready";
  $("#modeDescription").textContent = settings.active
    ? settings.mode === "automatic" ? "Eligible VIP replies send automatically. New numbers stay in drafts; money and payment messages wait for you." : "Replies are prepared for your approval; nothing sends without your say-so."
    : connected ? "Set your details, then switch on when you need a little space." : "Set up drafts here; connect an official WhatsApp Business account to receive messages.";
  $("#entryCount").textContent = entries.length;
  $("#entries").innerHTML = entries.length ? entries.map(renderEntry).join("") : '<p class="empty-state">Your recent activity will show up here.</p>';
  renderReminders(entries);
  $("#notificationButton").hidden = !("Notification" in window) || Notification.permission === "granted";
  $("#entries").querySelectorAll("[data-approve]").forEach((button) => {
    button.addEventListener("click", () => approve(button.dataset.approve));
  });
  $("#reminders").querySelectorAll("[data-follow-up]").forEach((button) => {
    button.addEventListener("click", () => completeFollowUp(button.dataset.followUp));
  });
}

function renderReminders(entries) {
  const reminders = entries.filter((entry) => entry.followUp === "pending");
  $("#reminderPanel").hidden = reminders.length === 0;
  $("#reminderCount").textContent = reminders.length;
  $("#reminders").innerHTML = reminders.map((entry) => {
    const category = entry.mediaKind === "voice_note" ? "Voice note to listen to" :
      entry.category === "money" ? "Payment to handle" : entry.category === "urgent" ? "Urgent message" : "Message";
    return `<article class="entry"><div class="entry-top"><span class="entry-name">${escapeHtml(entry.name)}</span><time class="entry-time">${new Date(entry.createdAt).toLocaleString()}</time></div><div class="entry-actions"><span class="entry-status">${escapeHtml(category)} · follow up</span><button class="button button-outline" data-follow-up="${escapeHtml(entry.id)}">Mark handled</button></div></article>`;
  }).join("");
}

function renderEntry(entry) {
  const categoryLabel = entry.contactKnown === false
    ? entry.category === "urgent" ? "New number · urgent" : "New or unrecognized number"
    : entry.mediaKind === "voice_note" ? "Voice note · listen later" :
    entry.category === "money" ? "Payment — handle personally" :
    entry.category === "urgent" ? "Urgent" : entry.category === "normal" ? "Friend" : entry.category;
  const statusLabel = entry.contactKnown === false && entry.status === "draft"
    ? "Draft · your approval needed"
    : entry.mediaKind === "voice_note" && entry.status === "manual"
    ? "No reply · listen and follow up"
    : ({
    draft: "Draft · not sent",
    sent: "Sent",
    sending: "Sending…",
    send_failed: "Not sent · check connection",
    manual: "No reply · handle personally",
    ignored: entry.reason === "group" ? "Group · no reply" : entry.reason === "quiet_hours" ? "Quiet hours · no reply" : entry.reason === "already_replied" ? "Already handled this session" : "Away mode was off",
  }[entry.status] || entry.status);
  const button = entry.status === "draft" && state.settings.active && state.connected &&
    (state.settings.mode === "approval" || entry.contactKnown === false)
    ? `<button class="button button-outline" data-approve="${escapeHtml(entry.id)}">Approve & send</button>`
    : "";
  return `<article class="entry"><div class="entry-top"><span class="entry-name">${escapeHtml(entry.name)}</span><time class="entry-time">${new Date(entry.createdAt).toLocaleString()}</time></div><div class="entry-meta"><span class="tag ${entry.category === "money" ? "warn" : ""}">${escapeHtml(categoryLabel)}</span></div>${entry.reply ? `<p class="entry-reply">${escapeHtml(entry.reply)}</p>` : ""}<div class="entry-actions"><span class="entry-status">${escapeHtml(statusLabel)}</span>${button}</div></article>`;
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "content-type": "application/json", ...options.headers },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.errors?.join(" ") || data.error || "Something went wrong.");
  return data;
}

async function refresh() {
  state = await api("/api/state");
  render();
}

form.addEventListener("change", (event) => {
  if (event.target.name === "mode") $("#confirmRow").hidden = event.target.value !== "automatic";
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const settings = {
    ...state.settings,
    reason: $("#reason").value,
    returnAt: $("#returnAt").value,
    urgentContact: $("#urgentContact").value,
    timeZone: $("#timeZone").value,
    vipFriends: $("#vipFriends").value.split(",").map((value) => value.trim()).filter(Boolean),
    quietHours: { start: $("#quietStart").value.trim() || "off", end: $("#quietEnd").value.trim() || "off" },
    mode: $('input[name="mode"]:checked').value,
    active: state.settings.active,
    confirmAutomatic: $("#confirmAutomatic").checked,
  };
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify(settings) });
    $("#saveState").textContent = "Saved";
    await refresh();
  } catch (error) {
    showNotice(error.message);
  }
});

$("#activateButton").addEventListener("click", async () => {
  const active = !state.settings.active;
  if (active && state.settings.mode === "automatic" && !$("#confirmAutomatic").checked) {
    showNotice("Confirm automatic sending before switching it on.");
    return;
  }
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        ...state.settings,
        active,
        mode: $('input[name="mode"]:checked').value,
        confirmAutomatic: $("#confirmAutomatic").checked,
      }),
    });
    $("#notice").hidden = true;
    await refresh();
    if (!active) notifyFreeTimeReminders();
  } catch (error) {
    showNotice(error.message);
  }
});

function notifyFreeTimeReminders() {
  const count = state.entries.filter((entry) => entry.followUp === "pending").length;
  if (!count) {
    showNotice("Away mode is off. You're all caught up!");
    return;
  }
  const message = `You have ${count} message${count === 1 ? "" : "s"} to check or follow up on.`;
  showNotice(message);
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification("You're back", { body: message });
  }
}

$("#notificationButton").addEventListener("click", async () => {
  if (!("Notification" in window)) {
    showNotice("Browser notifications aren't supported in this browser.");
    return;
  }
  const permission = await Notification.requestPermission();
  $("#notificationButton").hidden = permission === "granted";
  if (permission === "granted") {
    notifyFreeTimeReminders();
  } else {
    showNotice("Browser notifications were not enabled. In-app reminders will still be available.");
  }
});

$("#simulateForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/simulate", {
      method: "POST",
      body: JSON.stringify({
        name: $("#sampleName").value,
        text: $("#sampleText").value,
        isGroup: $("#sampleGroup").checked,
      }),
    });
    $("#sampleText").value = "";
    await refresh();
  } catch (error) {
    showNotice(error.message);
  }
});

async function approve(id) {
  try {
    await api(`/api/entries/${id}/approve`, { method: "POST", body: "{}" });
    await refresh();
  } catch (error) {
    showNotice(error.message);
  }
}

async function completeFollowUp(id) {
  try {
    await api(`/api/entries/${id}/follow-up/done`, { method: "POST", body: "{}" });
    await refresh();
  } catch (error) {
    showNotice(error.message);
  }
}

refresh().catch((error) => showNotice(error.message));
