"""Authenticated loopback identity endpoint used by the stdio bridge."""

from __future__ import annotations

from starlette.testclient import TestClient

from ifc_console.http_identity import identity_matches


def _client(core) -> TestClient:
    from ifc_console.mcp.server import build_http_app, build_mcp

    return TestClient(
        build_http_app(core, build_mcp(core)),
        base_url=f"http://127.0.0.1:{core.port}",
    )


def test_identity_proves_token_possession_without_receiving_authorization(core) -> None:
    nonce = "ab" * 32
    response = _client(core).get(f"/api/identify?nonce={nonce}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["name"] == "ifc-console"
    assert payload["port"] == core.port
    assert payload["nonce"] == nonce
    assert core.token not in response.text
    assert identity_matches(payload, core.token, nonce, core.port)
    assert not identity_matches(payload, "wrong-token", nonce, core.port)
    assert not identity_matches(payload, core.token, nonce, core.port + 1)


def test_identity_requires_one_canonical_nonce(core) -> None:
    client = _client(core)
    bad_urls = [
        "/api/identify",
        "/api/identify?nonce=short",
        f"/api/identify?nonce={'AB' * 32}",
        f"/api/identify?nonce={'00' * 32}&nonce={'11' * 32}",
    ]

    for url in bad_urls:
        response = client.get(url)
        assert response.status_code == 400, url
        assert response.json() == {"error": "invalid_nonce"}
        assert response.headers["cache-control"] == "no-store"


def test_identity_exemption_is_get_only_and_keeps_loopback_boundary(core) -> None:
    client = _client(core)
    nonce = "12" * 32

    assert client.post(f"/api/identify?nonce={nonce}").status_code == 401
    rebound = client.get(f"/api/identify?nonce={nonce}", headers={"Host": "attacker.example"})
    assert rebound.status_code == 403
