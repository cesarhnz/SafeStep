# SafeStep UI

A two-page caregiver experience built with React, TypeScript, Tailwind CSS, shadcn/ui (Radix), and Lucide. Home provides monitoring status, a local simulation, notification templates, recent records, and the configured Databricks dashboard. About explains the mission, four-step response, local processing, and technology.

## Run SafeStep locally

From the repository root:

```sh
npm --prefix ui install
npm --prefix ui run build
python3 ui/server.py
```

Open **http://127.0.0.1:8081** on the device running SafeStep. The gateway serves the production build and read-only `/api/dashboard` endpoint. Both `/` and `/about` support direct navigation and refresh. Python uses only its standard library.

To let caregivers open the dashboard from other devices on the same trusted home Wi-Fi, start it with `python3 ui/server.py --host 0.0.0.0`. Find the host computer or UNO Q's private Wi-Fi address, then open `http://<device-lan-address>:8081` on the caregiver device. Allow port 8081 on the private home network if the operating system asks. Keep the device and dashboard on the trusted home network; the UI has no user login. The default loopback setting keeps it accessible only from the host device.

The UI works without a running alert service and clearly labels its demo state. To show current monitoring, run the existing alert service and detector as described in the root README. Restart the alert service after updating this checkout so it exposes the new authenticated `/monitoring` endpoint.

## Development

Run `python3 ui/server.py` in one terminal, and `npm --prefix ui run dev` in another. Open the Local URL printed by Vite (normally http://127.0.0.1:5173). Vite forwards `/api` to the gateway on port 8081. For home-network development access, start Vite with its `--host 0.0.0.0` option too.

## Existing backend integration

The gateway reads `alerts/config.json` on the device, contacts the alert service's authenticated `/incidents` and `/monitoring` endpoints, and reads its append-only lifecycle log. API keys, private ntfy topics, Databricks tokens, and caregiver email addresses are never included in the web response. Local hosting lets this UI read the event history and current alert state directly from the device that runs SafeStep.

Optional server environment variables:

| Variable | Purpose |
| --- | --- |
| `SAFESTEP_ALERT_URL` | Alert service origin; defaults to localhost using its configured port. |
| `SAFESTEP_ALERT_API_KEY` | Overrides the key from the local alert configuration. |
| `SAFESTEP_EVENT_LOG` | Absolute path to lifecycle JSONL records. |
| `SAFESTEP_DASHBOARD_URL` | Direct HTTPS Databricks dashboard URL. Otherwise uses `analytics/reports/config.json` → `dashboard_url`. |

The Databricks button links directly to the configured dashboard, opening a new tab. If no valid dashboard URL is configured, the button is disabled with an explanation. No fallback links or invented dashboard URLs are used.

### Trustworthy states

- A healthy alert API is insufficient for **All clear**: all known detectors must have fresh, running heartbeats and no open fall/help incidents. An acknowledged fall stays visible until closed.
- An unavailable backend shows demo mode; previously fetched event history is preserved with a connection warning. The gateway can also display available historical records while the service is down.
- UI sample and simulation events live only in React memory. Reset removes the current simulation, cancels its remaining transitions, and restores the real monitoring state. They never call the alert service, send messages, or write to Databricks.
- Backend records explicitly marked `source: "demo"`, `is_demo: true`, or using the existing `demo-` simulator/seed IDs retain a Demo label. Backend demo records remain distinct from UI previews: their actual notification status is shown in event details.
- The **Caregiver notified** and **Databricks updated** demo steps are explicitly labeled **Simulated**. The app does not infer live analytics ingestion from an alert record.
- The notification and email cards are **previews only**. This app does not send per-fall emails; the existing service sends ntfy alerts and the existing Databricks jobs handle their configured report emails.
- **Alert service accepted** means the provider accepted a notification, never that a caregiver received/read it. Failed, pending, dry-run, and unknown transport states are separate. Legacy delivery rows without an explicit dry-run marker remain unknown; new rows persist the marker.
- No camera view, video player, upload, or video stream is included.

## Checks

```sh
npm --prefix ui run build
npm --prefix ui test
python3 -m unittest discover -s ui/tests -v
python3 -m unittest discover -s alerts/tests -v
```

Google Fonts enhance the typography when internet access is available; local system and Georgia fallbacks keep the app usable without it. Reduced motion, keyboard focus, a skip link, semantic navigation, and accessible Radix dialogs are supported.
