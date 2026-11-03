import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MONEY_PATTERN = re.compile(
    r"\b(?:money|cash|payments?|paid|paying|pay|transfer|lend|borrow|owe|repay|upi|gpay|google\s*pay|phonepe|paytm|venmo|zelle|paypal|cash\s*app|bank\s+transfer|account\s+number|send\s+me|refund|invoice|bill|loan|debt|credit\s+card|debit\s+card)\b"
    r"|(?:₹|\$|€|£)\s*\d"
    r"|\b\d[\d,]*(?:\.\d{1,2})?\s*(?:inr|usd|rs\.?|rupees?|dollars?|euros?|pounds?)\b",
    re.IGNORECASE,
)
URGENT_PATTERN = re.compile(
    r"\b(?:urgent|emergency|hospital|accident|help|ambulance|injured|bleeding|fire|police)\b",
    re.IGNORECASE,
)
REASONS = {"busy", "driving", "in a meeting", "resting", "travelling"}
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def classify_message(text=""):
    content = str(text)
    if MONEY_PATTERN.search(content):
        return "money"
    if URGENT_PATTERN.search(content):
        return "urgent"
    return "normal"


def is_quiet_time(now, quiet_hours, time_zone):
    if not quiet_hours or quiet_hours.get("start") == "off" or quiet_hours.get("end") == "off":
        return False
    try:
        local_now = now.astimezone(ZoneInfo(time_zone))
    except (ZoneInfoNotFoundError, ValueError):
        return True
    current = local_now.hour * 60 + local_now.minute
    start_hour, start_minute = map(int, quiet_hours["start"].split(":"))
    end_hour, end_minute = map(int, quiet_hours["end"].split(":"))
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _is_vip(sender, name, vip_friends):
    sender_digits = re.sub(r"\D", "", str(sender))
    normalized_name = str(name or "").strip().casefold()
    for friend in vip_friends:
        value = str(friend).strip()
        digits = re.sub(r"\D", "", value)
        if len(digits) >= 5 and sender_digits.endswith(digits):
            return True
        if normalized_name and value.casefold() == normalized_name:
            return True
    return False


def make_reply(category, settings, sender="", name=""):
    if category == "urgent":
        return f"This sounds urgent. Please call {settings['urgentContact']} if it can't wait. 📞"
    if settings["reason"] == "driving":
        return "I'm on the road right now, so I can't reply. Thanks for understanding! 🚗"
    if _is_vip(sender, name, settings["vipFriends"]):
        return "Hey, I'm caught up with something at the moment. If it's urgent, ring me. 💛"
    if settings["returnAt"].strip():
        return f"Hey, I'm tied up until {settings['returnAt'].strip()}. Thanks for your patience! 🙂"
    return "Hey! I'm a bit busy right now. I've seen your message, and thanks for understanding! 🙂"


def create_decision(message, settings, now=None):
    category = classify_message(message.get("text", ""))
    if message.get("isGroup"):
        return {"action": "skip", "reason": "group", "category": category}
    if category == "money":
        return {"action": "manual", "reason": "money", "category": category}
    if message.get("unclassifiable"):
        return {"action": "manual", "reason": "unclassified", "category": "unclassified"}
    if is_quiet_time(now or datetime.now().astimezone(), settings["quietHours"], settings["timeZone"]):
        return {"action": "skip", "reason": "quiet_hours", "category": category}
    return {
        "action": "draft",
        "reason": category,
        "category": category,
        "reply": make_reply(category, settings, message.get("from", ""), message.get("name", "")),
    }


def validate_settings(settings):
    errors = []
    if settings.get("reason") not in REASONS:
        errors.append("Choose an away reason.")
    if not str(settings.get("returnAt", "")).strip():
        errors.append("Add a return time or day.")
    if not str(settings.get("urgentContact", "")).strip():
        errors.append("Add an urgent contact.")
    if not isinstance(settings.get("vipFriends"), list) or not settings["vipFriends"]:
        errors.append("Add at least one VIP friend, or enter 'none'.")

    quiet = settings.get("quietHours") or {}
    is_off = quiet.get("start") == "off" and quiet.get("end") == "off"
    if not is_off and not (
        TIME_PATTERN.fullmatch(str(quiet.get("start", "")))
        and TIME_PATTERN.fullmatch(str(quiet.get("end", "")))
    ):
        errors.append("Set both quiet-hour times (24-hour HH:MM), or set both to 'off'.")

    time_zone = str(settings.get("timeZone", "")).strip()
    if not time_zone:
        errors.append("Set a valid time zone.")
    else:
        try:
            ZoneInfo(time_zone)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append("Set a valid time zone.")
    return errors
