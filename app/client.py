import asyncio
import re
import logging
from typing import Optional, Any
import httpx
from bs4 import BeautifulSoup

from app.config import settings

logger = logging.getLogger(__name__)


class PortalError(Exception):
    """Base exception for portal client errors."""
    pass


class PortalAuthError(PortalError):
    """Raised when portal authentication fails or credentials are rejected."""
    pass


class PortalUnavailable(PortalError):
    """Raised when portal is down, unreachable, or returns persistent 5xx."""
    pass


class PortalParseError(PortalError):
    """Raised when portal HTML/JSON cannot be parsed."""
    pass


class PortalClient:
    """Persistent HTTP client interacting with the legacy Urja Ops portal."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: Optional[float] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        max_concurrency: Optional[int] = None,
    ):
        self.base_url = (base_url or settings.PORTAL_BASE_URL).rstrip("/")
        self.username = username or settings.PORTAL_USERNAME
        self.password = password or settings.PORTAL_PASSWORD
        self.timeout = timeout or settings.PORTAL_TIMEOUT_SECONDS
        
        # Concurrency guard
        concurrency = max_concurrency or settings.PORTAL_MAX_CONCURRENCY
        self.semaphore = asyncio.Semaphore(concurrency)
        
        # Lock to ensure only one re-login runs at any time
        self._login_lock = asyncio.Lock()
        self._is_logged_in = False
        
        # Persistent httpx client with cookie jar
        client_kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "timeout": self.timeout,
            "follow_redirects": False,  # We intercept 302 redirects to detect session expiry
        }
        if transport is not None:
            client_kwargs["transport"] = transport

        self.client = httpx.AsyncClient(**client_kwargs)

    async def close(self):
        """Close the underlying HTTP client."""
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _fetch_csrf_token(self) -> Optional[str]:
        """Fetch login page and extract CSRF token if present."""
        try:
            resp = await self.client.get("/login", timeout=self.timeout)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                csrf_input = soup.find("input", {"name": re.compile(r"csrf", re.I)})
                if csrf_input and csrf_input.get("value"):
                    return str(csrf_input["value"])
        except Exception as e:
            logger.debug(f"Could not extract CSRF token from /login: {e}")
        return None

    async def login(self) -> bool:
        """Authenticate against the portal and store the session cookie."""
        async with self._login_lock:
            try:
                csrf_token = await self._fetch_csrf_token()
                payload = {
                    "username": self.username,
                    "email": self.username,
                    "password": self.password,
                }
                if csrf_token:
                    payload["csrf_token"] = csrf_token

                headers = {
                    "Origin": self.base_url,
                    "Referer": f"{self.base_url}/login",
                }

                resp = await self.client.post(
                    "/login", data=payload, headers=headers, timeout=self.timeout
                )

                # Check if login succeeded: either 302 redirecting to /meters, or 200 with session cookie
                if resp.status_code in (200, 302):
                    location = resp.headers.get("Location", "")
                    if "login" in location and "error" in location:
                        raise PortalAuthError("Invalid portal credentials")
                    
                    self._is_logged_in = True
                    logger.info("Successfully authenticated to Urja Ops Portal")
                    return True
                elif resp.status_code == 401:
                    raise PortalAuthError("Portal rejected login credentials with 401 Unauthorized")
                else:
                    raise PortalAuthError(f"Unexpected login status: {resp.status_code}")

            except httpx.RequestError as e:
                raise PortalUnavailable(f"Network error connecting to portal during login: {e}") from e

    def _is_session_expired(self, response: httpx.Response) -> bool:
        """Check if response indicates an expired or missing session."""
        if response.status_code == 401:
            return True
        if response.status_code in (302, 303, 307):
            location = response.headers.get("Location", "")
            if "/login" in location:
                return True
        return False

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """
        Execute an HTTP request with:
        - Concurrency throttling (semaphore)
        - Session expiry detection & single re-login retry
        - Exponential backoff retry on transient errors (timeouts, 5xx)
        """
        async with self.semaphore:
            # Ensure logged in first
            if not self._is_logged_in:
                await self.login()

            last_exc = None
            max_attempts = settings.PORTAL_MAX_RETRIES

            for attempt in range(1, max_attempts + 1):
                try:
                    resp = await self.client.request(method, path, **kwargs)

                    # Check for session expiry
                    if self._is_session_expired(resp):
                        logger.warning("Session expired or redirected to login. Re-authenticating...")
                        await self.login()
                        # Retry the request once after re-login
                        resp = await self.client.request(method, path, **kwargs)
                        if self._is_session_expired(resp):
                            raise PortalAuthError("Portal session expired and re-authentication failed to access resource")
                        return resp

                    # Transient server errors (500, 502, 503, 504) -> backoff and retry
                    if resp.status_code in (500, 502, 503, 504):
                        if attempt < max_attempts:
                            delay = 0.5 * (2 ** (attempt - 1))
                            logger.warning(f"Portal returned {resp.status_code}, retrying in {delay}s (attempt {attempt}/{max_attempts})...")
                            await asyncio.sleep(delay)
                            continue
                        else:
                            raise PortalUnavailable(f"Portal returned persistent {resp.status_code} error after {max_attempts} attempts")

                    return resp

                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    last_exc = e
                    if attempt < max_attempts:
                        delay = 0.5 * (2 ** (attempt - 1))
                        logger.warning(f"Network issue ({e}), retrying in {delay}s (attempt {attempt}/{max_attempts})...")
                        await asyncio.sleep(delay)
                    else:
                        raise PortalUnavailable(f"Portal request failed after {max_attempts} attempts: {e}") from e

            if last_exc:
                raise PortalUnavailable(f"Portal request failed: {last_exc}") from last_exc
            raise PortalUnavailable("Portal request failed unexpectedly")
