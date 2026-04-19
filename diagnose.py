#!/usr/bin/env python3
"""
Quick diagnostic — run this from the claude-menubar folder:
  source venv/bin/activate && python3 diagnose.py
"""
import sys, os, sqlite3, shutil, tempfile, hashlib, json, math, re
from pathlib import Path

# ── AES decrypt (same as main app) ───────────────────────────────────────────
try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False
    print("✗ pycryptodome not installed")

def _decrypt(encrypted_value, key):
    if not HAS_CRYPTO or len(encrypted_value) < 4 or encrypted_value[:3] != b"v10":
        return None
    try:
        ct = encrypted_value[3:]
        if len(ct) % 16 != 0:
            return None
        cipher = AES.new(key, AES.MODE_CBC, b" " * 16)
        raw = cipher.decrypt(ct)
        pad = raw[-1]
        if pad < 1 or pad > 16:
            return None
        data = raw[:-pad]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        # Strip Electron's fixed header (ends with backtick 0x60 within first 40 chars)
        pos = text.find("`", 0, 40)
        if pos != -1:
            text = text[pos + 1:]
        try:
            text.encode("latin-1")
            return text if text else None
        except UnicodeEncodeError:
            return text.encode("latin-1", errors="ignore").decode("latin-1") or None
    except Exception as e:
        return None

def sanitize_cookie(v):
    """Strip characters invalid in HTTP Cookie header values."""
    return re.sub(r'[\x00-\x1f\x7f;,\\"]', '', str(v))

# ── Get key via keyring ───────────────────────────────────────────────────────
password = None
print("\n── Keychain ─────────────────────────────────────")
try:
    import keyring
    pw = keyring.get_password("Claude Safe Storage", "Claude")
    if pw:
        password = pw.encode("utf-8")
        print(f"✓ keyring: got password (len={len(password)})")
    else:
        print("✗ keyring: returned None")
except Exception as e:
    print(f"✗ keyring error: {e}")

if not password:
    import subprocess
    try:
        r = subprocess.run(["/usr/bin/security", "find-generic-password",
                            "-s", "Claude Safe Storage", "-a", "Claude", "-w"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            password = r.stdout.strip().encode("utf-8")
            print(f"✓ subprocess: got password (len={len(password)})")
    except Exception as e:
        print(f"✗ error: {e}")

if not password:
    password = b"peanuts"

key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)
print(f"  Derived key: {key.hex()}")

# ── Read cookies ──────────────────────────────────────────────────────────────
print("\n── Cookies ──────────────────────────────────────")
db_path = Path.home() / "Library/Application Support/Claude/Cookies"
if not db_path.exists():
    print(f"✗ Cookie DB not found: {db_path}")
    sys.exit(1)

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
shutil.copy2(db_path, tmp.name); tmp.close()
conn = sqlite3.connect(tmp.name)

cookies = {}
for name, value, enc in conn.execute(
        "SELECT name, value, encrypted_value FROM cookies WHERE host_key LIKE '%claude%'"):
    if value:
        cookies[name] = value
    elif enc:
        d = _decrypt(enc, key)
        if d:
            cookies[name] = d
conn.close(); os.unlink(tmp.name)

org_id   = cookies.get("lastActiveOrg")
sess_key = cookies.get("sessionKey")

print(f"  lastActiveOrg = {org_id!r}")
print(f"  sessionKey    = {(sess_key or '')[:60]!r}...")

# Show raw cf_clearance before any processing
cf_raw = cookies.get("cf_clearance", "")
print(f"  cf_clearance  = {cf_raw[:80]!r}...")

if not org_id or not sess_key:
    print("\n✗ Missing org_id or sessionKey")
    sys.exit(1)

# ── Build sanitized cookie dict ───────────────────────────────────────────────
# Only include cookies that are clean ASCII (skip anything with garbage)
ESSENTIAL = {"sessionKey", "lastActiveOrg", "activitySessionId",
             "anthropic-device-id", "routingHint", "anthropic-consent-preferences",
             "__ssid", "cf_clearance", "__cf_bm"}

clean_cookies = {}
for k, v in cookies.items():
    sv = sanitize_cookie(v)
    # Only include if value looks like clean ASCII (no garbled bytes)
    try:
        sv.encode("ascii")
        clean_cookies[k] = sv
    except UnicodeEncodeError:
        print(f"  Skipping non-ASCII cookie: {k}")

print(f"\n  Sending {len(clean_cookies)} clean cookies")

# ── Try curl_cffi (Chrome TLS impersonation — bypasses Cloudflare JA3 checks) ─
print("\n── curl_cffi (Chrome impersonation) ─────────────")
try:
    from curl_cffi import requests as cffi_req
    print("  ✓ curl_cffi available")

    cffi_session = cffi_req.Session(impersonate="chrome124")
    for k, v in clean_cookies.items():
        cffi_session.cookies.set(k, v, domain="claude.ai")

    BASE = "https://claude.ai/api"
    url = f"{BASE}/organizations/{org_id}/usage"
    print(f"  GET {url}")
    r = cffi_session.get(url, timeout=15)
    print(f"  Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"  ✓ SUCCESS!")
        print(f"  five_hour utilization: {data.get('five_hour', {}).get('utilization', 'N/A')}")
        print(f"  seven_day utilization: {data.get('seven_day', {}).get('utilization', 'N/A')}")
    else:
        print(f"  Response: {r.text[:300]}")

except ImportError:
    print("  ✗ curl_cffi not installed — installing now...")
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "curl_cffi", "--quiet"])
    print("  Installed! Re-run diagnose.py to test.")

# ── Fallback: plain requests (likely Cloudflare blocked) ─────────────────────
print("\n── requests fallback ────────────────────────────")
import requests as req

session = req.Session()
session.headers.update({
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://claude.ai/",
    "Origin":          "https://claude.ai",
    "sec-ch-ua":       '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest":  "empty",
    "sec-fetch-mode":  "cors",
    "sec-fetch-site":  "same-origin",
})
for k, v in clean_cookies.items():
    session.cookies.set(k, v, domain="claude.ai")

BASE = "https://claude.ai/api"
url = f"{BASE}/organizations/{org_id}/usage"
try:
    r = session.get(url, timeout=10)
    print(f"  Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"  ✓ SUCCESS! five_hour={data.get('five_hour',{}).get('utilization','N/A')}")
    else:
        print(f"  (Cloudflare likely blocking plain requests — use curl_cffi above)")
except Exception as e:
    print(f"  Error: {e}")

print("\n✓ Diagnostic complete")
