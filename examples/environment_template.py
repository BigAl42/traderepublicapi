import getpass4 as getpass

# Trade Republic Login
NUMBER = "+491731234567"
PIN = ""

# Default to English if not specified when calling the API
LOCALE = "en"
CURRENCY = "EUR"

# Optional: path to the paired device PEM key (legacy auth='device' only)
# KEY_FILE = os.environ.get("TR_KEY_FILE", "key")

# Default login is web v2: confirm the push in the Trade Republic app.
# Cookies are stored in tr_cookies.txt so later runs can resume.

if not PIN:
    PIN = getpass.getpass("Pin:")