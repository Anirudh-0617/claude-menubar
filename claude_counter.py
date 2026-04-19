#!/usr/bin/env python3
"""
Claude Counter — macOS Menu Bar App
====================================
Shows live token usage, session/weekly usage bars, cache timer,
and cost estimates directly in the macOS menu bar — works with
the Claude desktop app (Electron) and claude.ai in any browser.

Reads session cookies from Claude's Electron cookie store.
No app modification required. Fully local, no external servers.
"""

import rumps
import sqlite3
import threading
import time
import json
import math
import shutil
import tempfile
import os
import subprocess
import hashlib
from datetime import datetime, timezone
from pathlib import Path

try:
    from curl_cffi import requests
    _SESSION_KWARGS = {"impersonate": "chrome124"}
    HAS_CFFI = True
except ImportError:
    import requests
    _SESSION_KWARGS = {}
    HAS_CFFI = False

try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

try:
    import Security as _Sec
    HAS_SECURITY = True
except ImportError:
    HAS_SECURITY = False

# In-process cache so Keychain is only accessed once per run
_KEY_CACHE: bytes | None = None
_KEY_FETCHED = False

# Persistent key cache — survives restarts so Keychain is NEVER prompted again
_KEY_CACHE_FILE = Path.home() / ".claude_counter_key.bin"

# Debug log
_LOG_FILE = Path("/tmp/claude_counter.log")

# ── Constants ─────────────────────────────────────────────────────────────────
CONTEXT_LIMIT   = 200_000
CACHE_WINDOW_S  = 5 * 60        # 5-minute cache window
REFRESH_SECS    = 30            # poll interval (seconds)

WARN_PCT    = 75
DANGER_PCT  = 90

PRICING = {
    "haiku":   {"input": 0.80,  "output": 4.00},
    "sonnet":  {"input": 3.00,  "output": 15.00},
    "opus":    {"input": 15.00, "output": 75.00},
}

# Electron stores cookies in this SQLite DB
COOKIE_DB_PATHS = [
    Path.home() / "Library/Application Support/Claude/Cookies",
    Path.home() / "Library/Application Support/Claude/Default/Cookies",
    Path.home() / "Library/Application Support/claude-desktop/Cookies",
]

BAR_WIDTH = 14   # chars for text progress bar

# ── Helpers ───────────────────────────────────────────────────────────────────
def make_bar(pct: float, width: int = BAR_WIDTH) -> str:
    """Build a compact Unicode progress bar: ████░░░░ 42%"""
    filled = round(pct / 100 * width)
    bar    = "█" * filled + "░" * (width - filled)
    return f"{bar} {pct:.1f}%"

def fmt_seconds(secs: float) -> str:
    if secs <= 0:    return "now"
    secs = int(secs)
    if secs < 60:    return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:       return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    if h < 24:       return f"{h}h {m:02d}m"
    d, h = divmod(h, 24)
    return f"{d}d {h:02d}h"

def fmt_tokens(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}k"
    return str(n)

def estimate_cost(model: str, input_tok: int, output_tok: int) -> str:
    p = PRICING.get(model, PRICING["sonnet"])
    cost = (input_tok / 1e6) * p["input"] + (output_tok / 1e6) * p["output"]
    if cost < 0.0001: return "<$0.0001"
    return f"${cost:.4f}"

# ── Electron cookie decryption (macOS Keychain) ───────────────────────────────
def _log(msg: str):
    """Append a debug line to /tmp/claude_counter.log."""
    try:
        with open(_LOG_FILE, "a") as f:
            f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _get_keychain_password_keyring(service: str, account: str) -> bytes | None:
    """
    Use the 'keyring' library — talks directly to macOS Keychain
    via PyObjC, no subprocess, no repeated permission dialogs.
    """
    try:
        import keyring as _keyring
        pw = _keyring.get_password(service, account)
        if pw:
            _log(f"keyring OK: service={service}")
            return pw.encode("utf-8") if isinstance(pw, str) else pw
        _log(f"keyring returned None/empty for service={service}")
    except Exception as e:
        _log(f"keyring error: {e}")
    return None


def _get_keychain_password_pyobjc(service: str, account: str) -> bytes | None:
    """
    Use PyObjC Security framework directly — correct 8-argument call signature.
    Returns raw password bytes, or None.
    """
    if not HAS_SECURITY:
        return None
    try:
        # SecKeychainFindGenericPassword takes 8 args:
        # (keychainOrArray, svcLen, svc, acctLen, acct, pwLen_out, pwData_out, itemRef_out)
        # PyObjC returns (errStatus, pwLen, pwData, itemRef)
        status, length, data, _item = _Sec.SecKeychainFindGenericPassword(
            None,
            len(service), service,
            len(account), account,
            None, None, None
        )
        if status == 0 and data is not None:
            _log(f"PyObjC OK: service={service}")
            return bytes(data[:length])
        _log(f"PyObjC status={status} for service={service}")
    except Exception as e:
        _log(f"PyObjC error ({service}): {e}")
    return None


def _get_keychain_password_subprocess(service: str, account: str,
                                       timeout: int = 30) -> bytes | None:
    """
    Last resort: /usr/bin/security CLI.
    timeout=30 gives the user enough time to click 'Always Allow'.
    """
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=timeout
        )
        pw = result.stdout.strip()
        if result.returncode == 0 and pw:
            _log(f"subprocess OK: service={service}")
            return pw.encode("utf-8")
        _log(f"subprocess rc={result.returncode} empty={not pw} for service={service}")
    except subprocess.TimeoutExpired:
        _log(f"subprocess TIMEOUT for service={service}")
    except Exception as e:
        _log(f"subprocess error ({service}): {e}")
    return None


def _get_electron_key(app_name: str = "Claude") -> bytes | None:
    """
    Returns the AES-128 key for Electron cookie decryption.

    Strategy (in order):
      1. Disk cache (~/.claude_counter_key.bin) — zero prompts after first run
      2. keyring library  — native macOS Keychain, no subprocess
      3. PyObjC Security  — direct framework call, correct 8-arg signature
      4. /usr/bin/security subprocess — 30s timeout so user can click 'Always Allow'
      5. 'peanuts' fallback — only for unencrypted/dev profiles

    The derived key is cached to disk so Keychain is NEVER asked again.
    If a cached key produces wrong results (all decrypts fail), delete
    ~/.claude_counter_key.bin to force a fresh fetch.
    """
    global _KEY_CACHE, _KEY_FETCHED
    if _KEY_FETCHED:
        return _KEY_CACHE

    _KEY_FETCHED = True
    _log(f"_get_electron_key: HAS_SECURITY={HAS_SECURITY} HAS_CRYPTO={HAS_CRYPTO}")

    # ── 1. Disk cache (only if it was written by a successful Keychain fetch) ─
    if _KEY_CACHE_FILE.exists():
        try:
            cached = _KEY_CACHE_FILE.read_bytes()
            if len(cached) == 16:
                _KEY_CACHE = cached
                _log("Loaded key from disk cache — skipping Keychain")
                return _KEY_CACHE
        except Exception as e:
            _log(f"Disk cache read error: {e}")

    # ── 2–4. Try Keychain — only "Claude Safe Storage" ─────────────────────
    # (The correct service for Claude desktop on macOS)
    svc  = f"{app_name} Safe Storage"
    acct = app_name

    password = (
        _get_keychain_password_keyring(svc, acct)
        or _get_keychain_password_pyobjc(svc, acct)
        or _get_keychain_password_subprocess(svc, acct, timeout=30)
    )

    if not password:
        _log("WARNING: all Keychain methods failed — using 'peanuts' (decryption will likely fail)")
        password = b"peanuts"

    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)
    _KEY_CACHE = key

    # ── 5. Write to disk — but ONLY if we didn't fall back to peanuts ──────
    if password != b"peanuts":
        try:
            _KEY_CACHE_FILE.write_bytes(key)
            os.chmod(_KEY_CACHE_FILE, 0o600)
            _log("Key written to disk cache (Keychain will not be asked again)")
        except Exception as e:
            _log(f"Disk cache write error: {e}")
    else:
        _log("Skipping disk cache write (wrong key — would poison future runs)")

    return _KEY_CACHE


def _decrypt_cookie(encrypted_value: bytes, key: bytes) -> str | None:
    """
    Decrypt a v10-prefixed Electron/Chromium cookie value (AES-128-CBC).

    Electron prepends a fixed 12-byte header + 0x60 separator byte to all
    cookie values before encrypting them.  After decryption the plaintext
    looks like:  <12-byte-fixed-prefix> + "`" + <actual-value>
    We strip everything up to and including the first backtick (0x60) that
    appears within the first 20 bytes.  If no backtick is present (older
    Electron / already-clean values) we return the full plaintext.
    """
    if not HAS_CRYPTO:
        return None
    try:
        if len(encrypted_value) < 4 or encrypted_value[:3] != b"v10":
            return None
        ciphertext = encrypted_value[3:]
        if len(ciphertext) % 16 != 0:
            return None
        cipher = AES.new(key, AES.MODE_CBC, b" " * 16)
        raw = cipher.decrypt(ciphertext)
        pad = raw[-1]
        if pad < 1 or pad > 16:
            return None
        data = raw[:-pad]

        # ── Decode to string ──────────────────────────────────────────────────
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")

        # ── Strip Electron's fixed header (ends with backtick 0x60 = '`') ────
        # Observed: 12-byte prefix + '`' appear before every real value.
        # Only strip if the backtick is in the first 20 chars (avoids stripping
        # content from cookie values that legitimately contain backticks later).
        backtick_pos = text.find("`", 0, 40)
        if backtick_pos != -1:
            text = text[backtick_pos + 1:]

        # ── Ensure the result is latin-1 safe (required for HTTP cookies) ─────
        try:
            text.encode("latin-1")
            return text if text else None
        except UnicodeEncodeError:
            # Strip any remaining non-latin-1 chars
            return text.encode("latin-1", errors="ignore").decode("latin-1") or None

    except Exception:
        return None

# ── Cookie extraction (Electron SQLite) ───────────────────────────────────────
def get_claude_cookies() -> dict:
    """
    Read cookies from Claude's Electron Chromium cookie store.
    Handles both plaintext and AES-encrypted (v10) cookie values.
    """
    _key = _get_electron_key("Claude")

    _log(f"get_claude_cookies: key={'ok' if _key else 'NONE'} HAS_CRYPTO={HAS_CRYPTO}")
    for db_path in COOKIE_DB_PATHS:
        if not db_path.exists():
            _log(f"  DB not found: {db_path}")
            continue
        _log(f"  Trying DB: {db_path}")
        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            shutil.copy2(db_path, tmp.name)
            tmp.close()

            conn = sqlite3.connect(tmp.name)
            # First check what hosts are in the DB
            hosts = conn.execute("SELECT DISTINCT host_key FROM cookies LIMIT 20").fetchall()
            _log(f"  DB hosts: {[h[0] for h in hosts]}")

            cur  = conn.execute(
                "SELECT name, value, encrypted_value "
                "FROM cookies WHERE host_key LIKE '%claude%'"
            )
            cookies = {}
            for name, value, encrypted_value in cur.fetchall():
                if value:
                    cookies[name] = value
                    _log(f"  plaintext: {name}={value[:20]}...")
                elif encrypted_value and _key:
                    decrypted = _decrypt_cookie(encrypted_value, _key)
                    if decrypted:
                        cookies[name] = decrypted
                        _log(f"  decrypted: {name}={decrypted[:20]}...")
                    else:
                        _log(f"  decrypt FAILED: {name} (len={len(encrypted_value)})")
            conn.close()
            os.unlink(tmp.name)

            _log(f"  cookies found: {list(cookies.keys())}")
            if cookies:
                return cookies
        except Exception as e:
            _log(f"  DB error: {e}")
            continue
    _log("  No cookies found in any DB")
    return {}

def get_session_key(cookies: dict) -> str | None:
    """Extract the session key from cookies."""
    for name in ("sessionKey", "session_key", "__Secure-next-auth.session-token",
                 "CF_Authorization", "intercom-session"):
        if name in cookies:
            return cookies[name]
    return None

def get_org_id(cookies: dict) -> str | None:
    return cookies.get("lastActiveOrg")

# ── Claude API ────────────────────────────────────────────────────────────────
class ClaudeAPI:
    BASE = "https://claude.ai/api"

    def __init__(self):
        # curl_cffi impersonates Chrome's TLS fingerprint → bypasses Cloudflare
        self._session = requests.Session(**_SESSION_KWARGS)
        if not HAS_CFFI:
            # Fallback headers for plain requests (may be Cloudflare-blocked)
            self._session.headers.update({
                "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/124.0.0.0 Safari/537.36",
                "Accept":          "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer":         "https://claude.ai/",
                "Origin":          "https://claude.ai",
                "sec-fetch-dest":  "empty",
                "sec-fetch-mode":  "cors",
                "sec-fetch-site":  "same-origin",
            })
        _log(f"ClaudeAPI init: HAS_CFFI={HAS_CFFI}")

    @staticmethod
    def _sanitize(v: str) -> str:
        """Strip characters that are invalid in HTTP Cookie header values."""
        import re
        return re.sub(r'[\x00-\x1f\x7f\s;,\\"]', '', str(v))

    def set_cookies(self, cookies: dict):
        self._session.cookies.clear()
        for k, v in cookies.items():
            self._session.cookies.set(k, self._sanitize(v), domain="claude.ai")

    def get_usage(self, org_id: str) -> dict | None:
        try:
            r = self._session.get(
                f"{self.BASE}/organizations/{org_id}/usage",
                timeout=10
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def get_recent_conversation(self, org_id: str) -> dict | None:
        """Fetch the most recently active conversation."""
        try:
            r = self._session.get(
                f"{self.BASE}/organizations/{org_id}/chat_conversations?limit=1",
                timeout=10
            )
            if r.status_code == 200:
                convs = r.json()
                if convs:
                    conv_id = convs[0].get("uuid") or convs[0].get("id")
                    if conv_id:
                        return self.get_conversation(org_id, conv_id)
        except Exception:
            pass
        return None

    def get_conversation(self, org_id: str, conv_id: str) -> dict | None:
        try:
            r = self._session.get(
                f"{self.BASE}/organizations/{org_id}/chat_conversations/{conv_id}"
                "?rendering_mode=messages&render_all_tools=true",
                timeout=15
            )
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None


# ── Token estimation ──────────────────────────────────────────────────────────
def count_tokens(text: str) -> int:
    """Word-boundary heuristic: ~4 chars/token × 1.05 overhead."""
    if not text:
        return 0
    words = text.split()
    tokens = sum(max(1, math.ceil(len(w) / 4)) for w in words)
    return math.ceil(tokens * 1.05)

def extract_message_text(msg: dict) -> str:
    parts = []
    for item in msg.get("content", []):
        if not isinstance(item, dict): continue
        t = item.get("type", "")
        if t in ("thinking", "redacted_thinking", "image", "document"): continue
        if t == "text":     parts.append(item.get("text", ""))
        elif t == "tool_use":
            parts.append(json.dumps({"name": item.get("name"), "input": item.get("input")}))
        elif t == "tool_result":
            parts.append(json.dumps({"content": item.get("content")}))
    for att in msg.get("attachments", []):
        if isinstance(att, dict) and att.get("extracted_content"):
            parts.append(att["extracted_content"])
    return "\n".join(parts)

def compute_conversation_tokens(conv: dict) -> dict:
    """Walk the active branch and sum tokens."""
    msgs  = conv.get("chat_messages", [])
    by_id = {m["uuid"]: m for m in msgs if "uuid" in m}
    leaf  = conv.get("current_leaf_message_uuid")
    root  = "00000000-0000-4000-8000-000000000000"

    trunk = []
    cur   = leaf
    while cur and cur != root:
        m = by_id.get(cur)
        if not m: break
        trunk.append(m)
        cur = m.get("parent_message_uuid")
    trunk.reverse()

    total = input_tok = output_tok = 0
    last_asst_ms = None

    for m in trunk:
        tok = count_tokens(extract_message_text(m))
        total += tok
        if m.get("sender") == "human":     input_tok  += tok
        elif m.get("sender") == "assistant":
            output_tok += tok
            ts = m.get("created_at")
            if ts:
                try:
                    ms = int(datetime.fromisoformat(
                        ts.replace("Z", "+00:00")).timestamp() * 1000)
                    if last_asst_ms is None or ms > last_asst_ms:
                        last_asst_ms = ms
                except Exception:
                    pass

    cached_until = (last_asst_ms + CACHE_WINDOW_S * 1000) if last_asst_ms else None
    return {
        "total":       total,
        "input":       input_tok,
        "output":      output_tok,
        "cached_until": cached_until,
        "messages":    len(trunk),
    }


# ── State ─────────────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.lock     = threading.Lock()
        self.cookies  = {}
        self.org_id   = None
        self.usage    = {}          # {five_hour, seven_day}
        self.tokens   = {}          # {total, input, output, cached_until}
        self.model    = "sonnet"
        self.error    = None
        self.last_ok  = None


# ── Menu bar app ──────────────────────────────────────────────────────────────
class ClaudeCounterApp(rumps.App):
    def __init__(self):
        super().__init__(
            "🪙",
            title="🪙 —",
            quit_button="Quit Claude Counter"
        )
        self.state = State()
        self.api   = ClaudeAPI()

        # ── Menu items ─────────────────────────────────────────────────────
        self.lbl_tokens   = rumps.MenuItem("Tokens: loading…")
        self.lbl_cache    = rumps.MenuItem("Cache: —")
        self.lbl_cost     = rumps.MenuItem("Est. cost: —")
        self.sep1         = rumps.separator

        self.lbl_session  = rumps.MenuItem("Session: loading…")
        self.bar_session  = rumps.MenuItem("  —")
        self.lbl_session_reset = rumps.MenuItem("  Resets: —")

        self.sep2         = rumps.separator

        self.lbl_weekly   = rumps.MenuItem("Weekly: loading…")
        self.bar_weekly   = rumps.MenuItem("  —")
        self.lbl_weekly_reset = rumps.MenuItem("  Resets: —")

        self.sep3         = rumps.separator
        self.btn_refresh  = rumps.MenuItem("Refresh Now", callback=self._on_refresh)
        self.lbl_status   = rumps.MenuItem("Status: starting…")

        self.menu = [
            self.lbl_tokens,
            self.lbl_cache,
            self.lbl_cost,
            rumps.separator,
            self.lbl_session,
            self.bar_session,
            self.lbl_session_reset,
            rumps.separator,
            self.lbl_weekly,
            self.bar_weekly,
            self.lbl_weekly_reset,
            rumps.separator,
            self.btn_refresh,
            self.lbl_status,
        ]

        # Start background thread
        self._stop = threading.Event()
        t = threading.Thread(target=self._bg_loop, daemon=True)
        t.start()

        # 1-second tick timer for live countdowns
        rumps.Timer(self._tick, 1).start()

    # ── Background polling ──────────────────────────────────────────────────
    def _bg_loop(self):
        while not self._stop.is_set():
            self._refresh()
            self._stop.wait(REFRESH_SECS)

    def _on_refresh(self, _):
        threading.Thread(target=self._refresh, daemon=True).start()

    def _refresh(self):
        try:
            # 1. Get cookies
            cookies = get_claude_cookies()
            org_id  = get_org_id(cookies)

            if not cookies:
                with self.state.lock:
                    self.state.error = "No cookies found. Open Claude app + log in.\nSee /tmp/claude_counter.log"
                return
            if not org_id:
                with self.state.lock:
                    self.state.error = f"No org ID. Cookies: {list(cookies.keys())[:3]}"
                return

            self.api.set_cookies(cookies)

            with self.state.lock:
                self.state.cookies = cookies
                self.state.org_id  = org_id
                self.state.error   = None

            # 2. Fetch usage
            usage_raw = self.api.get_usage(org_id)
            if usage_raw:
                with self.state.lock:
                    self.state.usage    = usage_raw
                    self.state.last_ok  = time.time()

            # 3. Fetch recent conversation tokens
            conv = self.api.get_recent_conversation(org_id)
            if conv:
                tok = compute_conversation_tokens(conv)
                # Detect model from conv metadata
                model_raw = (conv.get("model") or "").lower()
                if "haiku"  in model_raw: model = "haiku"
                elif "opus" in model_raw: model = "opus"
                else:                     model = "sonnet"
                with self.state.lock:
                    self.state.tokens = tok
                    self.state.model  = model

        except Exception as e:
            with self.state.lock:
                self.state.error = f"Error: {e}"

    # ── 1-second tick — update menu labels ─────────────────────────────────
    def _tick(self, _):
        with self.state.lock:
            error   = self.state.error
            usage   = self.state.usage
            tok     = self.state.tokens
            model   = self.state.model
            last_ok = self.state.last_ok

        now_ms = int(time.time() * 1000)
        now_s  = time.time()

        if error:
            self.title              = "🪙 !"
            self.lbl_status.title   = f"⚠ {error}"
            self.lbl_tokens.title   = "Tokens: —"
            return

        if last_ok:
            age = int(now_s - last_ok)
            self.lbl_status.title = f"Last updated {fmt_seconds(age)} ago · every {REFRESH_SECS}s"

        # ── Token pill (menu bar title) ─────────────────────────────────
        total = tok.get("total")
        if total is not None:
            pct = total / CONTEXT_LIMIT * 100
            ring = "🔴" if pct >= DANGER_PCT else ("🟡" if pct >= WARN_PCT else "🟢")
            self.title = f"🪙 {fmt_tokens(total)} {ring}"

            self.lbl_tokens.title = (
                f"Tokens: ~{total:,} / {CONTEXT_LIMIT:,}  ({pct:.1f}%)"
            )

            inp = tok.get("input", 0)
            out = tok.get("output", 0)
            if inp or out:
                self.lbl_tokens.title += f"\n  ↑ {fmt_tokens(inp)} in  ↓ {fmt_tokens(out)} out"

            cost = estimate_cost(model, inp, out)
            self.lbl_cost.title = f"Est. cost ({model.capitalize()}): {cost}"
        else:
            self.title            = "🪙 —"
            self.lbl_tokens.title = "Tokens: loading…"
            self.lbl_cost.title   = "Est. cost: —"

        # ── Cache countdown ─────────────────────────────────────────────
        cached_until = tok.get("cached_until")
        if cached_until:
            rem_s = (cached_until - now_ms) / 1000
            if rem_s > 0:
                self.lbl_cache.title = f"Cache: ⚡ {fmt_seconds(rem_s)} remaining"
            else:
                self.lbl_cache.title = "Cache: expired"
        else:
            self.lbl_cache.title = "Cache: —"

        # ── Session (5h) ────────────────────────────────────────────────
        fh = usage.get("five_hour") or {}
        fh_pct = fh.get("utilization", 0)
        self.lbl_session.title = f"Session (5h)  {fh_pct:.1f}%"
        self.bar_session.title = f"  {make_bar(fh_pct)}"
        if fh.get("resets_at"):
            try:
                reset_ts = datetime.fromisoformat(
                    fh["resets_at"].replace("Z", "+00:00")).timestamp()
                rem = reset_ts - now_s
                self.lbl_session_reset.title = f"  Resets in {fmt_seconds(rem)}"
            except Exception:
                self.lbl_session_reset.title = "  Resets: unknown"
        else:
            self.lbl_session_reset.title = "  Resets: —"

        # ── Weekly (7d) ─────────────────────────────────────────────────
        sd = usage.get("seven_day") or {}
        sd_pct = sd.get("utilization", 0)
        self.lbl_weekly.title = f"Weekly (7d)   {sd_pct:.1f}%"
        self.bar_weekly.title = f"  {make_bar(sd_pct)}"
        if sd.get("resets_at"):
            try:
                reset_ts = datetime.fromisoformat(
                    sd["resets_at"].replace("Z", "+00:00")).timestamp()
                rem = reset_ts - now_s
                self.lbl_weekly_reset.title = f"  Resets in {fmt_seconds(rem)}"
            except Exception:
                self.lbl_weekly_reset.title = "  Resets: unknown"
        else:
            self.lbl_weekly_reset.title = "  Resets: —"


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ClaudeCounterApp().run()
