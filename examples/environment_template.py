import getpass4 as getpass

# Trade Republic Login
NUMBER = "+491731234567"
PIN = ""

# Default to English if not specified when calling the API
LOCALE = "en"
CURRENCY = "EUR"

# Optional: path to the paired device PEM key (default: ./key)
# KEY_FILE = os.environ.get("TR_KEY_FILE", "key")

if not PIN:
    PIN = getpass.getpass("Pin:")