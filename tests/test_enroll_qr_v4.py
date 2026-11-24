"""Enrollment QR in the browser: POST /devices/enroll-qr and the public GET /enroll/view/{token}."""
import html
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text, update

from app.core.config import Settings
from app.core.timezone import now_utc
from app.db.models_assistant import Audit
from app.db.models_devices import Device, EnrollCode, EnrollQrView
from app.services import enroll_qr
from tests.test_devices_v4 import ADMIN, API, auth, enroll_key, sha256
from tests.test_route_scopes import api_routes, route_scopes

GENERIC = "Enlace no válido o caducado"


async def create(client, body=None, headers=ADMIN):
    return await client.post(f"{API}/devices/enroll-qr", headers=headers, json=body) if body is not None else await client.post(f"{API}/devices/enroll-qr", headers=headers)


async def make_view(client, body=None):
    response = await create(client, body)
    assert response.status_code == 201, response.text
    return response.json()


def token_of(view_url):
    return view_url.rsplit("/", 1)[1]


def view_path(view_url):
    return urlsplit(view_url).path


def link_of(page):
    return html.unescape(re.search(r"<code>(cofre://[^<]+)</code>", page).group(1))


def codes_of(link):
    query = parse_qs(urlsplit(link).query)
    return query["ingest"][0], query["interactive"][0]


async def test_admin_creates_view_url(client, db):
    body = await make_view(client)
    assert set(body) == {"view_url", "expires_at"}
    assert body["view_url"].startswith("http://test/api/v1/enroll/view/")
    assert len(token_of(body["view_url"])) >= 43  # token_urlsafe(32)
    remaining = datetime.fromisoformat(body["expires_at"]) - now_utc()
    assert timedelta(minutes=9) < remaining <= timedelta(minutes=10)
    codes = (await db.execute(select(EnrollCode).order_by(EnrollCode.id))).scalars().all()
    assert [row.scopes for row in codes] == [["ingest"], ["read", "review"]]
    assert all(row.used_at is None and row.expires_at == codes[0].expires_at for row in codes)


async def test_body_options(client):
    short = await make_view(client, {"ttl_minutes": 2, "name_hint": "Pixel de Ana"})
    remaining = datetime.fromisoformat(short["expires_at"]) - now_utc()
    assert timedelta(minutes=1) < remaining <= timedelta(minutes=2)
    assert (await make_view(client, {}))["view_url"]
    for bad in ({"ttl_minutes": 0}, {"ttl_minutes": 11}, {"name_hint": ""}):
        response = await create(client, bad)
        assert response.status_code == 422 and response.json()["code"] == "validation_error"


async def test_only_admin_can_create(client):
    assert (await create(client, headers={})).status_code == 401
    assert (await create(client, headers=auth("wrong-key"))).status_code == 401
    for scopes in (["read", "review"], ["read"], ["ingest"]):
        device = await enroll_key(client, scopes)
        response = await create(client, headers=auth(device["key"]))
        assert response.status_code == 403 and response.json()["code"] == "insufficient_scope"


async def test_public_base_url_setting(client, monkeypatch, settings):
    monkeypatch.setattr("app.api.routers.devices.get_settings", lambda: settings.model_copy(update={"public_api_base_url": "https://financeapi.example"}))
    body = await make_view(client)
    assert body["view_url"].startswith("https://financeapi.example/api/v1/enroll/view/")
    page = (await client.get(view_path(body["view_url"]))).text
    assert link_of(page).startswith("cofre://enroll?api=https://financeapi.example&ingest=")


def test_public_base_url_validation():
    config = {"finance_api_key": "k" * 32, "_env_file": None}
    assert Settings(public_api_base_url=" https://financeapi.example/ ", **config).public_api_base_url == "https://financeapi.example"
    assert Settings(**config).public_api_base_url == ""
    for bad in ("financeapi.example", "ftp://x", "https://x/api/v1", "https://x?a=1"):
        with pytest.raises(ValidationError):
            Settings(public_api_base_url=bad, **config)


async def test_view_page_has_qr_headers_and_link(client):
    body = await make_view(client)
    response = await client.get(view_path(body["view_url"]))  # no API key
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "<title>Conectar Cofre</title>" in page and "<h1>Conectar Cofre</h1>" in page and "<svg" in page
    assert "<ol>" in page and page.count("<li>") == 3 and "<details>" in page
    for source in ("http://", "https://", "src="):  # nothing external: no CDN, no script file
        assert source not in re.sub(r"<code>.*?</code>", "", page)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-robots-tag"] == "noindex"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "img-src data:" in csp and "style-src 'unsafe-inline'" in csp and "script-src 'unsafe-inline'" in csp
    link = link_of(page)
    ingest, interactive = codes_of(link)
    assert link == f"cofre://enroll?api=http://test&ingest={ingest}&interactive={interactive}"
    # No QR decoder in the dependencies: the inline SVG must be exactly what segno draws for that link text.
    assert enroll_qr.render_qr_svg(link) in page
    assert enroll_qr.render_qr_svg(link + "x") not in page


async def test_codes_in_link_enroll_the_phone_and_then_the_page_dies(client):
    path = view_path((await make_view(client))["view_url"])
    ingest, interactive = codes_of(link_of((await client.get(path)).text))
    first = await client.post(f"{API}/devices/enroll", json={"code": ingest, "name": "teléfono", "scopes": ["ingest"]})
    assert first.status_code == 201
    assert (await client.get(path)).status_code == 404  # one used code is enough
    second = await client.post(f"{API}/devices/enroll", json={"code": interactive, "name": "teléfono", "scopes": ["read", "review"]})
    assert second.status_code == 201 and second.json()["scopes"] == ["read", "review"]
    assert (await client.get(path)).status_code == 404
    assert (await client.post(f"{API}/devices/enroll", json={"code": ingest, "name": "x", "scopes": ["ingest"]})).status_code == 401


async def test_dead_links_are_one_generic_404(client, db):
    expired_codes = view_path((await make_view(client))["view_url"])
    expired_view = view_path((await make_view(client))["view_url"])
    used = view_path((await make_view(client))["view_url"])
    live = view_path((await make_view(client))["view_url"])
    ingest, _ = codes_of(link_of((await client.get(used)).text))
    await client.post(f"{API}/devices/enroll", json={"code": ingest, "name": "x", "scopes": ["ingest"]})
    expired_ingest, expired_interactive = codes_of(link_of((await client.get(expired_codes)).text))
    past = now_utc() - timedelta(seconds=1)
    await db.execute(update(EnrollCode).where(EnrollCode.code_hash.in_([sha256(expired_ingest), sha256(expired_interactive)])).values(expires_at=past))
    await db.execute(update(EnrollQrView).where(EnrollQrView.token_hash == sha256(token_of(expired_view))).values(expires_at=past))
    await db.commit()
    responses = [await client.get(path) for path in (expired_codes, expired_view, used, "/api/v1/enroll/view/does-not-exist", "/api/v1/enroll/view/" + "a" * 5000)]
    assert [response.status_code for response in responses] == [404] * 5
    assert len({response.text for response in responses}) == 1
    for response in responses:
        assert GENERIC in response.text and "<svg" not in response.text and "cofre://" not in response.text
        assert response.headers["cache-control"] == "no-store" and response.headers["x-robots-tag"] == "noindex"
        assert response.headers["content-type"].startswith("text/html")
    assert (await client.get(live)).status_code == 200


async def test_failed_views_count_toward_ip_limit(client):
    for _ in range(10):
        assert (await client.get("/api/v1/enroll/view/nope")).status_code == 404
    limited = await client.get("/api/v1/enroll/view/nope")
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) >= 1


async def test_only_hashes_are_stored_and_audit_has_no_secrets(client, db):
    body = await make_view(client, {"name_hint": "Pixel de Ana"})
    token = token_of(body["view_url"])
    ingest, interactive = enroll_qr.derive_codes(token)
    view = (await db.execute(select(EnrollQrView))).scalar_one()
    assert view.token_hash == sha256(token)
    dump = ""
    for table in ("enroll_qr_views", "enroll_codes", "audit"):
        dump += str((await db.execute(text(f"SELECT * FROM {table}"))).all())  # noqa: S608 - fixed table names
    for secret in (token, ingest, interactive):
        assert secret not in dump
    assert sha256(token) in dump and sha256(ingest) in dump and sha256(interactive) in dump
    entry = (await db.execute(select(Audit).where(Audit.action == "device.enroll_qr"))).scalar_one()
    assert entry.actor == "admin" and entry.target_id == str(view.id)
    assert entry.detail["name_hint"] == "Pixel de Ana" and entry.detail["ttl_minutes"] == 10
    assert not (await db.execute(select(Device))).first()  # creating the QR enrolls nothing


def test_routes_declare_their_scopes():
    from app.main import app

    scopes = {(route.path, method): route_scopes(route) for route in api_routes(app) for method in route.methods}
    assert scopes[("/api/v1/devices/enroll-qr", "POST")] == ["admin"]
    # Public on purpose, like /devices/enroll: a marker scope keeps "exactly one scope per route" true.
    assert scopes[("/api/v1/enroll/view/{token}", "GET")] == ["enroll_view"]
