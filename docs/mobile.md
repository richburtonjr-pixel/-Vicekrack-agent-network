# ViceKrack Mobile Command Center

A separate phone app for ViceKrack. The desktop Living HQ (and its 3D headquarters) stays exactly as it is.

## How it fits together (the two-building picture)

- **HQ (main building)**: `python -m vicekrack hq-serve --studio`, on `127.0.0.1:8765`. Unchanged, still
  refuses every address except the PC's own.
- **Mobile Command Center (building next door)**: `vicekrack/mobile/`, its own small server on
  `127.0.0.1:8770`. It owns pairing, the PIN, the daily cap, the action log, and (from PR 2) the phone screens.
- **The private hallway**: the mobile server calls the HQ's existing Studio API the same way the HQ page does
  (loopback Host and Origin, HQ session cookie, CSRF token). Every HQ rule still applies: exact consent per
  paid item, one action at a time, repeated request IDs answered once. No HQ code was changed.
- **The front door**: Tailscale Serve gives the PC a private `https://<pc>.<tailnet>.ts.net` address that only
  devices signed in to your Tailscale account can reach, and forwards it to `127.0.0.1:8770`. Nothing is
  opened to the public internet; Tailscale Funnel is never used.

## Safety layers

| Layer | What it does |
| --- | --- |
| Tailscale | Only your own devices can reach the address; traffic is encrypted. |
| Owner check | Requests through Tailscale must carry your Tailscale login (Serve sets this header itself and removes any copy a client sends). Remote access stays off until setup saves your address and login. |
| Host / Origin | Only this app's own addresses are accepted (blocks other websites and DNS rebinding). |
| Pairing | A 6-digit code shown on the PC (10 minutes, 5 tries, works once) pairs a phone. The phone gets an HttpOnly, Secure, SameSite=Strict cookie; only its SHA-256 is saved. Phones expire after 90 days; unpair from the phone or with `mobile-unpair-all`. |
| CSRF | Every phone POST carries a header token derived from the cookie, which other sites cannot read. |
| PIN | Every paid action needs your PIN (salted scrypt hash on the PC). 5 wrong PINs lock paid actions for 15 minutes. |
| Daily cap | Paid phone approvals reserve the HQ's own estimate (high end) against a daily limit (default $5, your time zone) **before** anything is sent. A repeated request never counts twice. If the HQ refuses before sending, the amount is given back; if the result is uncertain it stays counted. |
| PC-only actions | Retrying a request that may already be billed, and narration purchases (no estimate yet), can only be done on the PC. A paid action without an HQ estimate is refused. |
| Audit log | `runtime/mobile/audit.jsonl`: time, phone, action, IDs, estimate, outcome. Never a PIN, token, cookie, key or consent text. |
| xAI key | Saved in Windows Credential Manager (encrypted for your Windows login) and handed only to the HQ process at start. Never written to a file, printed, or sent to the phone. |

The cap counts estimates of phone-approved actions; actual xAI charges are on your xAI usage page.
Approvals made on the PC are not limited by the phone cap.

## One-time setup (PC)

All of these are double-click files in `scripts/windows/`:

1. Install **Tailscale for Windows** and sign in.
2. **`1 Set up ViceKrack Mobile.bat`**: saves your xAI key, sets your PIN and daily limit, finds your private
   Tailscale address and turns on the private front door. If Tailscale shows a link to enable HTTPS
   certificates, open it, click Enable, and run the file again.
3. **`Turn on auto-start.bat`**: ViceKrack starts by itself (minimized) when you sign in to Windows.
   Also set Windows power to never sleep while plugged in.
4. **`Start ViceKrack (PC + Phone).bat`**: starts the HQ (Video Studio, real mode, with the saved key) and the
   mobile server together, and restarts the HQ if it stops. If the HQ is already running, it uses that one.

## iPhone

1. Install **Tailscale** from the App Store and sign in with the same account. Keep it connected.
2. In Safari, open the `https://….ts.net` address that setup showed.
3. On the PC, double-click **`2 Pair my iPhone.bat`** and type the code on the phone.
4. Share › **Add to Home Screen**. (Lock-screen alerts, coming in a later PR, need iOS 16.4 or newer and the
   app opened from the Home Screen.)

## Commands (for reference)

```
python -m vicekrack mobile-setup | mobile-save-key | mobile-pair | mobile-start [--demo] | mobile-serve
python -m vicekrack mobile-phones | mobile-unpair-all | mobile-status
```

## Phone API (PR 1)

- `GET /m/api/status`: `{paired:false}` until paired; then PC, HQ mode, key present, spend vs cap, PIN set.
- `GET /m/api/session`: the paired phone and its CSRF token.
- `POST /m/api/pair` `{code, name}` · `POST /m/api/unpair`.
- `GET /m/api/hq/<overview|productions|production|workflow|research/jobs|research/job>?…`: HQ Studio reads.
- `POST /m/api/action/<research/prepare|research/submit|research/recover|research/make-video|workflow/start|
  workflow/resume|workflow/submit|workflow/submit-all|workflow/retry-scene|workflow/export|review/record>`
  with the HQ's body plus `request_id`, and `pin` for paid actions.

## Roadmap

1. PR 1 (merged): separate mobile server, private connection, pairing, PIN, $5 cap, audit log, saved key, auto-start.
2. **PR 2 (this)**: installable app (Add to Home Screen, blue VK icon, opens full-screen), bottom tabs, Home
   (PC/HQ status, waiting on you with estimates, running/finished/problems counts, live progress, recent
   activity, refreshes every 20 s) and System (PC, HQ, key, connection, spending, PIN, paired phones, recent
   phone actions). Research, Studio, Agents and Alerts show what's coming. `GET /m/api/home` and
   `GET /m/api/system` build these from HQ Studio reads and the mobile records only; when the HQ can't be read
   the app says so instead of showing numbers. The service worker stores only the app's own files, never data.
3. PR 3: Research and Video Studio screens (script review, approvals, video playback, downloads).
4. PR 4: Agents screen (real status only).
5. PR 5: Alerts: failures and finished projects, in the app and on the lock screen.
6. PR 6: System Control safe retries.
