"""Receipts by photo and utility bills (phases 15 and 17).

Every image is built here from invented bytes (no library, no real photo) and the vision model, the text model and
Firefly are fakes; nothing leaves the process. Registering the routers and the three Processor hooks is ServerQuality's
job (see ``docs/contract-additions/ServerCapture.md``): the hook-driven tests call the same functions directly.
"""
import importlib.util
import json
import struct
import zlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text

from app.api.routers import receipts as receipts_router
from app.clients import vision
from app.db.models import Bill, BillCandidate, BudgetMap, InboxItem, Receipt
from app.db.models_alerts import Alert
from app.services import ai_features, bills, image_clean, receipts
from tests.test_devices_v4 import API
from tests.test_ledger_v4 import ADMIN, FireflyLedger, make_inbox
from tests.test_llm_v3 import completion
from tests.test_llm_v3 import settings as llm_settings

MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "20261125_14_capture.py"



# --- Synthetic images -----------------------------------------------------------------------------------------------

def png(*extra: tuple[bytes, bytes]) -> bytes:
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    header = struct.pack(">IIBBBBB", 4, 4, 8, 0, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\xff" * 4 for _ in range(4))
    chunks = [chunk(b"IHDR", header), *(chunk(kind, payload) for kind, payload in extra), chunk(b"IDAT", zlib.compress(rows)), chunk(b"IEND", b"")]
    return b"\x89PNG\r\n\x1a\n" + b"".join(chunks)


def jpeg(orientation: int = 1, *, gps: bool = True) -> bytes:
    def segment(marker, body):
        return b"\xff" + bytes([marker]) + struct.pack(">H", len(body) + 2) + body

    tiff = b"MM\x00\x2a\x00\x00\x00\x08" + b"\x00\x01" + b"\x01\x12\x00\x03\x00\x00\x00\x01" + orientation.to_bytes(2, "big") + b"\x00\x00" + b"\x00\x00\x00\x00"
    exif = b"Exif\x00\x00" + tiff + (b"GPSLatitude 10.3910 N GPSLongitude 75.4794 W SerialNumber ABC123" if gps else b"")
    frame = b"\x08\x00\x04\x00\x04\x01\x01\x11\x00"
    scan = b"\x01\x01\x00\x00\x3f\x00"
    return (b"\xff\xd8" + segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00") + segment(0xE1, exif) + segment(0xED, b"Photoshop 3.0 secret")
            + segment(0xFE, b"comment with a name") + segment(0xDB, b"\x00" + bytes(64)) + segment(0xC0, frame) + segment(0xDA, scan)
            + b"\x12\x34\xff\x00\x56" + b"\xff\xd9")


def webp() -> bytes:
    def chunk(kind, payload):
        return kind + struct.pack("<I", len(payload)) + payload + (b"\x00" if len(payload) % 2 else b"")

    body = b"WEBP" + chunk(b"VP8X", bytes([0x3C]) + bytes(9)) + chunk(b"ICCP", b"profile") + chunk(b"VP8 ", bytes(range(10))) + chunk(b"EXIF", b"GPS here") + chunk(b"XMP ", b"<x:xmpmeta/>")
    return b"RIFF" + struct.pack("<I", len(body)) + body


# --- Image sanitizer ------------------------------------------------------------------------------------------------

def test_png_loses_text_time_and_exif_chunks_but_keeps_the_picture():
    original = png((b"tEXt", b"Location\x0010.39,-75.48"), (b"eXIf", b"MM GPS"), (b"tIME", bytes(7)), (b"iCCP", b"p\x00\x00zz"))
    clean, mime = image_clean.sanitize(original)
    assert mime == "image/png" and clean.startswith(b"\x89PNG")
    for leaked in (b"tEXt", b"eXIf", b"tIME", b"iCCP", b"Location", b"GPS"):
        assert leaked not in clean
    assert b"IHDR" in clean and b"IDAT" in clean and clean.endswith(b"IEND\xaeB`\x82")


def test_jpeg_loses_gps_and_comments_and_keeps_only_the_orientation():
    clean, mime = image_clean.sanitize(jpeg(orientation=6))
    assert mime == "image/jpeg"
    for leaked in (b"GPS", b"SerialNumber", b"Photoshop", b"comment", b"JFIF"):
        assert leaked not in clean
    assert clean.startswith(b"\xff\xd8") and clean.endswith(b"\x12\x34\xff\x00\x56\xff\xd9")
    assert image_clean._orientation(clean[clean.index(b"Exif\x00\x00") + 6:clean.index(b"Exif\x00\x00") + 6 + 26]) == 6
    upright, _ = image_clean.sanitize(jpeg(orientation=1))
    assert b"Exif" not in upright and b"GPS" not in upright


def test_webp_loses_exif_xmp_and_icc():
    clean, mime = image_clean.sanitize(webp())
    assert mime == "image/webp"
    for leaked in (b"EXIF", b"XMP ", b"ICCP", b"GPS"):
        assert leaked not in clean
    assert clean[12:16] == b"VP8X" and clean[20] == 0x10  # alpha kept, ICC/EXIF/XMP flags cleared
    assert int.from_bytes(clean[4:8], "little") == len(clean) - 8


@pytest.mark.parametrize("data", [b"%PDF-1.4 not an image", b"GIF89a....", b"<html>photo.jpg</html>", b"\xff\xd8\xff\xe0 truncated", b"\x89PNG\r\n\x1a\n" + b"x" * 30])
def test_real_type_is_checked_by_magic_bytes_and_broken_files_are_rejected(data):
    with pytest.raises(image_clean.ImageRejectedError):
        image_clean.sanitize(data)


# --- Vision client --------------------------------------------------------------------------------------------------

def answer(**changes):
    values = {
        "es_recibo": True, "comercio": "Supermercado Central", "nit": "NIT 900.123.456-7", "fecha": "2026-11-06", "total": 40000,
        "impuestos": 3000, "propina": None, "medio_pago": "efectivo",
        "items": [
            {"descripcion": "Leche", "cantidad": 2, "valor": 30000, "categoria": "Alimentación"},
            {"descripcion": "Entrada cine", "cantidad": 1, "valor": 10000, "categoria": "ocio"},
        ],
    }
    return values | changes


def test_receipt_json_is_validated_strictly():
    parsed = vision.parse_receipt(answer(), ["Alimentación", "Ocio"], date(2026, 11, 24))
    assert parsed["total"] == Decimal("40000.00") and parsed["nit"] == "900123456-7"
    assert parsed["date"] == date(2026, 11, 6) and parsed["payment_method"] == "efectivo"
    assert [row["category"] for row in parsed["items"]] == ["Alimentación", "Ocio"]
    assert vision.parse_receipt({"es_recibo": False}, [], date(2026, 11, 24)) == {"not_a_receipt": True}
    odd = vision.parse_receipt(answer(fecha="2031-01-01", nit="hola", medio_pago="bitcoin", items=[{"descripcion": "Pan", "valor": 1000, "categoria": "Inventada"}]), ["Ocio"], date(2026, 11, 24))
    assert odd["date"] is None and odd["nit"] is None and odd["payment_method"] is None and odd["items"][0]["category"] is None
    for bad in (answer(total="12.345"), answer(total=-5), answer(total=True), answer(items="x"), answer(items=[{"valor": 5}]), answer(items=[{"descripcion": "a", "valor": "1e3x"}]), []):
        with pytest.raises(ValueError):
            vision.parse_receipt(bad, [], date(2026, 11, 24))


async def test_vision_sends_a_data_uri_and_falls_back_to_the_second_model():
    seen, usage = [], {}

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if payload["model"] == "deepseek-v4.1-flash":
            return httpx.Response(500, text="secret upstream body")
        return completion(answer())

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        parsed, model = await vision.read_receipt(b"\x89PNG-clean", "image/png", ["Ocio"], "cycle", date(2026, 11, 24), usage, settings=llm_settings(), http=http)
    finally:
        await http.aclose()
    assert model == "mimo-v2.6-flash" and usage["calls"] == 2 and usage["model"] == "mimo-v2.6-flash"
    assert [payload["model"] for payload in seen] == ["deepseek-v4.1-flash", "mimo-v2.6-flash"]
    user = seen[0]["messages"][1]["content"]
    image = next(part for part in user if part["type"] == "image_url")["image_url"]["url"]
    assert image.startswith("data:image/png;base64,") and seen[0]["response_format"] == {"type": "json_object"}
    assert "no confiables" in seen[0]["messages"][0]["content"] and parsed["merchant"] == "Supermercado Central"


async def test_vision_unavailable_when_every_model_fails_or_answers_garbage():
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: completion({"total": "mucho"})))
    try:
        with pytest.raises(vision.VisionUnavailableError):
            await vision.read_receipt(b"x", "image/png", [], "cycle", date(2026, 11, 24), settings=llm_settings(), http=http)
    finally:
        await http.aclose()


# --- Pure receipt rules ---------------------------------------------------------------------------------------------

def line_items(*amounts, categories=None):
    return [{"description": f"L{i}", "quantity": Decimal(1), "amount": Decimal(amount), "category": (categories or [None] * len(amounts))[i]} for i, amount in enumerate(amounts)]


def test_items_must_add_up_to_the_total_with_rounding_tolerance_and_tax_or_tip_included_or_not():
    assert check_ok(line_items("30000", "10000"), "40000", None, None)
    assert check_ok(line_items("30000", "9999.50"), "40000", None, None)  # rounding
    assert check_ok(line_items("30000", "7000"), "40000", "3000", None)  # prices without tax
    assert check_ok(line_items("30000", "6000"), "40000", None, "4000")  # prices without tip
    assert not check_ok(line_items("30000", "5000"), "40000", None, None)
    assert receipts.check_items([], Decimal("40000"), None, None)["ok"] is None


def check_ok(items, total, tax, tip):
    return receipts.check_items(items, Decimal(total), Decimal(tax) if tax else None, Decimal(tip) if tip else None)["ok"]


def test_split_scales_groups_to_the_movement_total_and_adds_up_to_the_cent():
    items = line_items("30000", "10000", "3333.33", categories=["Alimentación", "Ocio", "Ocio"])
    split = receipts.propose_split(items, Decimal("44000.00"))
    assert [row["category"] for row in split] == ["Alimentación", "Ocio"]
    assert sum(row["amount"] for row in split) == Decimal("44000.00") and split[0]["items"] == [0]
    assert receipts.propose_split(line_items("5", "6", categories=["Ocio", "Ocio"]), Decimal("11")) is None
    assert receipts.propose_split(line_items("5", "6", categories=["Ocio", None]), Decimal("11"), fallback="Ocio") is None
    assert len(receipts.propose_split(line_items("5", "6", categories=["Ocio", None]), Decimal("11"), fallback="Hogar")) == 2


# --- Receipts through the API ---------------------------------------------------------------------------------------

def moment(days_ago=1):
    return datetime.now(UTC).replace(hour=17, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)


def parsed_receipt(**changes):
    values = {
        "merchant": "SUPERMERCADO CENTRAL", "nit": "9001234567", "date": moment().date(), "total": Decimal("40000.00"),
        "tax": Decimal("3000.00"), "tip": None, "payment_method": "efectivo",
        "items": [
            {"description": "Leche", "quantity": Decimal("2.000"), "amount": Decimal("30000.00"), "category": "Alimentación"},
            {"description": "Entrada cine", "quantity": Decimal("1.000"), "amount": Decimal("10000.00"), "category": "Ocio"},
        ],
    }
    return values | changes


@pytest.fixture(name="fake")
def ledger_stand_in(clients):
    """Same stateful Firefly stand-in as ``test_ledger_v4``'s ``fake`` fixture (own copy: importing a fixture shadows its parameter name)."""
    firefly, _ = clients
    firefly.accounts.return_value = [*firefly.accounts.return_value, {"id": "6", "name": "Tarjeta Nu", "type": "liabilities"}]
    firefly.categories.return_value = ["Alimentación", "Transporte", "Ocio"]
    return FireflyLedger(firefly, firefly.accounts.return_value, budgets={"11": "Mercado", "12": "Ocio"}, bills={"21": "Internet", "22": "Arriendo"})


@pytest.fixture
def seen_images():
    return []


@pytest.fixture
def reader(monkeypatch, seen_images):
    """The vision model: records what it was given and answers with ``reader.answer`` (or raises ``reader.error``)."""
    state = type("State", (), {"answer": parsed_receipt(), "error": None, "calls": 0})()

    async def read(image, mime, categories, cycle_id, today, usage=None, **kwargs):
        state.calls += 1
        seen_images.append((image, mime, list(categories)))
        if usage is not None:
            usage["calls"] = 1
        if state.error:
            raise state.error
        return state.answer, "deepseek-v4.1-flash"

    monkeypatch.setattr(receipts.vision, "read_receipt", read)
    return state


@pytest.fixture
def run(monkeypatch, db, clients, reader):
    record = AsyncMock()
    monkeypatch.setattr(receipts.ai_budget, "record", record)

    async def background(receipt_id, image, mime):
        await receipts.process(db, clients[0], receipt_id, image, mime)

    monkeypatch.setattr(receipts_router, "process_background", background)
    return record


async def upload(client, data, *, item_id=None, name="recibo.jpg", headers=ADMIN, content_type="image/jpeg"):
    form = {"item_id": str(item_id)} if item_id is not None else {}
    return await client.post(f"{API}/receipts", headers=headers, files={"file": (name, data, content_type)}, data=form)


async def receipt(client, receipt_id):
    response = await client.get(f"{API}/receipts/{receipt_id}", headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


async def expense(service, fake, *, amount="40000.00", **changes):
    """A registered expense that also lives in the fake ledger; returns the inbox item."""
    when = changes.pop("occurred_at", moment())
    group = fake.seed(amount=amount, date=f"{when.date()}T12:00:00-05:00", external_id=f"fa:{changes.get('source_ref', 'sms-1')}", category="Alimentación")
    return await make_inbox(service, amount=Decimal(amount), occurred_at=when, firefly_transaction_id=group, **changes)


async def test_upload_reads_the_receipt_without_metadata_and_matches_the_expense(client, run, reader, seen_images, service, fake, db):
    item = await expense(service, fake)
    response = await upload(client, png((b"tEXt", b"GPS 10.39,-75.48"), (b"eXIf", b"MM GPS")), name="recibo.png", content_type="image/png")
    assert response.status_code == 202, response.text
    assert response.json() == {"id": 1, "status": "processing"}

    image, mime, categories = seen_images[0]
    assert mime == "image/png" and b"GPS" not in image and b"tEXt" not in image and b"eXIf" not in image
    assert categories == ["Alimentación", "Transporte", "Ocio"]
    run.assert_awaited_once()
    assert run.await_args.args[1:3] == ("receipt_vision", 1) and run.await_args.kwargs == {"auto": False}

    body = await receipt(client, 1)
    assert body["status"] == "ready" and body["reason"] is None and body["model"] == "deepseek-v4.1-flash"
    assert (body["merchant"], body["nit"], body["total"], body["tax"], body["payment_method"]) == ("SUPERMERCADO CENTRAL", "9001234567", "40000.00", "3000.00", "efectivo")
    assert body["items"][0] == {"description": "Leche", "quantity": "2", "amount": "30000.00", "category": "Alimentación"}
    assert body["items_check"]["ok"] is True and body["items_check"]["sum"] == "40000.00"
    assert body["match"]["kind"] == "matched" and body["match"]["item"]["id"] == item.id
    assert body["split_suggestion"] == {"total": "40000.00", "splits": [
        {"category": "Alimentación", "amount": "30000.00", "items": [0]}, {"category": "Ocio", "amount": "10000.00", "items": [1]}]}
    listing = (await client.get(f"{API}/receipts", headers=ADMIN)).json()
    assert listing["total"] == 1 and listing["items"][0]["item_id"] == item.id and "items" not in listing["items"][0]


async def test_only_the_structured_result_is_stored_and_it_is_encrypted(client, run, db):
    await upload(client, jpeg())
    raw = (await db.execute(text("SELECT * FROM receipts"))).mappings().one()
    blob = "\n".join(str(value) for value in raw.values())
    assert raw["data"].startswith("enc:v1:")
    for leaked in ("SUPERMERCADO", "9001234567", "Leche", "GPS", "Exif"):
        assert leaked not in blob
    assert not {"image", "file", "content", "bytes"} & {column.name for column in Receipt.__table__.columns}


async def test_same_image_is_idempotent_and_failed_ones_are_retried(client, run, reader):
    first, second = await upload(client, jpeg()), await upload(client, jpeg())
    assert first.json()["id"] == second.json()["id"] == 1 and second.json()["status"] == "ready" and reader.calls == 1
    reader.error = vision.VisionUnavailableError("down")
    other = await upload(client, jpeg(orientation=3))
    assert (await receipt(client, other.json()["id"]))["reason"] == "vision_unavailable"
    reader.error = None
    again = await upload(client, jpeg(orientation=3))
    assert again.json() == {"id": other.json()["id"], "status": "processing"}
    assert (await receipt(client, other.json()["id"]))["status"] == "ready"


@pytest.mark.parametrize(("data", "status", "code"), [
    (b"", 422, "empty_file"), (b"%PDF-1.4 hola", 422, "unsupported_image_type"),
    (b"\xff\xd8\xff" + bytes(6 * 1024 * 1024), 413, "payload_too_large"), (b"\xff\xd8\xff\xe0\x00\x10junk", 422, "image_corrupt"),
])
async def test_upload_validation(client, run, data, status, code):
    response = await upload(client, data, name="foto.jpg", content_type="image/jpeg")
    assert response.status_code == status and response.json()["code"] == code
    assert (await client.get(f"{API}/receipts", headers=ADMIN)).json()["total"] == 0


async def test_upload_needs_the_review_scope_and_a_linkable_expense(client, run, service, fake, db):
    assert (await upload(client, jpeg(), headers={})).status_code == 401
    assert (await upload(client, jpeg(), item_id=999)).json()["code"] == "item_not_found"
    deposit = await make_inbox(service, source_ref="dep-1", transaction_type="deposit", firefly_transaction_id="1")
    assert (await upload(client, jpeg(), item_id=deposit.id)).json()["code"] == "item_not_linkable"
    item = await expense(service, fake)
    linked = await upload(client, jpeg(), item_id=item.id)
    assert linked.status_code == 202
    body = await receipt(client, linked.json()["id"])
    assert body["match"]["kind"] == "linked" and body["match"]["item"]["id"] == item.id
    assert (await upload(client, jpeg(orientation=8), item_id=item.id)).json()["code"] == "item_has_receipt"


async def test_matching_accepts_a_tip_and_two_days_and_rejects_other_merchants_or_dates(client, run, reader, service, fake):
    tipped = await expense(service, fake, amount="44000.00", source_ref="tip", occurred_at=moment(3))  # 10 % tip, 2 days after the receipt
    reader.answer = parsed_receipt(date=moment(1).date())
    other = await upload(client, jpeg())
    assert (await receipt(client, other.json()["id"]))["match"]["item"]["id"] == tipped.id

    far = await expense(service, fake, amount="55000.00", source_ref="far", occurred_at=moment(10))
    reader.answer = parsed_receipt(total=Decimal("55000.00"), date=moment(1).date(), items=[])
    result = await receipt(client, (await upload(client, jpeg(orientation=2))).json()["id"])
    assert result["match"] == {"kind": None, "item": None, "candidates": []} and far.id

    reader.answer = parsed_receipt(total=Decimal("20000.00"), merchant="FARMACIA LA ESQUINA", items=[])
    await expense(service, fake, amount="20000.00", source_ref="pharm", merchant="DROGUERIA OTRA")
    result = await receipt(client, (await upload(client, jpeg(orientation=3))).json()["id"])
    assert result["match"]["item"] is None


async def test_two_equally_good_expenses_are_candidates_not_a_match(client, run, service, fake):
    first = await expense(service, fake, source_ref="a")
    second = await expense(service, fake, source_ref="b")
    body = await receipt(client, (await upload(client, jpeg())).json()["id"])
    assert body["match"]["item"] is None and set(body["match"]["candidates"]) == {first.id, second.id}


async def test_totals_that_do_not_add_up_need_review_and_offer_no_split(client, run, reader):
    reader.answer = parsed_receipt(items=[{"description": "Leche", "quantity": Decimal(1), "amount": Decimal("5000.00"), "category": "Ocio"}])
    body = await receipt(client, (await upload(client, jpeg())).json()["id"])
    assert (body["status"], body["reason"], body["items_check"]["ok"], body["split_suggestion"]) == ("needs_review", "items_total_mismatch", False, None)


async def test_not_a_receipt_and_missing_date(client, run, reader):
    reader.answer = {"not_a_receipt": True}
    body = await receipt(client, (await upload(client, jpeg())).json()["id"])
    assert (body["status"], body["reason"]) == ("failed", "not_a_receipt")
    reader.answer = parsed_receipt(date=None)
    body = await receipt(client, (await upload(client, jpeg(orientation=4))).json()["id"])
    assert (body["status"], body["reason"]) == ("needs_review", "date_missing")


async def test_apply_split_rewrites_the_movement_by_category_and_can_be_undone(client, run, service, fake, db):
    item = await expense(service, fake)
    receipt_id = (await upload(client, jpeg())).json()["id"]
    response = await client.post(f"{API}/receipts/{receipt_id}/apply", headers=ADMIN, json={"split": True})
    assert response.status_code == 200, response.text
    body = response.json()
    journals = fake.journals(item.firefly_transaction_id)
    assert sorted((j["category_name"], j["amount"]) for j in journals) == [("Alimentación", "30000.000000000000"), ("Ocio", "10000.000000000000")]
    assert body["created"] is None and body["receipt"]["applied"] is True and body["receipt"]["split_suggestion"] is None
    assert body["receipt"]["applied_result"]["split"] is True and body["change_id"] == body["receipt"]["applied_result"]["change_id"]
    again = await client.post(f"{API}/receipts/{receipt_id}/apply", headers=ADMIN, json={"split": True})
    assert again.status_code == 409 and again.json()["code"] == "already_applied"
    undone = await client.post(f"{API}/ledger/changes/{body['change_id']}/undo", headers=ADMIN)
    assert undone.status_code == 200 and len(fake.journals(item.firefly_transaction_id)) == 1


async def test_apply_split_spreads_a_tip_over_the_groups(client, run, service, fake):
    item = await expense(service, fake, amount="44000.00", occurred_at=moment(2))
    await upload(client, jpeg(), item_id=item.id)
    response = await client.post(f"{API}/receipts/1/apply", headers=ADMIN, json={"split": True})
    assert response.status_code == 200, response.text
    assert sum(Decimal(j["amount"]) for j in fake.journals(item.firefly_transaction_id)) == Decimal("44000")


async def test_apply_without_a_movement_cannot_split_and_validates_the_body(client, run, reader, fake):
    reader.answer = parsed_receipt(merchant="TIENDA SIN GASTO")
    receipt_id = (await upload(client, jpeg())).json()["id"]
    split = await client.post(f"{API}/receipts/{receipt_id}/apply", headers=ADMIN, json={"split": True})
    assert split.status_code == 409 and split.json()["code"] == "no_movement"
    assert (await client.post(f"{API}/receipts/{receipt_id}/apply", headers=ADMIN, json={})).json()["code"] == "nothing_to_apply"
    assert (await client.post(f"{API}/receipts/{receipt_id}/apply", headers=ADMIN, json={"split": True, "extra": 1})).status_code == 422
    assert (await client.post(f"{API}/receipts/77/apply", headers=ADMIN, json={"split": True})).json()["code"] == "receipt_not_found"


async def test_apply_creates_a_cash_expense_when_the_owner_asks(client, run, reader, clients, add_own_account, fake, db):
    cash = {"id": "7", "name": "Efectivo", "type": "asset"}
    clients[0].accounts.return_value = [*clients[0].accounts.return_value, cash]
    fake.accounts["7"] = cash
    await add_own_account("Efectivo", bank="cash", kind="cash")
    reader.answer = parsed_receipt(merchant="Tienda de barrio", total=Decimal("15000.00"), tax=None, items=[
        {"description": "Pan", "quantity": Decimal(3), "amount": Decimal("15000.00"), "category": "Alimentación"}])
    receipt_id = (await upload(client, jpeg())).json()["id"]
    body = (await receipt(client, receipt_id))
    assert body["match"]["item"] is None and body["suggested_category"] == "Alimentación"
    response = await client.post(f"{API}/receipts/{receipt_id}/apply", headers=ADMIN, json={"create_cash": True})
    assert response.status_code == 200, response.text
    created = response.json()["created"]
    item = await db.get(InboxItem, created["inbox_id"])
    assert item.status == "created" and item.amount == Decimal("15000.00") and item.asset_account == "Efectivo" and item.category == "Alimentación"
    assert item.source_ref == f"manual:receipt:{receipt_id}" and item.merchant == "TIENDA DE BARRIO"
    assert (await receipt(client, receipt_id))["match"]["kind"] == "cash"
    assert (await client.post(f"{API}/receipts/{receipt_id}/apply", headers=ADMIN, json={"create_cash": True})).json()["code"] == "already_applied"


async def test_apply_link_and_delete(client, run, service, fake, reader):
    item = await expense(service, fake, amount="41000.00", source_ref="other")  # does not match by itself (tip too large for the total)
    reader.answer = parsed_receipt(total=Decimal("30000.00"), items=[], merchant="OTRO COMERCIO")
    receipt_id = (await upload(client, jpeg())).json()["id"]
    assert (await receipt(client, receipt_id))["match"]["item"] is None
    linked = await client.post(f"{API}/receipts/{receipt_id}/apply", headers=ADMIN, json={"item_id": item.id})
    assert linked.status_code == 200 and linked.json()["receipt"]["match"]["kind"] == "linked" and linked.json()["receipt"]["applied"] is False
    assert (await client.delete(f"{API}/receipts/{receipt_id}", headers=ADMIN)).json() == {"deleted": True}
    assert (await client.get(f"{API}/receipts/{receipt_id}", headers=ADMIN)).json()["code"] == "receipt_not_found"
    assert (await client.delete(f"{API}/receipts/{receipt_id}", headers=ADMIN)).status_code == 404


async def test_purge_fails_stuck_receipts_and_deletes_old_ones(db):
    now = datetime.now(UTC)
    db.add_all([
        Receipt(status="processing", image_sha256="a" * 64, created_at=now - timedelta(hours=2)),
        Receipt(status="ready", image_sha256="b" * 64, created_at=now - timedelta(days=receipts.RETENTION_DAYS + 1)),
        Receipt(status="ready", image_sha256="c" * 64, created_at=now),
    ])
    await db.commit()
    assert await receipts.purge(db) == 1
    rows = {row.image_sha256[0]: row for row in (await db.scalars(select(Receipt).execution_options(populate_existing=True))).all()}
    assert set(rows) == {"a", "c"} and (rows["a"].status, rows["a"].reason) == ("failed", "interrupted")


# --- Bills: helpers -------------------------------------------------------------------------------------------------

def today():
    return bills.local_today()


def bill_body(**changes):
    return {"entity": "Aguas de Cartagena S.A. E.S.P.", "service": "agua", "amount": "85400.00", "due_date": (today() + timedelta(days=5)).isoformat(), **changes}


async def add_bill(client, **changes):
    response = await client.post(f"{API}/bills", headers=ADMIN, json=bill_body(**changes))
    assert response.status_code == 201, response.text
    return response.json()


BILL_EMAIL = """Aguas de Cartagena S.A. E.S.P. - NIT 890.399.011-3
Estimado cliente: su factura de servicios del periodo de noviembre ya esta disponible.
Referencia de pago: 1234567890
Valor a pagar: $85.400
Fecha limite de pago: {due}
Realice su pago oportuno en linea y evite la suspension del servicio."""


async def bill_email(db, *, source_ref="mail-1", sender="facturas@aguasdecartagena.com.co", received=None, body=None):
    received = received or datetime.now(UTC) - timedelta(days=5)
    due = (today() + timedelta(days=5)).strftime("%d/%m/%Y")
    item = InboxItem(
        source="email", source_ref=source_ref, sender=sender, subject_or_title="Factura de agua noviembre", raw_text=body or BILL_EMAIL.format(due=due),
        occurred_at=received, status="ignored", method="parser",
    )
    db.add(item)
    await db.commit()
    return item


def extraction(**changes):
    return {
        "es_factura": True, "entidad": "Aguas de Cartagena S.A. E.S.P.", "servicio": "agua", "valor_a_pagar": 85400,
        "fecha_limite": (today() + timedelta(days=5)).isoformat(), "fecha_emision": (today() - timedelta(days=5)).isoformat(),
        "periodo": "2026-11", "referencia": "1234567890", **changes,
    }


@pytest.fixture
def text_model(clients, monkeypatch):
    llm = clients[1]
    llm.complete_json = AsyncMock(return_value=extraction())
    monkeypatch.setattr(ai_features.ai_budget, "allow", AsyncMock(return_value=True))
    monkeypatch.setattr(ai_features.ai_budget, "record", AsyncMock())
    return llm


# --- Bills: e-mail source, payment match ----------------------------------------------------------------------------

async def test_bill_email_becomes_a_pending_bill_and_a_pse_payment_settles_it(db, service, clients, settings, text_model, fake):
    item = await bill_email(db)
    assert await bills.consider_ignored(db, item) is True
    await db.commit()
    stored = (await db.execute(text("SELECT text FROM bill_candidates"))).scalar_one()
    assert stored.startswith("enc:v1:")

    counts = await bills.extract_pending(db, clients[0], text_model, settings)
    assert counts["created"] == 1 and counts["stopped"] is None
    sent = text_model.complete_json.await_args.args[1]["UNTRUSTED_DATA"]
    assert "Valor a pagar" in sent and "1234567890" not in sent  # the account-like number went through ``redact``
    candidate = await db.scalar(select(BillCandidate))
    assert candidate.status == "done" and candidate.text is None

    (bill,) = (await client_list(db))
    assert (bill["entity"], bill["service"], bill["amount"], bill["status"], bill["reference"], bill["source"]) == (
        "AGUAS DE CARTAGENA", "agua", "85400.00", "pending", "****7890", "email")

    group = fake.seed(amount="85400.00", date=f"{datetime.now(UTC).astimezone(bills._zone()).date()}T12:00:00-05:00", description="Pago PSE a Aguas de Cartagena")
    payment = await make_inbox(
        service, source_ref="pse-1", amount=Decimal("85400.00"), occurred_at=datetime.now(UTC), merchant="AGUAS DE CARTAGENA",
        description="Pago PSE a Aguas de Cartagena", tags=["pse"], firefly_transaction_id=group,
    )
    await bills.on_item_created(service, payment)
    row = await db.scalar(select(Bill))
    assert (row.status, row.paid_item_id) == ("paid", payment.id) and row.paid_at is not None
    again = await bills.consider_ignored(db, item)
    assert again is False  # one candidate per e-mail


async def client_list(db):
    today_ = today()
    return [bills.bill_out(row, today_) for row in (await db.scalars(select(Bill))).all()]


async def test_the_payment_inherits_the_budget_learned_for_the_entity(db, service, fake):
    db.add(BudgetMap(kind="merchant", key="AGUAS DE CARTAGENA", budget="Mercado", streak=2, learned=True))
    await db.commit()
    group = fake.seed(amount="85000.00", date=f"{today()}T12:00:00-05:00")
    bill, _ = await bills._insert(db, {
        "entity": "AGUAS DE CARTAGENA", "entity_key": "aguas de cartagena", "service": "agua", "reference": None, "period": None,
        "amount": Decimal("85400.00"), "due_date": today() + timedelta(days=3), "issued_on": today() - timedelta(days=3)}, source="manual", source_item_id=None)
    await db.commit()
    payment = await make_inbox(service, source_ref="p", amount=Decimal("85000.00"), occurred_at=datetime.now(UTC), merchant="AGUAS DE CARTAGENA", firefly_transaction_id=group)
    await bills.on_item_created(service, payment)  # 85000 is within 1 % of 85400
    assert (await db.get(Bill, bill.id, populate_existing=True)).status == "paid"
    assert fake.journals(group)[0]["budget_name"] == "Mercado"


@pytest.mark.parametrize(("amount", "days", "merchant"), [
    ("86500.00", 0, "AGUAS DE CARTAGENA"),   # more than 1 % off
    ("85400.00", 40, "AGUAS DE CARTAGENA"),  # after due date + 10 days
    ("85400.00", -20, "AGUAS DE CARTAGENA"),  # before the issue date
    ("85400.00", 0, "TIENDA OTRA"),          # other entity
])
async def test_payments_outside_the_rule_do_not_settle_the_bill(db, service, fake, amount, days, merchant):
    await bills._insert(db, {
        "entity": "AGUAS DE CARTAGENA", "entity_key": "aguas de cartagena", "service": "agua", "reference": None, "period": None,
        "amount": Decimal("85400.00"), "due_date": today() + timedelta(days=5), "issued_on": today() - timedelta(days=5)}, source="manual", source_item_id=None)
    await db.commit()
    payment = await make_inbox(service, source_ref="x", amount=Decimal(amount), occurred_at=datetime.now(UTC) + timedelta(days=days), merchant=merchant)
    await bills.on_item_created(service, payment)
    assert (await db.scalar(select(Bill))).status == "pending"


async def test_non_bill_mail_is_not_queued_and_invalid_answers_are_discarded(db, clients, settings, text_model):
    promo = await bill_email(db, source_ref="promo", body="Gran oferta de internet: 50% de descuento este mes, aprovecha.")
    one_word = await bill_email(db, source_ref="one", body="Le recordamos que su factura llegara pronto.")
    sms = InboxItem(source="sms", source_ref="s", sender="x", raw_text=BILL_EMAIL.format(due="01/12/2026"), status="ignored")
    db.add(sms)
    await db.commit()
    assert [await bills.consider_ignored(db, row) for row in (promo, one_word, sms)] == [False, False, False]

    mail = await bill_email(db, source_ref="mail-2")
    await bills.consider_ignored(db, mail)
    await db.commit()
    for bad in (extraction(valor_a_pagar="12.345"), extraction(fecha_limite="2031-01-01"), extraction(entidad="")):
        text_model.complete_json.return_value = bad
        counts = await bills.extract_pending(db, clients[0], text_model, settings)
        assert counts["created"] == 0
    assert (await db.scalar(select(BillCandidate))).status == "failed"
    assert (await db.scalar(sa.select(sa.func.count()).select_from(Bill))) == 0


async def test_the_model_saying_it_is_not_a_bill_skips_the_candidate_and_a_blocked_budget_stops_the_run(db, clients, settings, text_model, monkeypatch):
    mail = await bill_email(db)
    await bills.consider_ignored(db, mail)
    await db.commit()
    monkeypatch.setattr(ai_features.ai_budget, "allow", AsyncMock(return_value=False))
    assert (await bills.extract_pending(db, clients[0], text_model, settings))["stopped"] == "budget"
    assert text_model.complete_json.await_count == 0 and (await db.scalar(select(BillCandidate))).status == "pending"
    monkeypatch.setattr(ai_features.ai_budget, "allow", AsyncMock(return_value=True))
    text_model.complete_json.side_effect = RuntimeError("down")
    assert (await bills.extract_pending(db, clients[0], text_model, settings))["stopped"] == "llm"
    text_model.complete_json.side_effect = None
    text_model.complete_json.return_value = {"es_factura": False}
    assert (await bills.extract_pending(db, clients[0], text_model, settings))["skipped"] == 1


async def test_job_enqueue_and_retention(db, monkeypatch):
    queue = AsyncMock()
    monkeypatch.setattr(bills.ai_jobs, "enqueue", queue)
    assert await bills.enqueue_extraction(db) is False and queue.await_count == 0
    mail = await bill_email(db)
    await bills.consider_ignored(db, mail)
    await db.commit()
    assert await bills.enqueue_extraction(db) is True
    args = queue.await_args
    assert args.args[1] == "bills_extract" and args.args[4] == "bills_extract" and args.kwargs["dedupe_key"] == "bills_extract"
    old = await db.scalar(select(BillCandidate))
    old.created_at = datetime.now(UTC) - timedelta(days=bills.CANDIDATE_RETENTION_DAYS + 1)
    await db.commit()
    assert await bills.purge_candidates(db) == 1


# --- Bills: manual registration and API -----------------------------------------------------------------------------

async def test_manual_bill_crud(client, db):
    created = await add_bill(client, reference="CONTRATO-98765432", period="2026-11", amount="85400.50")
    assert created["id"] == 1 and created["entity"] == "AGUAS DE CARTAGENA" and created["reference"] == "****5432"
    assert (created["amount"], created["currency"], created["status"], created["days_to_due"], created["source"], created["paid_item_id"]) == ("85400.50", "COP", "pending", 5, "manual", None)
    assert "98765432" not in json.dumps(created) and "98765432" not in " ".join(str(v) for v in (await db.execute(text("SELECT * FROM bills"))).one())
    dup = await client.post(f"{API}/bills", headers=ADMIN, json=bill_body(amount="85400.50"))
    assert dup.status_code == 409 and dup.json()["code"] == "bill_exists"
    for changes in ({"service": "tv"}, {"amount": "0"}, {"amount": "10.123"}, {"entity": "A"}, {"period": "noviembre"}, {"extra": 1}):
        assert (await client.post(f"{API}/bills", headers=ADMIN, json=bill_body(**changes))).status_code == 422, changes

    patched = await client.patch(f"{API}/bills/1", headers=ADMIN, json={"amount": "90000.00", "due_date": (today() - timedelta(days=1)).isoformat(), "service": "luz"})
    assert patched.status_code == 200 and (patched.json()["amount"], patched.json()["status"], patched.json()["service"]) == ("90000.00", "overdue", "luz")
    assert (await client.patch(f"{API}/bills/1", headers=ADMIN, json={"status": "paid"})).status_code == 422
    assert (await client.patch(f"{API}/bills/9", headers=ADMIN, json={"amount": "1"})).json()["code"] == "bill_not_found"
    assert (await client.delete(f"{API}/bills/1", headers=ADMIN)).json() == {"deleted": True}
    assert (await client.delete(f"{API}/bills/1", headers=ADMIN)).status_code == 404


async def test_listing_filters_by_status_and_month_and_mark_paid(client, service, fake, db):
    future = today() + timedelta(days=40)
    await add_bill(client)
    await add_bill(client, entity="Gases del Caribe", service="gas", amount="30000.00", due_date=(today() - timedelta(days=2)).isoformat())
    await add_bill(client, entity="Claro", service="telefonia", amount="60000.00", due_date=future.isoformat())

    async def listing(**params):
        response = await client.get(f"{API}/bills", headers=ADMIN, params=params)
        assert response.status_code == 200, response.text
        return response.json()

    assert (await listing())["total"] == 3
    assert [row["entity"] for row in (await listing(status="overdue"))["items"]] == ["GASES DEL CARIBE"]
    assert [row["entity"] for row in (await listing(status="pending"))["items"]] == ["AGUAS DE CARTAGENA", "CLARO"]
    assert [row["entity"] for row in (await listing(month=future.strftime("%Y-%m")))["items"]][-1:] == ["CLARO"]
    assert (await client.get(f"{API}/bills", headers=ADMIN, params={"month": "2026-13"})).status_code == 422
    assert (await client.get(f"{API}/bills", headers=ADMIN, params={"status": "nope"})).status_code == 422

    group = fake.seed(amount="30000.00", date=f"{today()}T12:00:00-05:00")
    payment = await make_inbox(service, source_ref="gas", amount=Decimal("30000.00"), merchant="GASES DEL CARIBE", occurred_at=datetime.now(UTC), firefly_transaction_id=group)
    paid = await client.post(f"{API}/bills/2/mark-paid", headers=ADMIN, json={"item_id": payment.id})
    assert paid.status_code == 200 and (paid.json()["status"], paid.json()["paid_item_id"]) == ("paid", payment.id)
    assert (await client.post(f"{API}/bills/2/mark-paid", headers=ADMIN, json={})).json()["code"] == "already_paid"
    other = await client.post(f"{API}/bills/1/mark-paid", headers=ADMIN, json={"item_id": payment.id})
    assert other.status_code == 409 and other.json()["code"] == "item_already_linked"
    assert (await client.post(f"{API}/bills/1/mark-paid", headers=ADMIN, json={"item_id": 999})).json()["code"] == "item_not_found"
    by_hand = await client.post(f"{API}/bills/1/mark-paid", headers=ADMIN, json={})
    assert by_hand.json()["status"] == "paid" and by_hand.json()["paid_item_id"] is None
    reopened = await client.patch(f"{API}/bills/1", headers=ADMIN, json={"status": "pending"})
    assert reopened.json()["status"] == "pending" and reopened.json()["paid_at"] is None
    assert [row["status"] for row in (await listing(status="paid"))["items"]] == ["paid"]


async def test_bills_need_scopes(client):
    assert (await client.get(f"{API}/bills", headers={})).status_code == 401
    assert (await client.post(f"{API}/bills", headers={}, json=bill_body())).status_code == 401


# --- Bills: reminders and recurrence --------------------------------------------------------------------------------

async def test_reminders_three_days_before_on_the_day_and_when_overdue_once_each(client, db):
    await add_bill(client, entity="Aguas Uno", due_date=(today() + timedelta(days=3)).isoformat())
    await add_bill(client, entity="Luz Dos", service="luz", amount="50000.00", due_date=today().isoformat())
    await add_bill(client, entity="Gas Tres", service="gas", amount="20000.00", due_date=(today() - timedelta(days=1)).isoformat())
    await add_bill(client, entity="Internet Cuatro", service="internet", amount="70000.00", due_date=(today() + timedelta(days=9)).isoformat())
    emitted = await bills.alert_check(db, today())
    assert sorted(alert.title for alert in emitted) == ["Factura vencida: GAS TRES", "Vence en 3 días: AGUAS UNO", "Vence hoy: LUZ DOS"]
    assert {alert.type for alert in emitted} == {"bill_due"} and all(alert.currency == "COP" for alert in emitted)
    vencida = next(alert for alert in emitted if alert.title.startswith("Factura vencida"))
    assert vencida.amount == Decimal("20000.00") and "$20.000 COP" in vencida.body and "venció el" in vencida.body
    assert await bills.alert_check(db, today()) == []  # nothing repeats

    tomorrow = await bills.alert_check(db, today() + timedelta(days=1))
    assert [alert.title for alert in tomorrow] == ["Factura vencida: LUZ DOS"]  # the day-after alert; the others were already sent
    statuses = {row.entity: row.status for row in (await db.scalars(select(Bill))).all()}
    assert statuses == {"AGUAS UNO": "pending", "LUZ DOS": "overdue", "GAS TRES": "overdue", "INTERNET CUATRO": "pending"}
    assert (await db.scalar(sa.select(sa.func.count()).select_from(Alert))) == 4


async def test_a_paid_bill_gets_no_reminders(client, db):
    await add_bill(client, due_date=today().isoformat())
    await client.post(f"{API}/bills/1/mark-paid", headers=ADMIN, json={})
    assert await bills.alert_check(db, today()) == []


async def test_recurrence_proposes_next_months_expected_amount_and_feeds_the_cash_flow(client):
    now = today()
    for back, amount in ((2, "80000.00"), (1, "90000.00"), (0, "100000.00")):
        await add_bill(client, due_date=bills._add_months(now.replace(day=10), -back, 10).isoformat(), amount=amount)
    for back in (1, 0):
        await add_bill(client, entity="Gas Natural", service="gas", amount="40000.00", due_date=bills._add_months(now.replace(day=20), -back, 20).isoformat())
    await add_bill(client, entity="Una sola vez", service="otro", amount="10000.00", due_date=now.isoformat())

    body = (await client.get(f"{API}/bills/upcoming", headers=ADMIN, params={"days": 60})).json()
    assert body["as_of"] == now.isoformat() and body["days"] == 60
    expected = {row["entity"]: row for row in body["expected"]}
    assert set(expected) == {"AGUAS DE CARTAGENA", "GAS NATURAL"}
    water = expected["AGUAS DE CARTAGENA"]
    assert water["expected_amount"] == "90000.00" and water["variability"] == "variable" and water["based_on"] == 3
    assert date.fromisoformat(water["expected_due_date"]).day == 10 and date.fromisoformat(water["expected_due_date"]) >= now
    assert expected["GAS NATURAL"]["expected_amount"] == "40000.00" and expected["GAS NATURAL"]["variability"] == "stable"
    totals = body["totals"]
    assert Decimal(totals["expected"]) == Decimal("130000.00")
    assert Decimal(totals["all"]) == Decimal(totals["overdue"]) + Decimal(totals["pending"]) + Decimal(totals["expected"])
    assert all(row["status"] != "paid" for row in body["items"])
    assert (await client.get(f"{API}/bills/upcoming", headers=ADMIN, params={"days": 0})).status_code == 422


async def test_recurrence_skips_months_already_billed_and_stale_entities(db):
    now = today()
    for back in (1, 0):
        await bills._insert(db, {"entity": "AL DIA", "entity_key": "al dia", "service": "luz", "reference": None, "period": None, "amount": Decimal("1000.00"),
                                 "due_date": bills._add_months(now.replace(day=5), -back + 1, 5), "issued_on": now - timedelta(days=3)}, source="manual", source_item_id=None)
    for back in (6, 5):
        await bills._insert(db, {"entity": "VIEJA", "entity_key": "vieja", "service": "luz", "reference": None, "period": None, "amount": Decimal("1000.00"),
                                 "due_date": bills._add_months(now.replace(day=5), -back, 5), "issued_on": now - timedelta(days=200)}, source="manual", source_item_id=None)
    await db.commit()
    assert await bills.proposals(db, now) == []


# --- Cycle hook and migration ---------------------------------------------------------------------------------------

async def test_after_cycle_sweeps_payments_alerts_queues_the_job_and_isolates_failures(db, service, clients, settings, fake, monkeypatch):
    queue = AsyncMock()
    monkeypatch.setattr(bills.ai_jobs, "enqueue", queue)
    await bills._insert(db, {"entity": "AGUAS DE CARTAGENA", "entity_key": "aguas de cartagena", "service": "agua", "reference": None, "period": None,
                             "amount": Decimal("85400.00"), "due_date": today() + timedelta(days=2), "issued_on": today() - timedelta(days=8)}, source="manual", source_item_id=None)
    await db.commit()
    group = fake.seed(amount="85400.00", date=f"{today()}T12:00:00-05:00")
    await make_inbox(service, source_ref="late", amount=Decimal("85400.00"), merchant="AGUAS DE CARTAGENA", occurred_at=datetime.now(UTC), firefly_transaction_id=group)
    results = await bills.after_cycle(db, clients[0], clients[1], settings)
    assert results["payments"] == 1 and results["alerts"] == 0 and results["job"] is False
    assert (await db.scalar(select(Bill))).status == "paid"

    monkeypatch.setattr(bills, "alert_check", AsyncMock(side_effect=RuntimeError("boom")))
    results = await bills.after_cycle(db, clients[0], clients[1], settings)
    assert results["alerts"] == {"error": "RuntimeError"} and "purge" in results


def test_migration_matches_the_models_and_is_chained():
    spec = importlib.util.spec_from_file_location("capture_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (module.revision, module.down_revision) == ("20261125_14", "20261124_13")
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        module.upgrade()
    inspector = sa.inspect(engine)
    for model in (Receipt, Bill, BillCandidate):
        assert {c["name"] for c in inspector.get_columns(model.__tablename__)} == {c.name for c in model.__table__.columns}
    assert any(constraint["column_names"] == ["entity_key", "due_date", "amount"] for constraint in inspector.get_unique_constraints("bills"))
    with pytest.raises(RuntimeError):
        module.downgrade()
    engine.dispose()
