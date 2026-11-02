# WhatsApp Away Assistant

A small, privacy-minded away assistant for friends. The backend is Python and uses deterministic rules (not an external AI service) to prepare short replies, route payment/UPI messages for personal handling, skip groups, observe quiet hours, and prevent repeat drafts or replies in one away session. The existing HTML, CSS, and browser JavaScript remain the dashboard presentation layer; Python handles the application logic, storage, HTTP API, webhook verification, and official Cloud API integration.

**Safe defaults:** The app starts inactive in **Draft & approve** mode. Drafts are not sent. Approving one sends only after Meta Cloud API credentials are configured and away mode is explicitly activated. Automatic sending is an optional, separately confirmed mode and is rejected unless all required credentials are present. No credentials or live WhatsApp connection are included.

## Requirements and local development

- Python 3.9 or later
- `tzdata` (installed from `requirements.txt`; needed for IANA time zones on Windows and other systems without system time-zone data)
- A Meta developer app with WhatsApp Business Platform Cloud API access for real webhook/send integration. A personal WhatsApp account is not an integration target.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m unittest discover -s test -v
python app.py
```

Open `http://127.0.0.1:3000`. Add all away settings, save, then switch away mode on. “Try a sample message” exercises classifications locally; no message is sent. Local mode binds only to loopback and needs no password. On macOS/Linux, activate the environment with `source .venv/bin/activate`.

## Settings and behavior

- Away reason, return time/day, urgent contact, VIP names or phone numbers, and quiet hours are required before activation. Use `none` for no VIP friends and `off` for both quiet-hour fields to disable quiet hours.
- Times use 24-hour time in the browser's detected IANA time zone, saved with settings so deployments in a different server zone still honor the user's quiet hours. Quiet intervals can span midnight.
- Payment/money/UPI detection runs before urgent detection; matching messages are logged for personal handling and never receive an automated reply.
- Non-text media that the rules cannot safely classify is also left for personal handling rather than receiving an automatic reply.
- “urgent”, “emergency”, “hospital”, “accident”, and “help” (plus related terms) select a short reply asking the sender to call the configured urgent contact. The app does not claim to notify that person.
- Group chats and quiet-hour messages are not replied to. The WhatsApp Business Platform's supported webhook payloads are generally for direct business conversations; group detection remains a defensive guard for providers that supply a group marker.
- One reply or draft per sender per away session prevents repeat replies. Turning away mode off ends the session; turning it on starts a new one.
- Messages from different friends are processed concurrently, including overlapping Cloud API requests. Each sender is handled in order so simultaneous messages from one friend cannot cause duplicate replies.
- When you switch away mode off with **I'm free now**, the dashboard creates follow-up reminders for messages received during that away session. Mark each handled when you're done. Grant browser notification permission from the reminder panel to receive a one-time notification then; reminders remain in the dashboard if browser notifications are unavailable.
- Reply wording avoids promising a callback or exposing location/plans. The driving reply only says you cannot reply right now.

The application stores settings and a capped recent activity log in `DATA_DIR/assistant.json` (default `./data`). Keep that file private and back it up only if appropriate. It does not store inbound message text or webhook bodies. `APP_PASSWORD` protects the dashboard/API with HTTP Basic authentication; use HTTPS in front of it. Do not expose a public deployment without setting a strong password.

## Official Meta / WhatsApp setup

1. Create a Meta developer app and add the WhatsApp product. Set up a WhatsApp Business Account and a phone number you control; complete any required business verification and access review.
2. In Meta's WhatsApp API setup, obtain a long-lived/system-user access token and the phone number ID. Keep the token secret; never put it in browser code or commit it.
3. Configure the environment variables from `.env.example`: `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, and `WHATSAPP_API_VERSION` (a currently supported Graph API version for your Meta app). The app secret validates `X-Hub-Signature-256`; the verify token is a private string you choose and enter in both Meta and this app.
4. Deploy behind HTTPS and set Meta's webhook callback URL to `https://your-host/webhook`. Subscribe the app to the `messages` webhook field. The GET verification route checks `hub.verify_token`; POST events must have a valid Meta signature.
5. Add all required away settings in the dashboard. For review-first use, stay in **Draft & approve** and activate; approve individual drafts to send. Automatic mode requires the separate confirmation checkbox and configured credentials, and sends eligible messages without review.
6. First test with Meta's test number and a few consenting friends. Confirm tone, money/urgent edge cases, quiet hours, webhook delivery, and the sender's current WhatsApp messaging permissions before enabling automatic replies.

Meta/provider rules, message templates, the customer-service window, phone-number eligibility, rate limits, regional availability, and costs can change. Check current Meta policies and your BSP/provider's fees and terms before launch. The app does not bypass those requirements, create a WhatsApp account, or guarantee delivery. It sends ordinary text messages through the official Cloud API only; messages outside Meta's allowed messaging window may be rejected.

## Deployment

Run `python app.py` under a process manager or container with a persistent private `DATA_DIR`. Set `HOST=0.0.0.0`, a strong `APP_PASSWORD`, and all required Meta secrets in the hosting platform's secret manager. Put the app behind HTTPS, configure the public webhook URL in Meta, and restrict access to the dashboard/API. Do not bake secrets into images or source. Install Python dependencies with `python -m pip install -r requirements.txt`.

## Later: business replies

The MVP is friends-only; it does not classify customers or provide business reply templates. To extend it later, retain the same safety and delivery engine but add a separate, explicitly selected business profile with its own customer contacts and replies for bookings, prices, hours, and service-status questions. Keep friend and customer lists and tones separate; do not infer a business profile from message text.
