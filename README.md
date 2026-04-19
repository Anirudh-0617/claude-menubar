# Claude Counter — macOS Menu Bar App 🪙

A native macOS menu bar app that shows your live Claude token usage, session/weekly limits, and estimated cost — works directly with the **Claude desktop app**, no browser required.

```
🪙 14.2k 🟢        ← lives in your macOS menu bar

Tokens: ~14,200 / 200,000  (7.1%)
  ↑ 9.8k in  ↓ 4.4k out
Cache: ⚡ 3m 42s remaining
Est. cost (Sonnet): $0.0042

Session (5h)   27.0%   Resets in 3h 43m
Weekly  (7d)   25.0%   Resets in 6d 6h

Refresh Now
Last updated now · every 30s
```

---

## Requirements

- macOS 12 or later (Intel or Apple Silicon)
- Python 3.9+  — check with `python3 --version`
- [Claude desktop app](https://claude.ai/download) installed and signed in at least once

---

## Installation

```bash
git clone https://github.com/Anirudh-0617/claude-menubar.git
cd claude-menubar
chmod +x install.sh
./install.sh
open "/Applications/Claude Counter.app"
```

> **First launch — two things to expect:**
>
> **1. Gatekeeper warning** — macOS will block the app since it's not App Store signed.
> Go to **System Settings → Privacy & Security** → click **"Open Anyway"**,
> or right-click the `.app` → **Open**.
>
> **2. Keychain prompt** — the app asks for your macOS password once to read Claude's
> encryption key from Keychain. This is normal and only happens once — the key is
> cached to disk after the first read.

---

## Updating

```bash
cd claude-menubar
./rebuild.sh
```

Kills any running instance, rebuilds the `.app`, reinstalls to `/Applications`, relaunches.

---

## Troubleshooting

If the icon shows `🌑 !` or doesn't update:

```bash
cd claude-menubar
source venv/bin/activate
python3 diagnose.py
```

Or watch the live log:
```bash
tail -f /tmp/claude_counter.log
```

`diagnose.py` tests each step independently: Keychain → cookie decryption → API call → response.

---

## How It Works

```
Menu Bar App
  │
  ├── Reads ~/Library/Application Support/Claude/Cookies (SQLite)
  │         AES-128-CBC decrypt with key from macOS Keychain
  │         (PBKDF2-SHA1, salt="saltysalt", 1003 iterations)
  │
  ├── HTTP via curl_cffi — impersonates Chrome124 TLS fingerprint
  │         Bypasses Cloudflare (plain requests always returns 403)
  │
  ├── GET /api/organizations/{org}/usage
  │         → five_hour + seven_day utilization %
  │
  └── GET /api/organizations/{org}/chat_conversations/{id}
            → message tree → token count heuristic
```

**Why `curl_cffi`?** Python's `requests` has a different TLS fingerprint from Chrome. Cloudflare detects this and blocks with 403. `curl_cffi` impersonates Chrome's exact TLS handshake — Cloudflare can't tell the difference.

---

## Files

| File | Purpose |
|------|---------|
| `claude_counter.py` | Main app — menu bar UI, cookie decrypt, API polling |
| `requirements.txt` | Python dependencies |
| `setup.py` | py2app config — builds standalone `.app` bundle |
| `install.sh` | First-time setup (venv + build + install to /Applications) |
| `rebuild.sh` | Update & reinstall after any code changes |
| `diagnose.py` | Step-by-step debug tool |

---

## Privacy

Fully local — reads your own cookies from your own Mac and calls `claude.ai` directly with your session. No external servers, no analytics, no tracking.

---

## Also Check Out

The **Chrome extension** version that overlays token info inside claude.ai:
👉 [github.com/Anirudh-0617/claude-counter-mac](https://github.com/Anirudh-0617/claude-counter-mac)

---

## Credits

Inspired by [claude-counter](https://github.com/she-llac/claude-counter) by she-llac.
Built by [Anirudh-0617](https://github.com/Anirudh-0617).

## License

MIT
