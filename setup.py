"""
py2app setup — builds Claude Counter as a standalone .app bundle.
Run: python setup.py py2app
"""
from setuptools import setup

APP      = ["claude_counter.py"]
DATA     = []
OPTIONS  = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName":             "Claude Counter",
        "CFBundleDisplayName":      "Claude Counter",
        "CFBundleIdentifier":       "com.anirudh.claude-counter",
        "CFBundleVersion":          "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement":              True,        # menu-bar only, no Dock icon
        "NSAppleEventsUsageDescription": "Claude Counter reads usage from the Claude app.",
        "NSHumanReadableCopyright": "© 2026 Anirudh Annaboina",
    },
    "packages":  ["rumps", "requests", "urllib3", "certifi", "charset_normalizer",
                  "curl_cffi", "Security", "keyring"],
    "includes":  ["sqlite3", "shutil", "tempfile", "threading", "json", "math", "subprocess"],
    "iconfile":  "AppIcon.icns",
}

setup(
    app     = APP,
    name    = "Claude Counter",
    data_files = DATA,
    options = {"py2app": OPTIONS},
    setup_requires = ["py2app"],
)
