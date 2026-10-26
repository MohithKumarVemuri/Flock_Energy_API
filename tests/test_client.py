import pytest
import httpx
from app.client import PortalClient, PortalAuthError, PortalUnavailable


@pytest.mark.asyncio
async def test_client_login_and_request():
    login_called = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal login_called
        if request.url.path == "/login" and request.method == "POST":
            login_called += 1
            return httpx.Response(302, headers={"Location": "/meters", "Set-Cookie": "urja_session=mock_token"})
        elif request.url.path == "/login" and request.method == "GET":
            return httpx.Response(200, text='<input name="csrf_token" value="abc123csrf" />')
        elif request.url.path == "/meters":
            return httpx.Response(200, text="<html><body>Meter List</body></html>")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = PortalClient(base_url="https://test-portal.local", transport=transport)
    
    resp = await client.request("GET", "/meters")
    assert resp.status_code == 200
    assert "Meter List" in resp.text
    assert login_called == 1
    await client.close()


@pytest.mark.asyncio
async def test_client_expired_session_auto_reauth_and_retry_success():
    """Verify that an expired session (redirect to /login) triggers re-login and retries successfully."""
    request_count = 0
    login_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count, login_count
        if request.url.path == "/login":
            if request.method == "GET":
                return httpx.Response(200, text="")
            elif request.method == "POST":
                login_count += 1
                return httpx.Response(302, headers={"Location": "/meters", "Set-Cookie": f"urja_session=token_{login_count}"})
        elif request.url.path == "/meters/J100000":
            request_count += 1
            if request_count == 1:
                # First attempt after initial login: simulate expired session redirect
                return httpx.Response(302, headers={"Location": "/login?expired=1"})
            # Second attempt succeeds
            return httpx.Response(200, text="<html>Detail J100000</html>")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = PortalClient(base_url="https://test-portal.local", transport=transport)

    resp = await client.request("GET", "/meters/J100000")
    assert resp.status_code == 200
    assert "Detail J100000" in resp.text
    assert login_count == 2  # initial login + 1 re-login
    assert request_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_client_repeated_auth_failure_raises_typed_error():
    """Verify that if re-login does not resolve unauthorized state, typed PortalAuthError is raised."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(302, headers={"Location": "/meters"})
        elif request.url.path == "/meters/secret":
            # Always redirects to /login
            return httpx.Response(302, headers={"Location": "/login"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = PortalClient(base_url="https://test-portal.local", transport=transport)

    with pytest.raises(PortalAuthError):
        await client.request("GET", "/meters/secret")
    await client.close()


@pytest.mark.asyncio
async def test_client_persistent_5xx_raises_portal_unavailable():
    """Verify that repeated 502/503 errors trigger backoff retry and raise PortalUnavailable."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(302, headers={"Location": "/meters"})
        return httpx.Response(503, text="Service Unavailable")

    transport = httpx.MockTransport(handler)
    client = PortalClient(base_url="https://test-portal.local", transport=transport)

    with pytest.raises(PortalUnavailable):
        await client.request("GET", "/meters")
    await client.close()
