from ecdsa import NIST256p, SigningKey
from ecdsa.util import sigencode_der
import base64
import hashlib
import time
import requests
import asyncio
import websockets
from deprecated import deprecated

import os
import platform
import uuid
from datetime import datetime
from http.cookiejar import MozillaCookieJar
from pathlib import Path

import json


# Web frontend identity. Bump APP_VERSION when TR rejects login with CLIENT_VERSION_OUTDATED.
APP_VERSION = os.environ.get("TR_APP_VERSION", "2.2631.13")
WEB_PLATFORM = os.environ.get("TR_PLATFORM", "web-pro")
WEB_USER_AGENT = os.environ.get(
    "TR_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
)
WS_CONNECT_ID_WEB = 31
WS_CONNECT_ID_APP = 21

LOGIN_ERRORS = {
    "PROCESS_GONE": "The login request expired. Please start again.",
    "ALREADY_PROCESSED": "The login request was rejected or has already been used.",
    "NOT_FOUND": "Trade Republic does not know this login request.",
    "TOO_MANY_REQUESTS": "Too many attempts. Please wait before trying again.",
    "VALIDATION_CODE_INVALID": "That authenticator code is not correct.",
    "VALIDATION_CODE_ALREADY_USED": "That authenticator code was already used.",
    "NUMBER_INVALID": "The phone number is not a Trade Republic account.",
    "CLIENT_VERSION_OUTDATED": "This client version is rejected. Update APP_VERSION / headers.",
}


class TRapiException(Exception):
    pass


class TRapiExcServerErrorState(TRapiException):
    pass


class TRapiExcServerUnknownState(TRapiException):
    pass


class TRApi:
    url = "https://api.traderepublic.com"

    def __init__(self, number, pin, locale='en', key_file=None, auth="web", cookies_file=None):
        self.number = number
        self.pin = pin
        self.locale = locale
        self.auth = auth
        self.key_file = key_file or os.environ.get("TR_KEY_FILE", "key")
        self.cookies_file = Path(cookies_file or os.environ.get("TR_COOKIES_FILE", "tr_cookies.txt"))
        self.signing_key = None
        self.ws = None
        self.sessionToken = None
        self.refreshToken = None
        self.sec_acc_no = None
        self._process_id = None
        self._required_action = None
        self._device_info = None
        self._session_expires_at = 0
        self.mu = asyncio.Lock()
        self.started = False

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": WEB_USER_AGENT})
        jar = MozillaCookieJar(str(self.cookies_file))
        if self.cookies_file.is_file():
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, ValueError):
                pass
        self.session.cookies = jar

        types = ["cash", "portfolio", "availableCash"]

        self.dict = {str(k): str(v) for v, k in enumerate(types)}

        self.callbacks = {}

        self.latest_response = {}

    def load_cookies_from_disk(self) -> bool:
        """Reload Mozilla cookie jar from disk (e.g. after offline check_login)."""
        if not self.cookies_file.is_file():
            return False
        jar = MozillaCookieJar(str(self.cookies_file))
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, ValueError):
            return False
        self.session.cookies = jar
        cookie = next(
            (c.value for c in self.session.cookies if c.name == "tr_session"),
            None,
        )
        if cookie:
            self.sessionToken = cookie
            return True
        return False

    def session_needs_refresh(self, *, skew_seconds: float = 45.0) -> bool:
        """True when a soft web-session refresh should run before the next query."""
        expires = float(getattr(self, "_session_expires_at", 0) or 0)
        if expires <= 0:
            return False
        return time.time() >= (expires - skew_seconds)

    def _stable_device_id(self):
        seed = "|".join(
            [str(uuid.getnode()), platform.node(), platform.machine(), platform.system()]
        )
        return hashlib.sha512(seed.encode()).hexdigest()

    def _timezone_name(self):
        try:
            return str(Path("/etc/localtime").resolve()).split("zoneinfo/")[1]
        except (OSError, IndexError):
            return "Etc/UTC"

    def _login_headers(self):
        if self._device_info is None:
            chrome = None
            ua = self.session.headers.get("User-Agent", "")
            if "Chrome/" in ua:
                chrome = ua.split("Chrome/", 1)[1].split(" ")[0]
            offset = datetime.now().astimezone().utcoffset()
            device_name = os.environ.get(
                "TR_DEVICE_NAME",
                f"{platform.node() or 'Unknown'} (Hermes MCP)",
            )
            device = {
                "stableDeviceId": self._stable_device_id(),
                "deviceName": device_name,
                "browser": "Chrome",
                "browserVersion": chrome or "",
                "os": platform.system(),
                "osVersion": platform.release(),
                "timezone": self._timezone_name(),
                "timezoneOffset": -int(offset.total_seconds() // 60) if offset else 0,
                "screen": "1920x1080x24",
                "preferredLanguages": [self.locale],
                "numberOfCores": os.cpu_count() or 1,
            }
            self._device_info = base64.b64encode(json.dumps(device).encode()).decode()
        return {
            "X-TR-Device-Info": self._device_info,
            "X-TR-App-Version": APP_VERSION,
            "X-Tr-Platform": WEB_PLATFORM,
            "Accept-Language": self.locale,
        }

    def _raise_login_error(self, response):
        if response.status_code < 400:
            return
        try:
            code = response.json()["errors"][0]["errorCode"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise TRapiException(
                f"Login failed with status {response.status_code}: {response.text[:300]}"
            )
        raise TRapiException(LOGIN_ERRORS.get(code, f"Login failed: {code}."))

    def _save_cookies(self):
        try:
            self.cookies_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cookies_file.with_suffix(self.cookies_file.suffix + ".tmp")
            jar = self.session.cookies
            previous = getattr(jar, "filename", str(self.cookies_file))
            jar.filename = str(tmp)
            try:
                jar.save(ignore_discard=True, ignore_expires=True)
            finally:
                jar.filename = previous
            tmp.replace(self.cookies_file)
            try:
                os.chmod(self.cookies_file, 0o600)
            except OSError:
                pass
            jar.filename = str(self.cookies_file)
        except OSError:
            pass

    def _refresh_web_session(self):
        r = self.session.get(f"{self.url}/api/v1/auth/web/session", timeout=20)
        if r.status_code < 400:
            self._session_expires_at = time.time() + 290
        return r

    def refresh_account_settings(self):
        """Load /api/v2/auth/account and cache securitiesAccountNumber."""
        self._refresh_web_session()
        r = self.session.get(
            f"{self.url}/api/v2/auth/account",
            headers=self._login_headers(),
            timeout=20,
        )
        if r.status_code >= 400:
            return None
        data = r.json()
        self.sec_acc_no = data.get("securitiesAccountNumber") or self.sec_acc_no
        return data

    def _has_tr_session_cookie(self):
        return any(c.name == "tr_session" for c in self.session.cookies)

    def clear_tr_session_cookie(self) -> None:
        """Drop in-memory tr_session so WebSocket can reconnect anonymously.

        Expired cookies are otherwise attached to every ``wss://`` connect and
        cause HTTP 401 even for public subscriptions (charts, search, news).
        Does not delete the cookie file on disk.
        """
        try:
            self.session.cookies.clear(domain=".traderepublic.com", path="/", name="tr_session")
        except (KeyError, TypeError, AttributeError):
            expired = [c for c in list(self.session.cookies) if c.name == "tr_session"]
            for cookie in expired:
                try:
                    self.session.cookies.clear(
                        domain=cookie.domain, path=cookie.path, name=cookie.name
                    )
                except (KeyError, TypeError, AttributeError):
                    pass
        self.sessionToken = None
        self._session_expires_at = 0

    def _resume_web_session(self):
        """Resume using cookie jar and/or already-injected in-memory tr_session.

        Does not require the cookie file to exist when a tr_session cookie is
        already present in the requests session (e.g. TR_TOKEN injection).
        """
        if not self.cookies_file.is_file() and not self._has_tr_session_cookie():
            return False
        data = self.refresh_account_settings()
        if data is None:
            self.clear_tr_session_cookie()
            return False
        cookie = next(
            (c.value for c in self.session.cookies if c.name == "tr_session"),
            None,
        )
        self.sessionToken = cookie
        return True

    async def reset_transport(self):
        """Close websocket and clear one-shot receive state so queries can retry."""
        async with self.mu:
            self.started = False
            self.callbacks = {}
            self.latest_response = {}
            ws = self.ws
            self.ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    def reset_transport_sync(self):
        """Best-effort sync reset when no awaitable context is available.

        Must not call ``run_until_complete`` / ``asyncio.run`` and must not
        ``create_task`` on a foreign/running loop — that races reconnects and
        surfaces as ``RuntimeError: This event loop is already running`` under
        FastMCP. Async callers must use ``await reset_transport()`` instead.
        """
        self.started = False
        self.callbacks = {}
        self.latest_response = {}
        self.ws = None

    def register_new_device(self, processId=None):
        self.signing_key = SigningKey.generate(curve=NIST256p, hashfunc=hashlib.sha512)
        if processId is None:
            r = requests.post(
                f"{self.url}/api/v1/auth/account/reset/device",
                json={"phoneNumber": self.number, "pin": self.pin},
            )

            bFailed = False
            try:
                processId = r.json()["processId"]
            except KeyError:
                bFailed = True

            if bFailed:
                raise TRapiException(f"Cannot Login! Details: {r.text}")
            else:
                print(f"*** The process id is: {processId}")

        pubkey = base64.b64encode(
            self.signing_key.get_verifying_key().to_string("uncompressed")
        ).decode("ascii")

        token = input("Enter your token: ")

        r = requests.post(
            f"{self.url}/api/v1/auth/account/reset/device/{processId}/key",
            json={"code": token, "deviceKey": pubkey},
        )

        if r.status_code == 200:
            key = self.signing_key.to_pem()
            with open(self.key_file, "wb") as f:
                f.write(key)

            return key
        raise TRapiException(f"Device registration failed: {r.status_code} {r.text}")

    def _login_device(self, **kwargs):
        res = None
        if os.path.isfile(self.key_file):
            res = self.do_request(
                "/api/v1/auth/login",
                payload={"phoneNumber": self.number, "pin": self.pin},
            )

        if res is None or (
            res.status_code == 401
            and not kwargs.get("already_tried_registering", False)
        ):
            self.register_new_device()
            res = self._login_device(already_tried_registering=True)

        if res.status_code != 200:
            raise TRapiException(
                f"Device login failed ({res.status_code}). "
                "The app ECDSA login is outdated; use auth='web' (default)."
            )

        data = res.json()
        self.refreshToken = data["refreshToken"]
        self.sessionToken = data["sessionToken"]

        if data["accountState"] != "ACTIVE":
            raise TRapiException("Account not active")

        return res

    def _weblogin_process(self):
        r = self.session.get(
            f"{self.url}/api/v2/auth/web/login/processes/{self._process_id}",
            headers=self._login_headers(),
            timeout=20,
        )
        self._raise_login_error(r)
        return r.json()

    def _await_web_confirmation(self, timeout=120):
        process = self._weblogin_process()
        deadline = time.time() + timeout
        expires = process.get("expiresAt")
        try:
            if isinstance(expires, (int, float)):
                deadline = expires / 1000 if expires > 1e11 else float(expires)
        except (TypeError, ValueError):
            pass
        print("Confirm this login in the Trade Republic app (push notification)...")
        while True:
            status = process.get("status")
            if status in ("CONFIRMED", "COMPLETED"):
                print("Login confirmed.")
                return
            if status not in (None, "PENDING"):
                raise TRapiException(f"Unexpected login process status: {status!r}")
            if time.time() >= deadline:
                raise TRapiException("The login was not confirmed in time.")
            time.sleep(2)
            process = self._weblogin_process()

    def _complete_authenticator(self, code):
        r = self.session.post(
            f"{self.url}/api/v2/auth/web/login/processes/{self._process_id}/authenticator-verification",
            json={"code": code},
            headers=self._login_headers(),
            timeout=20,
        )
        self._raise_login_error(r)

    def start_web_login(self) -> dict:
        """Start web login (push / authenticator). Does not wait for confirmation.

        Returns a dict with process_id, status, required_action, and raw process.
        """
        r = self.session.post(
            f"{self.url}/api/v2/auth/web/login",
            json={"phoneNumber": self.number, "pin": self.pin},
            headers=self._login_headers(),
            timeout=20,
        )
        self._raise_login_error(r)
        data = r.json()
        self._process_id = data.get("processId")
        if not self._process_id:
            raise TRapiException(f"Web login did not return processId: {data}")

        process = self._weblogin_process()
        self._required_action = process.get("requiredAction")
        return {
            "process_id": self._process_id,
            "status": process.get("status"),
            "required_action": self._required_action,
            "expires_at": process.get("expiresAt"),
            "process": process,
        }

    def poll_web_login(self) -> dict:
        """Poll an in-flight web login process started by start_web_login()."""
        if not self._process_id:
            raise TRapiException("No web login process in progress.")
        process = self._weblogin_process()
        self._required_action = process.get("requiredAction") or self._required_action
        return {
            "process_id": self._process_id,
            "status": process.get("status"),
            "required_action": self._required_action,
            "expires_at": process.get("expiresAt"),
            "process": process,
        }

    def complete_web_login_authenticator(self, code: str) -> dict:
        """Submit authenticator code for a pending web login process."""
        if not code or not str(code).strip():
            raise TRapiException("Authenticator code is required.")
        self._complete_authenticator(str(code).strip())
        return self.poll_web_login()

    def finalize_web_login(self) -> bool:
        """Persist cookies after the login process is CONFIRMED/COMPLETED."""
        if not self._process_id:
            raise TRapiException("No web login process in progress.")
        self._save_cookies()
        self._refresh_web_session()
        data = self.refresh_account_settings()
        if data is None:
            return False
        cookie = next(
            (c.value for c in self.session.cookies if c.name == "tr_session"),
            None,
        )
        self.sessionToken = cookie
        self._process_id = None
        self._required_action = None
        return bool(cookie)

    def _login_web(self, **kwargs):
        if kwargs.get("resume", True) and self._resume_web_session():
            print("Resumed saved Trade Republic web session.")
            return True

        started = self.start_web_login()
        if started.get("required_action") == "AUTHENTICATOR_VERIFICATION":
            code = kwargs.get("authenticator_code") or os.environ.get("TR_AUTHENTICATOR_CODE")
            if not code:
                code = input("Authenticator code: ")
            self.complete_web_login_authenticator(code)
        else:
            self._await_web_confirmation(timeout=kwargs.get("login_timeout", 120))

        if not self.finalize_web_login():
            raise TRapiException("Web login finished but session cookies were not established.")
        return True

    def login(self, **kwargs):
        """Log in. Default is current web login (v2 push confirm in the app).

        auth='web' keeps the phone app logged in.
        auth='device' is the legacy ECDSA pairing path (usually CLIENT_VERSION_OUTDATED).
        """
        if self.auth == "device":
            return self._login_device(**kwargs)
        return self._login_web(**kwargs)

    def _ws_cookie_header(self):
        parts = [
            f"{cookie.name}={cookie.value}"
            for cookie in self.session.cookies
            if getattr(cookie, "domain", "").endswith("traderepublic.com")
        ]
        if not parts:
            return None
        return {"Cookie": "; ".join(parts)}

    async def _ensure_ws(self):
        if self.ws is not None:
            return
        extra_headers = self._ws_cookie_header()
        connect_kwargs = {}
        if extra_headers:
            connect_kwargs["additional_headers"] = extra_headers
        self.ws = await websockets.connect("wss://api.traderepublic.com", **connect_kwargs)
        if self.auth == "device":
            msg = json.dumps({"locale": self.locale})
            connect_id = WS_CONNECT_ID_APP
        else:
            msg = json.dumps(
                {
                    "locale": self.locale,
                    "platformId": "webtrading",
                    "platformVersion": "chrome - 94.0.4606",
                    "clientId": "app.traderepublic.com",
                    "clientVersion": "5582",
                }
            )
            connect_id = WS_CONNECT_ID_WEB
        await self.ws.send(f"connect {connect_id} {msg}")
        response = await self.ws.recv()
        if response != "connected":
            raise TRapiException(f"Connection Error: {response}")

    async def sub(self, payload_key, callback, **kwargs):
        await self._ensure_ws()

        payload = kwargs.get("payload", {"type": payload_key})
        if self.auth == "device" and self.sessionToken:
            payload["token"] = self.sessionToken

        key = kwargs.get("key", payload_key)
        id = self.type_to_id(key)
        if id is None:
            async with self.mu:
                id = str(len(self.dict))
                self.dict[key] = id

        await self.ws.send(f"sub {id} {json.dumps(payload)}")

        self.callbacks[id] = callback

    def do_request(self, path, payload):

        if self.signing_key is None:
            with open(self.key_file, "rb") as f:
                self.signing_key = SigningKey.from_pem(
                    f.read(), hashfunc=hashlib.sha512
                )

        timestamp = int(time.time() * 1000)

        payload_string = json.dumps(payload)

        signature = self.signing_key.sign(
            bytes(f"{timestamp}.{payload_string}", "utf-8"),
            hashfunc=hashlib.sha512,
            sigencode=sigencode_der,
        )

        headers = dict()
        headers["X-Zeta-Timestamp"] = str(timestamp)
        headers["X-Zeta-Signature"] = base64.b64encode(signature).decode("ascii")
        headers["Content-Type"] = "application/json"
        return requests.request(
            method="POST", url=f"{self.url}{path}", data=payload_string, headers=headers
        )

    async def get_data(self):
        return await self.ws.recv()

    # list of requests: https://github.com/J05HI/pytr
    # -----------------------------------------------------------

    exchange_list = ["LSX", "TDG", "LUS", "TUB", "BHS", "B2C"]
    range_list = ["1d", "5d", "1m", "3m", "1y", "max"]
    product_category_list = ["vanillaWarrant", "knockOutProduct", "factor"]
    instrument_list = ["stock", "fund", "derivative", "crypto"]
    jurisdiction_list = ["AT", "DE", "ES", "FR", "IT", "NL", "BE", "EE", "FI", "IE", "GR", "LU", "LT",
                         "LV", "PT", "SI", "SK"]
    expiry_list = ["gfd", "gtd", "gtc"]
    order_type_list = ["buy", "sell"]

    # todo accruedInterestTermsRequired

    async def add_to_watchlist(self, id, callback=print):
        """addToWatchlist request"""
        return await self.sub(
            "addToWatchlist",
            payload={"type": "addToWatchlist", "instrumentId": id},
            callback=callback,
            key=f"addToWatchlist {id}"
        )

    async def aggregate_history_light(self, isin, range="max", resolution=604800000, exchange="LSX", callback=print):
        """aggregateHistoryLight request

        No login required

        :param isin: the stock's isin
        :param range: the range to display ("1d", "5d", "1m", "3m", "1y", "max")
        :param resolution: the resolution in milliseconds; the default is 7 days
        :param exchange: the exchange the instrument is traded at
        :param callback: callback function
        :return: stock history
        """
        if range not in self.range_list:
            raise TRapiException(f"Range of time must be either one of {self.range_list}")

        if exchange not in self.exchange_list:
            raise TRapiException(f"exchange must be either one of {self.exchange_list}")

        return await self.sub(
            "aggregateHistoryLight",
            payload={"type": "aggregateHistoryLight",
                     "range": range,
                     "id": f"{isin}.{exchange}",
                     "resolution": resolution},
            callback=callback,
            key=f"aggregateHistoryLight {isin} {exchange} {range}",
        )

    async def available_cash(self, callback=print):
        """availableCash request"""
        await self.sub("availableCash", callback)

    async def available_cash_for_payout(self, callback=print):
        """availableCashForPayout request"""
        await self.sub("availableCashForPayout", callback)

    async def available_size(self, isin, exchange="LSX", callback=print):
        """availableSize request — how many units can be bought/sold at the exchange."""
        if exchange not in self.exchange_list:
            raise TRapiException(f"exchange must be either one of {self.exchange_list}")
        return await self.sub(
            "availableSize",
            payload={
                "type": "availableSize",
                "parameters": {"exchangeId": exchange, "instrumentId": isin},
            },
            callback=callback,
            key=f"availableSize {isin} {exchange}",
        )

    async def cancel_order(self, id, callback=print):
        """cancelOrder request"""
        return await self.sub(
            "cancelOrder",
            payload={"type": "cancelOrder", "orderId": id},
            callback=callback,
            key=f"cancelOrder {id}"
        )

    async def cancel_price_alarm(self, id, callback=print):
        """cancelPriceAlarm request"""
        return await self.sub(
            "cancelPriceAlarm",
            payload={"type": "cancelPriceAlarm", "id": id},
            callback=callback,
            key=f"cancelPriceAlarm {id}",
        )

    async def cancel_savings_plan(self, id, callback=print):
        """cancelSavingsPlan request"""
        await self.sub(
            "cancelSavingsPlan",
            payload={"type": "cancelSavingsPlan", "id": id},
            callback=callback,
            key=f"cancelSavingsPlan {id}"
        )

    async def cash(self, callback=print):
        """cash request"""
        await self.sub("cash", callback)

    # todo changeOrder

    async def change_savings_plan(self, id, isin, amount, startDate, interval, warnings_shown,
                                  callback=print):  # todo what is warningsshown?
        """changeSavingsPlan request"""

        params = {"instrumentId": isin,
                  "amount": amount,
                  "startDate": startDate,
                  "interval": interval
                  }

        return await self.sub(
            "changeSavingsPlan",
            payload={
                "type": "changeSavingsPlan",
                "id": id,
                "parameters": params,
                "warningsShown": warnings_shown,
            },
            callback=callback,
            key=f"changeSavingsPlan {id}"
        )

    # todo collection

    async def compact_portfolio(self, callback=print):
        """compactPortfolio request (legacy). Prefer compact_portfolio_by_type."""
        await self.sub("compactPortfolio", callback)

    async def compact_portfolio_by_type(self, sec_acc_no=None, callback=print):
        """compactPortfolioByType request — current TR web portfolio endpoint (2026)."""
        payload = {"type": "compactPortfolioByType"}
        sec_acc_no = sec_acc_no or self.sec_acc_no
        if sec_acc_no:
            payload["secAccNo"] = sec_acc_no
        return await self.sub(
            "compactPortfolioByType",
            payload=payload,
            callback=callback,
            key=f"compactPortfolioByType {sec_acc_no}",
        )

    async def account_pairs(self, callback=print):
        """accountPairs request — securities/cash account numbers including tax wrappers."""
        return await self.sub("accountPairs", callback)

    # todo  confirmOrder

    async def create_price_alarm(self, isin, target_price, callback=print):
        """createPriceAlarm request"""
        return await self.sub(
            "createPriceAlarm",
            payload={
                "type": "createPriceAlarm",
                "instrumentId": isin,
                "targetPrice": target_price,
            },
            callback=callback,
            key=f"createPriceAlarm {isin} {target_price}",
        )

    async def create_savings_plan(self, isin, amount, startDate, interval, warnings_shown,
                                  callback=print):  # todo what is warningsshown?
        """createSavingsPlan request"""

        params = {"instrumentId": isin,
                  "amount": amount,
                  "startDate": startDate,
                  "interval": interval
                  }

        return await self.sub(
            "createSavingsPlan",
            payload={
                "type": "createSavingsPlan",
                "parameters": params,
                "warningsShown": warnings_shown,
            },
            callback=callback,
            key=f"createSavingsPlan {params} {warnings_shown}"
        )

    async def crypto_details(self, isin, callback=print):
        """cryptoDetails request"""
        return await self.sub(
            "cryptoDetails",
            payload={"type": "cryptoDetails", "id": isin},
            callback=callback,
            key=f"cryptoDetails {isin}",
        )

    async def etf_composition(self, isin, callback=print):
        """etfComposition request"""
        return await self.sub(
            "etfComposition",
            payload={"type": "etfComposition", "id": isin},
            callback=callback,
            key=f"etfComposition {isin}",
        )

    async def etf_details(self, isin, callback=print):
        """etfDetails request"""
        return await self.sub(
            "etfDetails",
            payload={"type": "etfDetails", "id": isin},
            callback=callback,
            key=f"etfDetails {isin}",
        )

    async def frontend_experiment(self, operation, experimentId, identifier, callback=print):
        """frontendExperiment request"""
        return await self.sub(
            "frontendExperiment",
            payload={"type": "frontendExperiment", "operation": operation, "experimentId": experimentId,
                     "identifier": identifier},
            callback=callback,
            key=f"frontendExperiment {operation} {experimentId} {identifier}",
        )

    async def instrument(self, id, callback=print):
        """instrument request

        No login required

        Gets basic information about the instrument. For more information, use stock_details, crypto_details and etf_details.

        :param id: instrument's id
        :param callback: callback function
        :return: information about the instrument
        """
        return await self.sub(
            "instrument",
            payload={"type": "instrument", "id": id},
            callback=callback,
            key=f"instrument {id}",
        )

    # todo: there is a parameter needed, probably the exchange?
    async def instrument_exchange(self, instrument_id, callback=print):
        """instrumentExchange request"""
        return await self.sub(
            "instrumentExchange",
            payload={"type": "instrumentExchange", "instrumentId": instrument_id},
            callback=callback,
            key=f"instrumentExchange {instrument_id}",
        )

    async def home_instrument_exchange(self, instrument_id, callback=print):
        """homeInstrumentExchange request"""
        return await self.sub(
            "homeInstrumentExchange",
            payload={"type": "homeInstrumentExchange", "instrumentId": instrument_id},
            callback=callback,
            key=f"homeInstrumentExchange {instrument_id}",
        )

    async def instrument_suitability(self, instrument_id, callback=print):
        """instrumentSuitability request"""
        return await self.sub(
            "instrumentSuitability",
            payload={"type": "instrumentSuitability", "instrumentId": instrument_id},
            callback=callback,
            key=f"instrumentSuitability {instrument_id}",
        )

    # todo investableWatchlist
    async def message_of_the_day(self, callback=print):
        """messageOfTheDay request"""
        await self.sub("messageOfTheDay", callback)

    # todo  namedWatchlist
    async def neon_cards(self, callback=print):
        """neonCards request"""
        await self.sub("neonCards", callback)

    async def derivatives(self, isin, product_category, callback=print):
        """derivatives request"""
        if product_category not in self.product_category_list:
            raise TRapiException(
                f"product_category must be either one of {self.product_category_list}"
            )
        return await self.sub(
            "derivatives",
            payload={"type": "derivatives", "underlying": isin, "productCategory": product_category},
            callback=callback,
            key=f"derivatives {isin}",
        )

    async def neon_search(self, query="", page=1, page_size=20, instrument_type="stock", jurisdiction="DE",
                          callback=print):
        """neonSearch request

        No login required.

        :return: list of instruments
        """

        if instrument_type not in self.instrument_list:
            raise TRapiException(f"type must be either one of {self.instrument_list}")

        if jurisdiction not in self.jurisdiction_list:
            raise TRapiException(f"Jurisdiction must be either one of {self.jurisdiction_list}")

        filter = [{"key": "type", "value": instrument_type},
                  {"key": "jurisdiction", "value": jurisdiction},
                  # [{"key": "relativePerformance", "value": "VAL"}]  # todo: are there more filters?
                  ]
        data = {"q": query,
                "page": page,
                "pageSize": page_size,
                "filter": filter}
        await self.sub(
            "neonSearch",
            callback=callback,
            payload={"type": "neonSearch", "data": data},
            key=f"neonSearch {query} {page} {page_size} {filter}",
        )

    async def neon_search_aggregations(self, query="", page=1, page_size=20, instrument_type="stock", jurisdiction="DE",
                                       callback=print):
        """neonSearchAggregations request

        No login required

        :return: list of categories of instruments and number of instruments per category"""

        if instrument_type not in self.instrument_list:
            raise TRapiException(f"type must be either one of {self.instrument_list}")

        if jurisdiction not in self.jurisdiction_list:
            raise TRapiException(f"Jurisdiction must be either one of {self.jurisdiction_list}")

        filter = [{"key": "type", "value": instrument_type},
                  {"key": "jurisdiction", "value": jurisdiction},
                  # [{"key": "relativePerformance", "value": "VAL"}]  # todo: are there more filters?
                  ]
        data = {"q": query,
                "page": page,
                "pageSize": page_size,
                "filter": filter}
        await self.sub(
            "neonSearchAggregations",
            callback=callback,
            payload={"type": "neonSearchAggregations", "data": data},
            key=f"neonSearchAggregations {query} {page} {page_size} {filter}",
        )

    async def neon_search_suggested_tags(self, query="", callback=print):
        """neonSearchSuggestedTags request"""

        data = {"q": query,
                }
        await self.sub(
            "neonSearchSuggestedTags",
            callback=callback,
            payload={"type": "neonSearchSuggestedTags", "data": data},
            key=f"neonSearchSuggestedTags {query}",
        )

    async def neon_search_tags(self, callback=print):
        """neonSearchTags request

        No login required

        :return: available search tags
        """
        await self.sub("neonSearchTags", callback)

    async def neon_news(self, isin, callback=print):
        """neonNews request

        No login required

        :return: news articles about the company
        """
        await self.sub(
            "neonNews",
            callback=callback,
            payload={"type": "neonNews", "isin": isin},
            key=f"news {isin}"
        )

    async def news_subscriptions(self, callback=print):
        """newsSubscriptions request"""
        return await self.sub("newsSubscriptions", callback)

    async def orders(self, terminated=False, callback=print):
        """orders request"""
        return await self.sub(
            "orders",
            callback=callback,
            payload={"type": "orders", "terminated": terminated},
            key=f"orders {terminated}")

    async def performance(self, isin, exchange="LSX", callback=print):
        """performance request"""
        if exchange not in self.exchange_list:
            raise TRapiException(f"exchange must be either one of {self.exchange_list}")
        return await self.sub(
            "performance",
            payload={"type": "performance", "id": f"{isin}.{exchange}"},
            callback=callback,
            key=f"performance {isin} {exchange}",
        )

    async def portfolio(self, callback=print):
        """portfolio"""
        await self.sub("portfolio", callback)

    async def portfolio_aggregate_history(self, range="max", callback=print):
        """portfolioAggregateHistory request"""
        if range not in self.range_list:
            raise TRapiException(f"Range of time must be either one of {self.range_list}")
        return await self.sub(
            "portfolioAggregateHistory",
            payload={"type": "portfolioAggregateHistory", "range": range},
            callback=callback,
            key=f"portfolioAggregateHistory {range}",
        )

    async def portfolio_aggregate_history_light(self, range="max", callback=print):
        """portfolioAggregateHistoryLight request"""
        if range not in self.range_list:
            raise TRapiException(f"Range of time must be either one of {self.range_list}")
        return await self.sub(
            "portfolioAggregateHistoryLight",
            payload={"type": "portfolioAggregateHistoryLight", "range": range},
            callback=callback,
            key=f"portfolioAggregateHistoryLight {range}",
        )
    async def portfolio_status(self, callback=print):
        """portfolioStatus request"""
        return await self.sub("portfolioStatus", callback)

    async def price_alarms(self, callback=print):
        """priceAlarms request"""
        return await self.sub("priceAlarms", callback)

    async def price_for_order(self, isin, exchange="LSX", order_type="buy", callback=print):
        """priceForOrder request"""
        if exchange not in self.exchange_list:
            raise TRapiException(f"exchange must be either one of {self.exchange_list}")
        if order_type not in self.order_type_list:
            raise TRapiException(f"order_Type must be either of {self.order_type_list}")
        return await self.sub(
            "priceForOrder",
            payload={
                "type": "priceForOrder",
                "parameters": {
                    "exchangeId": exchange,
                    "instrumentId": isin,
                    "type": order_type,
                },
            },
            callback=callback,
            key=f"priceForOrder {isin} {exchange} {order_type}",
        )
    async def remove_from_watchlist(self, instrument_id, callback=print):
        """removeFromWatchlist request"""
        return await self.sub(
            "removeFromWatchlist",
            callback=callback,
            payload={"type": "removeFromWatchlist", "instrumentId": instrument_id},
            key=f"removeFromWatchlist {instrument_id}")

    async def savings_plan_parameters(self, isin, callback=print):
        """savingsPlanParameters request"""
        return await self.sub(
            "savingsPlanParameters",
            payload={"type": "savingsPlanParameters", "instrumentId": isin},
            callback=callback,
            key=f"savingsPlanParameters {isin}",
        )

    async def savings_plans(self, callback=print):
        """savingsPlans request"""
        return await self.sub("savingsPlans", callback)

    async def settings(self, callback=print):
        """settings request"""
        return await self.sub("settings", callback)

    async def simple_create_order(
            self,
            order_id,
            isin,
            order_type,
            size,
            limit,
            expiry,
            exchange="LSX",
            callback=print,
    ):
        """simpleCreateOrder request"""
        if expiry not in self.expiry_list:
            raise TRapiException(f"Expiry must be either of {self.expiry_list}")

        if order_type not in self.order_type_list:
            raise TRapiException(
                f"order_Type must be either of {self.order_type_list}"
            )

        if exchange not in self.exchange_list:
            raise TRapiException(f"exchange must be either one of {self.exchange_list}")

        payload = {
            "type": "simpleCreateOrder",
            "clientProcessId": order_id,
            "warningsShown": ["userExperience"],
            "acceptedWarnings": ["userExperience"],
            "parameters": {
                "instrumentId": isin,
                "exchangeId": exchange,
                "expiry": {"type": expiry},
                "limit": limit,
                "mode": "limit",
                "size": size,
                "type": order_type,
            },
        }

        return await self.sub(
            "simpleCreateOrder",
            payload=payload,
            callback=callback,
            key=f"simpleCreateOrder {order_id}",
        )

    async def stock_detail_dividends(self, isin, callback=print):
        """stockDetailDividends request

        Login required!

        :param: isin: the stock's isin
        :return: complete list of stock's past dividends
        """
        await self.sub(
            "stockDetailDividends",
            callback=callback,
            payload={"type": "stockDetailDividends", "id": isin},  # todo: variable jurisdiction , "jurisdiction": "DE"?
            key=f"stockDetailDividends {isin}",
        )

    async def stock_detail_kpis(self, isin, callback=print):
        """stockDetailKpis request

        Login required!

        :param: isin: the stock's isin
        :return: list of stock's past kpis per year
        """
        await self.sub(
            "stockDetailKpis",
            callback=callback,
            payload={"type": "stockDetailKpis", "id": isin},  # todo: variable jurisdiction , "jurisdiction": "DE"?
            key=f"stockDetailKpis {isin}",
        )

    async def stock_details(self, isin, callback=print):
        """stockDetails request

        Login required!

        Gets detailed summary about stock. For more information you might need to use stock_detail_dividends or stock_detail_kpis

        :param: isin: the stock's isin
        :return: more detailed information about stock than instrument request
        """
        await self.sub(
            "stockDetails",
            callback=callback,
            payload={"type": "stockDetails", "id": isin},  # todo: variable jurisdiction , "jurisdiction": "DE"?
            key=f"stockDetails {isin}",
        )

    async def ticker(self, isin, exchange="LSX", callback=print):
        """ticker request"""

        if exchange not in self.exchange_list:
            raise TRapiException(f"exchange must be either one of {self.exchange_list}")

        await self.sub(
            "ticker",
            callback=callback,
            payload={"type": "ticker", "id": f"{isin}.{exchange}"},
            key=f"ticker {isin} {exchange}",
        )

    async def timeline(self, after=None, callback=print):
        """timeline request"""
        return await self.sub(
            "timeline",
            payload={"type": "timeline", "after": after},
            callback=callback,
            key=f"timeline {after}",
        )

    async def timeline_actions(self, callback=print):
        """timelineActions request"""
        return await self.sub("timelineActions", callback)

    async def timeline_transactions(self, after=None, callback=print):
        """timelineTransactions request — cash-relevant timeline subset."""
        return await self.sub(
            "timelineTransactions",
            payload={"type": "timelineTransactions", "after": after},
            callback=callback,
            key=f"timelineTransactions {after}",
        )

    async def timeline_activity_log(self, after=None, callback=print):
        """timelineActivityLog request"""
        return await self.sub(
            "timelineActivityLog",
            payload={"type": "timelineActivityLog", "after": after},
            callback=callback,
            key=f"timelineActivityLog {after}",
        )

    async def timeline_detail(self, id, callback=print):
        """timelineDetail request"""
        return await self.sub(
            "timelineDetail",
            payload={"type": "timelineDetail", "id": id},
            callback=callback,
            key=f"timelineDetail {id}",
        )

    async def timeline_detail_v2(self, id, callback=print):
        """timelineDetailV2 request — current detail payload used by the TR app."""
        return await self.sub(
            "timelineDetailV2",
            payload={"type": "timelineDetailV2", "id": id},
            callback=callback,
            key=f"timelineDetailV2 {id}",
        )

    async def subscribe_news(self, isin, callback=print):
        """subscribeNews request"""
        return await self.sub(
            "subscribeNews",
            payload={"type": "subscribeNews", "instrumentId": isin},
            callback=callback,
            key=f"subscribeNews {isin}",
        )

    async def unsubscribe_news(self, isin, callback=print):
        """unsubscribeNews request"""
        return await self.sub(
            "unsubscribeNews",
            payload={"type": "unsubscribeNews", "instrumentId": isin},
            callback=callback,
            key=f"unsubscribeNews {isin}",
        )

    async def watchlist(self, callback=print):
        """watchlist request"""
        return await self.sub("watchlist", callback)

    #  todo watchlists

    # -----------------------------------------------------------
    # old names of functions

    @deprecated(reason="Use function neon_news")
    async def news(self, isin, callback=print):
        await self.neon_news(isin, callback=callback)

    @deprecated(reason="Use function instrument")
    async def derivativ_details(self, isin, callback=print):
        await self.instrument(isin, callback=callback)

    @deprecated(reason="Use function portfolio_aggregate_history")
    async def port_hist(self, range="max", callback=print):
        await self.portfolio_aggregate_history(range=range, callback=callback)

    @deprecated(reason="Use function orders")
    async def curr_orders(self, callback=print):
        await self.orders(callback=callback)

    @deprecated(reason="Use function timeline")
    async def hist(self, after=None, callback=print):
        await self.timeline(after=after, callback=callback)

    @deprecated(reason="Use function timeline_detail")
    async def hist_event(self, id, callback=print):
        await self.timeline_detail(id, callback=callback)

    @deprecated(reason="Use function orders")
    async def all_orders(self, callback=print):
        await self.orders(callback=callback)

    @deprecated(reason="Use function cancel_order")
    async def order_cancel(self, id, callback=print):
        await self.cancel_order(id, callback=callback)

    @deprecated(reason="Use function simple_create_order")
    async def limit_order(
            self,
            order_id,
            isin,
            order_type,
            size,
            limit,
            expiry,
            exchange="LSX",
            callback=print,
    ):
        await self.simple_create_order(order_id, isin, order_type, size, limit, expiry, exchange=exchange,
                                       callback=callback)

    @deprecated(reason="Use function aggregate_history_light")
    async def stock_history(self, isin, range="max", callback=print):
        await self.aggregate_history_light(isin, range=range, callback=callback)

    # -----------------------------------------------------------

    async def start(self, receive_one=False):
        async with self.mu:
            if self.started:
                raise TRapiException("TrApi has already been started")

            self.started = True

        try:
            while True:
                data_a = await self.get_data()

                data = str(data_a).split()

                id, state = data[:2]

                # Initial response
                if len(data[2:]) == 1:
                    data = data[2:][0]
                else:
                    data = data[2:]

                if state == "D":
                    data = self.decode_updates(id, data)
                elif state == "A":
                    pass
                elif state == "C":
                    continue
                elif state == "E":
                    sErr = f"ERROR state: {state} data: {data}"
                    raise TRapiExcServerErrorState(
                        f"Error during server access\n\tServer-side Object probably expired...\n\t{sErr}"
                    )
                else:
                    sErr = f"ERROR UNKNOWN state: {state} data: {data}"
                    print(sErr)
                    raise TRapiExcServerUnknownState(f"Error during server access\n\t{sErr}")

                if isinstance(data, list):
                    data = " ".join(data)

                self.latest_response[id] = data
                obj = json.loads(data)

                key = None
                for k, v in self.dict.items():
                    if v == id:
                        key = k
                        break

                if isinstance(obj, list):
                    # if it is a list just add the key to every element
                    for i in range(0, len(obj)):
                        obj[i]["key"] = key
                elif isinstance(obj, dict):
                    obj["key"] = key

                if receive_one:
                    return obj
                self.callbacks[id](obj)
        finally:
            # Always release one-shot receive state (timeouts, cancel, E/unknown).
            if receive_one:
                self.started = False
                self.callbacks = {}
                self.latest_response = {}

    @classmethod
    def all_isins(cls):
        folder = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(folder, "isins.txt")
        with open(path) as f:
            isins = f.read().splitlines()

        return isins

    def type_to_id(self, t: str) -> str:
        return self.dict.get(t, None)

    def decode_updates(self, key, payload):
        # Let's take an example, the first payload is the initial response we go
        # and the second one is update, meaning there are new values.
        #
        # The second one looks kinda strange but we will get to it.
        #
        # 1. {"bid":{"time":1611928659702,"price":13.873,"size":3615},"ask":{"time":1611928659702,"price":13.915,
        # "size":3615},"last":{"time":1611928659702,"price":13.873,"size":3615},"pre":{"time":1611855712255,
        # "price":13.756,"size":0},"open":{"time":1611901151053,"price":13.743,"size":0},"qualityId":"realtime",
        # "leverage":null,"delta":null}
        #
        # 2. ['=23', '-5', '+64895', '=14', '-1', '+5', '=36', '-5', '+64895', '=14',
        # '-1', '+3', '=37', '-5', '+64895', '=14', '-1', '+5', '=173']
        #
        # The payload is in json format but to update the payload we have to treat it as a string.
        # Lets name the 1 payload fst. We treat fst as a string and in the second payload
        # we have instructions which values to keep and which to update.
        #   +23 => Keep 23 chars of the previous payload
        #   -5 => Replace the next 5 chars
        #   +64895 => Replace those 5 chars with 64895
        #   =14 => Keep 14 chars of the previous payload

        latest = self.latest_response[key]

        cur = 0

        rsp = ""
        for x in payload:

            instruction = x[0]
            rst = x[1:]

            if instruction == "=":
                num = int(rst)
                rsp += latest[cur: (cur + num)]
                cur += num
            elif instruction == "-":
                cur += int(rst)
            elif instruction == "+":
                rsp += rst
            else:
                raise TRapiException("Error in decode_updates()")

        return rsp


class TrBlockingApi(TRApi):
    def __init__(self, number, pin, timeout=20.0, locale="en", key_file=None, auth="web", cookies_file=None):
        self.timeout = timeout
        super().__init__(number, pin, locale, key_file=key_file, auth=auth, cookies_file=cookies_file)

    def _run(self, coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            raise RuntimeError(
                "TrBlockingApi cannot run inside an active asyncio event loop "
                "(e.g. FastMCP / Hermes). Use async TRApi with await, not "
                "TrBlockingApi sync wrappers."
            )
        return asyncio.get_event_loop().run_until_complete(self.get_one(coro))

    async def get_one(self, f):
        await f
        res = None
        try:
            res = await asyncio.wait_for(
                super().start(receive_one=True), timeout=self.timeout
            )
            return res
        except Exception as e:
            raise e
            # return None

    # -----------------------------------------------------------

    def aggregate_history_light(self, isin, range="max", resolution=604800000, exchange="LSX"):
        return self._run(
            super().aggregate_history_light(
                isin, range=range, resolution=resolution, exchange=exchange
            )
        )

    def available_cash(self):
        return self._run(super().available_cash())

    def available_cash_for_payout(self):
        return self._run(super().available_cash_for_payout())

    def cash(self):
        return self._run(super().cash())

    def instrument(self, id):
        return self._run(super().instrument(id))

    def neon_search(
        self,
        query="",
        page=1,
        page_size=20,
        instrument_type="stock",
        jurisdiction="DE",
    ):
        return self._run(
            super().neon_search(
                query=query,
                page=page,
                page_size=page_size,
                instrument_type=instrument_type,
                jurisdiction=jurisdiction,
            )
        )

    def neon_news(self, isin):
        return self._run(super().neon_news(isin))

    def neon_search_tags(self):
        return self._run(super().neon_search_tags())

    def orders(self):
        return self._run(super().orders())

    def portfolio(self):
        return self._run(super().portfolio())

    def portfolio_aggregate_history(self, range="max"):
        return self._run(super().portfolio_aggregate_history(range=range))

    def stock_detail_dividends(self, isin):
        return self._run(super().stock_detail_dividends(isin))

    def stock_detail_kpis(self, isin):
        return self._run(super().stock_detail_kpis(isin))

    def stock_details(self, isin):
        return self._run(super().stock_details(isin))

    def ticker(self, isin, exchange="LSX"):
        return self._run(super().ticker(isin, exchange))

    def timeline(self, after=None):
        return self._run(super().timeline(after=after))

    def timeline_detail(self, id):
        return self._run(super().timeline_detail(id=id))

    def account_pairs(self):
        return self._run(super().account_pairs())

    def compact_portfolio(self):
        return self._run(super().compact_portfolio())

    def compact_portfolio_by_type(self, sec_acc_no=None):
        return self._run(super().compact_portfolio_by_type(sec_acc_no=sec_acc_no))

    def crypto_details(self, isin):
        return self._run(super().crypto_details(isin))

    def etf_details(self, isin):
        return self._run(super().etf_details(isin))

    def etf_composition(self, isin):
        return self._run(super().etf_composition(isin))

    def news_subscriptions(self):
        return self._run(super().news_subscriptions())

    def performance(self, isin, exchange="LSX"):
        return self._run(super().performance(isin, exchange=exchange))

    def portfolio_status(self):
        return self._run(super().portfolio_status())

    def price_alarms(self):
        return self._run(super().price_alarms())

    def savings_plans(self):
        return self._run(super().savings_plans())

    def settings(self):
        return self._run(super().settings())

    def timeline_transactions(self, after=None):
        return self._run(super().timeline_transactions(after=after))

    def timeline_activity_log(self, after=None):
        return self._run(super().timeline_activity_log(after=after))

    def timeline_detail_v2(self, id):
        return self._run(super().timeline_detail_v2(id))

    def watchlist(self):
        return self._run(super().watchlist())

    # -----------------------------------------------------------
    # old names of functions

    @deprecated(reason="Use function timeline")
    def hist(self, after=None):
        return self.timeline(after=after)

    @deprecated(reason="Use function neon_news")
    def news(self, isin):
        return self.neon_news(isin)

    @deprecated(reason="Use function orders")
    def curr_orders(self):
        return self.orders()

    @deprecated(reason="Use function portfolio_aggregate_history")
    def port_hist(self, range="max"):
        return self.portfolio_aggregate_history(range=range)

    @deprecated(reason="Use function instrument")
    def derivativ_details(self, isin):
        return self.instrument(isin)

    @deprecated(reason="Use function aggregate_history_light")
    def stock_history(self, isin, range="max"):
        return self.aggregate_history_light(isin, range=range)

    @deprecated(reason="Use function timeline_detail")
    def hist_event(self, id):
        return self.timeline_detail(id)
