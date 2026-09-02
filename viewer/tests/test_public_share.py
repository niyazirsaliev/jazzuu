import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, public_gateway, public_share
from app.public_gateway import create_app


def _payload():
    return {
        "name": "Встреча <script>alert(1)</script>",
        "start_at": "2026-08-15T12:00:00Z",
        "duration_ms": 123000,
        "summary": "# Итог\n<script>alert(2)</script> [bad](javascript:alert(3))",
        "transcript": "Обсудили <img src=x onerror=alert(4)>",
        "mindmap": {"name": "План", "children": [{"name": "Шаг"}]},
    }


def _create(tmp_path, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    audio = tmp_path / "source.mp3"
    audio.write_bytes(bytes(range(256)) * 4)
    now = kwargs.pop("now", None)
    create_kwargs = {} if now is None else {"now": now}
    return public_share.create(
        tmp_path / "store",
        "hmac-secret",
        "internal-rec-1",
        _payload(),
        ttl_seconds=kwargs.pop("ttl_seconds", 86400),
        toggles=kwargs.pop(
            "toggles",
            {
                "metadata": True,
                "summary": True,
                "transcript": True,
                "mindmap": True,
                "images": False,
                "audio": True,
            },
        ),
        audio_path=audio,
        **create_kwargs,
        **kwargs,
    )


def test_export_isolated_store_hashes_secret_and_enforces_ttl_and_limits(tmp_path):
    created = _create(tmp_path)
    store = tmp_path / "store"
    raw = (store / "shares.db").read_bytes()
    assert created["secret"].encode() not in raw
    assert b"internal-rec-1" not in raw
    assert created["url_path"] == f"/{created['share_id']}#{created['secret']}"

    exported = json.loads((store / "exports" / created["share_id"] / "content.json").read_text())
    assert "id" not in exported
    assert "source_path" not in exported
    assert exported["transcript"].startswith("Обсудили")
    assert (store / "exports" / created["share_id"] / "audio.mp3").stat().st_size == 1024

    with pytest.raises(ValueError, match="ttl"):
        _create(tmp_path / "bad-ttl", ttl_seconds=3601)
    with pytest.raises(public_share.ShareLimitError):
        _create(tmp_path / "active-limit", max_active=0)
    with pytest.raises(public_share.ShareLimitError):
        _create(tmp_path / "storage-limit", max_storage_bytes=10)


def test_expired_export_bytes_are_reclaimed_before_storage_admission(tmp_path):
    toggles = {
        "metadata": False,
        "summary": False,
        "transcript": True,
        "mindmap": False,
        "audio": False,
    }
    expired = _create(tmp_path, now=100, ttl_seconds=3600, toggles=toggles)
    store = tmp_path / "store"
    expired_directory = store / "exports" / expired["share_id"]
    reusable_bytes = (expired_directory / "content.json").stat().st_size

    replacement = _create(
        tmp_path,
        now=4000,
        ttl_seconds=3600,
        toggles=toggles,
        max_storage_bytes=reusable_bytes,
    )

    assert not expired_directory.exists()
    assert (store / "exports" / replacement["share_id"] / "content.json").is_file()


def test_orphaned_staging_directory_is_reclaimed_before_create(tmp_path):
    toggles = {
        "metadata": False,
        "summary": False,
        "transcript": True,
        "mindmap": False,
        "audio": False,
    }
    initial = _create(tmp_path, now=100, ttl_seconds=3600, toggles=toggles)
    store = tmp_path / "store"
    share_bytes = (
        store / "exports" / initial["share_id"] / "content.json"
    ).stat().st_size
    assert public_share.revoke(store, "hmac-secret", initial["share_id"], now=101) == 1
    orphan = store / "exports" / ".share-crashed"
    orphan.mkdir()
    (orphan / "audio.mp3").write_bytes(b"x" * share_bytes)

    replacement = _create(
        tmp_path,
        now=200,
        ttl_seconds=3600,
        toggles=toggles,
        max_storage_bytes=share_bytes,
    )

    assert not orphan.exists()
    assert (store / "exports" / replacement["share_id"] / "content.json").is_file()


def test_concurrent_creators_cannot_both_pass_storage_limit(tmp_path):
    toggles = {
        "metadata": False,
        "summary": False,
        "transcript": True,
        "mindmap": False,
        "audio": False,
    }
    initial = _create(tmp_path, now=100, ttl_seconds=3600, toggles=toggles)
    store = tmp_path / "store"
    one_share_bytes = (
        store / "exports" / initial["share_id"] / "content.json"
    ).stat().st_size
    assert public_share.revoke(store, "hmac-secret", initial["share_id"], now=101) == 1
    barrier = threading.Barrier(2)

    def create_one(recording_id):
        barrier.wait()
        try:
            return public_share.create(
                store,
                "hmac-secret",
                recording_id,
                _payload(),
                ttl_seconds=3600,
                toggles=toggles,
                now=200,
                max_storage_bytes=one_share_bytes,
            )
        except public_share.ShareLimitError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create_one, ("rec-a", "rec-b")))

    assert sum(result is not None for result in results) == 1
    assert len([path for path in (store / "exports").iterdir() if not path.name.startswith(".")]) == 1


def test_concurrent_staging_never_exceeds_physical_storage_limit(tmp_path, monkeypatch):
    store = tmp_path / "store"
    store.mkdir()
    toggles = {
        "metadata": False,
        "summary": False,
        "transcript": True,
        "mindmap": False,
        "audio": True,
    }
    sources = []
    for name in ("a.mp3", "b.mp3"):
        source = tmp_path / name
        source.write_bytes(b"a" * 1000)
        sources.append(source)

    first_staged = threading.Event()
    release_first = threading.Event()
    second_reached_connect = threading.Event()
    thread_context = threading.local()
    original_copy_bounded = public_share._copy_bounded
    original_connect = public_share._connect

    def tracked_connect(path):
        if getattr(thread_context, "is_second", False):
            second_reached_connect.set()
        return original_connect(path)

    def paused_copy(source, destination, byte_limit):
        result = original_copy_bounded(source, destination, byte_limit)
        if not first_staged.is_set():
            first_staged.set()
            assert release_first.wait(timeout=5)
        return result

    monkeypatch.setattr(public_share, "_connect", tracked_connect)
    monkeypatch.setattr(public_share, "_copy_bounded", paused_copy)

    def create_one(recording_id, audio_path):
        thread_context.is_second = recording_id == "rec-b"
        try:
            return public_share.create(
                store,
                "hmac-secret",
                recording_id,
                _payload(),
                ttl_seconds=3600,
                toggles=toggles,
                audio_path=audio_path,
                now=200,
                max_storage_bytes=1100,
            )
        except public_share.ShareLimitError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(create_one, "rec-a", sources[0])
        assert first_staged.wait(timeout=5)
        second = executor.submit(create_one, "rec-b", sources[1])
        assert second_reached_connect.wait(timeout=5)
        exports = store / "exports"
        observed_bytes = sum(
            candidate.stat().st_size
            for candidate in exports.rglob("*")
            if candidate.is_file()
        )
        release_first.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert observed_bytes <= 1100
    assert sum(result is not None for result in results) == 1


def test_audio_growth_during_staging_cannot_exceed_reserved_bytes(tmp_path, monkeypatch):
    store = tmp_path / "store"
    source = tmp_path / "source.mp3"
    source.write_bytes(b"a" * 1000)
    original_mkdtemp = public_share.tempfile.mkdtemp

    def grow_source_before_copy(*args, **kwargs):
        temporary = original_mkdtemp(*args, **kwargs)
        with source.open("ab") as audio:
            audio.write(b"b" * 1000)
        return temporary

    monkeypatch.setattr(public_share.tempfile, "mkdtemp", grow_source_before_copy)
    created = public_share.create(
        store,
        "hmac-secret",
        "rec-a",
        _payload(),
        ttl_seconds=3600,
        toggles={
            "metadata": False,
            "summary": False,
            "transcript": True,
            "mindmap": False,
            "audio": True,
        },
        audio_path=source,
        now=200,
        max_storage_bytes=1100,
    )

    exports = store / "exports"
    physical_bytes = sum(
        candidate.stat().st_size
        for candidate in exports.rglob("*")
        if candidate.is_file()
    )
    assert physical_bytes <= 1100
    assert (exports / created["share_id"] / "audio.mp3").stat().st_size == 1000


def test_content_toggles_export_only_selected_material(tmp_path):
    created = _create(
        tmp_path,
        toggles={
            "metadata": False,
            "summary": False,
            "transcript": True,
            "mindmap": False,
            "audio": False,
        },
    )
    directory = tmp_path / "store" / "exports" / created["share_id"]
    exported = json.loads((directory / "content.json").read_text())
    assert exported == {"transcript": "Обсудили <img src=x onerror=alert(4)>"}
    assert not (directory / "audio.mp3").exists()


def test_generic_landing_exchanges_fragment_for_scoped_secure_cookie(tmp_path):
    created = _create(tmp_path)
    app = create_app(
        tmp_path / "store",
        "hmac-secret",
        cookie_secure=True,
        public_base_url="https://share.example",
    )
    client = TestClient(app)

    landing = client.get(f"/{created['share_id']}")
    missing = client.get("/AAAAAAAAAAAAAAAAAAAAAAAA")
    assert landing.status_code == missing.status_code == 200
    assert landing.text == missing.text
    assert "Встреча" not in landing.text
    assert "history.replaceState" in landing.text
    assert "location.hash" in landing.text
    assert landing.headers["referrer-policy"] == "no-referrer"
    assert landing.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in landing.headers["content-security-policy"]
    assert "connect-src 'self'" in landing.headers["content-security-policy"]

    exchanged = client.post(
        f"/{created['share_id']}/session",
        json={"secret": created["secret"]},
        headers={"Origin": "https://share.example"},
    )
    assert exchanged.status_code == 204
    cookie = exchanged.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert f"Path=/{created['share_id']}" in cookie
    assert created["secret"] not in cookie
    assert "pd1_" in cookie
    assert created["secret"] not in exchanged.text

    cross_site = client.post(
        f"/{created['share_id']}/session",
        json={"secret": created["secret"]},
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert cross_site.status_code == 404


def test_magic_link_claims_once_binds_device_and_revoke_invalidates_it(tmp_path):
    created = _create(tmp_path)
    app = create_app(
        tmp_path / "store", "hmac-secret", cookie_secure=False,
        public_base_url="https://share.example",
    )
    browser_a = TestClient(app)
    browser_b = TestClient(app)
    claimed = browser_a.post(
        f"/{created['share_id']}/session", json={"secret": created["secret"]},
    )
    assert claimed.status_code == 204
    assert browser_a.get(f"/{created['share_id']}/content").status_code == 200
    assert browser_a.get(f"/{created['share_id']}/content").status_code == 200

    denied = browser_b.post(
        f"/{created['share_id']}/session", json={"secret": created["secret"]},
    )
    unknown = browser_b.post(
        "/AAAAAAAAAAAAAAAAAAAAAAAA/session", json={"secret": created["secret"]},
    )
    assert (denied.status_code, denied.text) == (unknown.status_code, unknown.text)
    assert browser_b.get(f"/{created['share_id']}/content").status_code == 404

    assert public_share.revoke(
        tmp_path / "store", "hmac-secret", created["share_id"],
    ) == 1
    assert browser_a.get(f"/{created['share_id']}/content").status_code == 404


def test_concurrent_magic_link_claim_has_exactly_one_winner_without_writing_export_store(tmp_path):
    created = _create(tmp_path)
    barrier = threading.Barrier(2)
    state = tmp_path / "gateway" / "device-auth.db"
    before = hashlib.sha256((tmp_path / "store" / "shares.db").read_bytes()).digest()

    def attempt():
        client = TestClient(create_app(
            tmp_path / "store", "hmac-secret", gateway_state=state,
            cookie_secure=False, public_base_url="https://share.example",
        ))
        barrier.wait()
        response = client.post(
            f"/{created['share_id']}/session", json={"secret": created["secret"]},
        )
        return response.status_code, client

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    winners = [client for status, client in results if status == 204]
    assert len(winners) == 1
    assert winners[0].get(f"/{created['share_id']}/content").status_code == 200
    assert state.is_file()
    assert hashlib.sha256((tmp_path / "store" / "shares.db").read_bytes()).digest() == before


def test_gateway_reveals_only_selected_share_and_safely_renders_text(tmp_path):
    created = _create(tmp_path)
    other = public_share.create(
        tmp_path / "store",
        "hmac-secret",
        "internal-rec-2",
        {"name": "Sibling secret", "transcript": "Never leak this"},
        ttl_seconds=86400,
        toggles={"metadata": True, "summary": False, "transcript": True, "mindmap": False, "audio": False},
    )
    client = TestClient(create_app(
        tmp_path / "store", "hmac-secret", cookie_secure=False,
        public_base_url="https://share.example",
    ))
    assert client.post(f"/{created['share_id']}/session", json={"secret": created["secret"]}).status_code == 204

    page = client.get(f"/{created['share_id']}/content")
    assert page.status_code == 200
    assert "Встреча" in page.text
    assert "Never leak this" not in page.text
    assert "internal-rec-1" not in page.text
    assert "<script>alert" not in page.text
    assert "&lt;img src=x onerror=alert(4)&gt;" in page.text
    assert "javascript:" not in page.text
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert "media-src 'self'" in page.headers["content-security-policy"]
    assert client.get(f"/{other['share_id']}/content").status_code == 404
    assert client.get("/api/recordings").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/%2e%2e/content").status_code == 404


def test_gateway_exports_and_serves_selected_mindmap_and_summary_card_png(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"safe-image" * 20
    mindmap = tmp_path / "source-mindmap.png"
    summary_card = tmp_path / "source-summary-card.png"
    mindmap.write_bytes(png)
    summary_card.write_bytes(png + b"card")
    created = _create(
        tmp_path,
        toggles={
            "metadata": True,
            "summary": False,
            "transcript": False,
            "mindmap": True,
            "images": True,
            "audio": False,
        },
        mindmap_path=mindmap,
        summary_card_path=summary_card,
    )
    exported = tmp_path / "store" / "exports" / created["share_id"]
    assert (exported / "mindmap.png").read_bytes() == png
    assert (exported / "summary-card.png").read_bytes() == png + b"card"

    client = TestClient(create_app(
        tmp_path / "store", "hmac-secret", cookie_secure=False,
        public_base_url="https://share.example",
    ))
    assert client.post(
        f"/{created['share_id']}/session", json={"secret": created["secret"]},
    ).status_code == 204
    content = client.get(f"/{created['share_id']}/content")
    assert content.status_code == 200
    assert f'/{created["share_id"]}/mindmap.png' in content.text
    assert f'/{created["share_id"]}/summary-card.png' in content.text
    assert "img-src 'self'" in content.headers["content-security-policy"]
    for filename in ("mindmap.png", "summary-card.png"):
        image = client.get(f"/{created['share_id']}/{filename}")
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert client.get("/AAAAAAAAAAAAAAAAAAAAAAAA/mindmap.png").status_code == 404


def test_gateway_orders_selected_content_audio_image_summary_map_transcript(tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"image" * 20
    mindmap = tmp_path / "map.png"
    summary_card = tmp_path / "card.png"
    mindmap.write_bytes(png)
    summary_card.write_bytes(png)
    created = _create(
        tmp_path,
        toggles={
            "metadata": True, "audio": True, "images": True,
            "summary": True, "mindmap": True, "transcript": True,
        },
        mindmap_path=mindmap,
        summary_card_path=summary_card,
    )
    client = TestClient(create_app(
        tmp_path / "store", "hmac-secret", cookie_secure=False,
        public_base_url="https://share.example",
    ))
    assert client.post(
        f"/{created['share_id']}/session", json={"secret": created["secret"]},
    ).status_code == 204

    page = client.get(f"/{created['share_id']}/content")
    positions = [
        page.text.index("<h2>Аудио</h2>"),
        page.text.index("<h2>Визуальная карточка</h2>"),
        page.text.index("<h2>Резюме</h2>"),
        page.text.index("<h2>Карта</h2>"),
        page.text.index("<h2>Транскрипт</h2>"),
    ]
    assert positions == sorted(positions)


def test_gateway_reads_multiple_isolated_export_stores_without_listing(tmp_path):
    owner = _create(tmp_path / "owner")
    tenant_b = public_share.create(
        tmp_path / "tenant-b" / "store",
        "tenant-b-secret",
        "tenant-b-rec-1",
        {**_payload(), "name": "Tenant B only"},
        ttl_seconds=3600,
        toggles={
            "metadata": True, "summary": False, "transcript": False,
            "mindmap": False, "images": False, "audio": False,
        },
    )
    client = TestClient(create_app(
        stores=[
            (tmp_path / "tenant-alpha" / "store", "hmac-secret"),
            (tmp_path / "tenant-b" / "store", "tenant-b-secret"),
        ],
        gateway_state=tmp_path / "gateway-state.db", cookie_secure=False,
        public_base_url="https://share.example",
    ))
    assert client.post(
        f"/{tenant_b['share_id']}/session", json={"secret": tenant_b["secret"]},
    ).status_code == 204
    tenant_b_page = client.get(f"/{tenant_b['share_id']}/content")
    assert tenant_b_page.status_code == 200
    assert "Tenant B only" in tenant_b_page.text
    assert "internal-rec-1" not in tenant_b_page.text
    assert client.get("/api/recordings").status_code == 404
    assert client.get("/search").status_code == 404
    assert client.post(
        f"/{owner['share_id']}/session", json={"secret": tenant_b["secret"]},
    ).status_code == 404


def test_invalid_expired_revoked_and_absent_are_opaque(tmp_path, monkeypatch):
    created = _create(tmp_path, now=100)
    client = TestClient(create_app(
        tmp_path / "store", "hmac-secret", cookie_secure=False,
        public_base_url="https://share.example",
    ))
    wrong = client.post(f"/{created['share_id']}/session", json={"secret": "wrong"})
    absent = client.post("/AAAAAAAAAAAAAAAAAAAAAAAA/session", json={"secret": "wrong"})
    assert (wrong.status_code, wrong.text) == (absent.status_code, absent.text)
    existing_content = client.get(f"/{created['share_id']}/content")
    absent_content = client.get("/AAAAAAAAAAAAAAAAAAAAAAAA/content")
    assert (existing_content.status_code, existing_content.text) == (
        absent_content.status_code,
        absent_content.text,
    )

    assert public_share.revoke(tmp_path / "store", "hmac-secret", created["share_id"], now=101) == 1
    revoked = client.post(f"/{created['share_id']}/session", json={"secret": created["secret"]})
    assert (revoked.status_code, revoked.text) == (wrong.status_code, wrong.text)

    monkeypatch.setattr(public_share.time, "time", lambda: 100 + 86401)
    expired_created = _create(tmp_path / "expired", now=100)
    expired_client = TestClient(create_app(
        tmp_path / "expired" / "store", "hmac-secret", cookie_secure=False,
        public_base_url="https://share.example",
    ))
    expired = expired_client.post(
        f"/{expired_created['share_id']}/session", json={"secret": expired_created["secret"]}
    )
    assert (expired.status_code, expired.text) == (wrong.status_code, wrong.text)


def test_reconcile_revokes_deleted_or_archived_source(tmp_path):
    kept = _create(tmp_path)
    removed = public_share.create(
        tmp_path / "store",
        "hmac-secret",
        "internal-rec-2",
        {"transcript": "gone"},
        ttl_seconds=86400,
        toggles={"metadata": False, "summary": False, "transcript": True, "mindmap": False, "audio": False},
        now=100,
    )
    assert public_share.reconcile_sources(
        tmp_path / "store", "hmac-secret", {"internal-rec-1"}, now=101
    ) == 1
    assert public_share.verify(tmp_path / "store", "hmac-secret", kept["share_id"], kept["secret"], now=102)
    assert public_share.verify(tmp_path / "store", "hmac-secret", removed["share_id"], removed["secret"], now=102) is None


def test_owner_can_list_active_shares_without_recovering_secrets(tmp_path):
    created = _create(tmp_path)
    active = public_share.list_recording(
        tmp_path / "store", "hmac-secret", "internal-rec-1"
    )
    assert active == [
        {
            "share_id": created["share_id"],
            "created_at": created["created_at"],
            "expires_at": created["expires_at"],
            "content": {
                "metadata": True,
                "summary": True,
                "transcript": True,
                "mindmap": True,
                "images": False,
                "audio": True,
            },
        }
    ]
    assert created["secret"] not in json.dumps(active)


def test_audio_range_is_bounded_and_never_cached(tmp_path):
    created = _create(tmp_path)
    client = TestClient(create_app(
        tmp_path / "store", "hmac-secret", cookie_secure=False,
        public_base_url="https://share.example",
    ))
    client.post(f"/{created['share_id']}/session", json={"secret": created["secret"]})

    partial = client.get(f"/{created['share_id']}/audio", headers={"Range": "bytes=10-19"})
    assert partial.status_code == 206
    assert partial.content == bytes(range(10, 20))
    assert partial.headers["content-range"] == "bytes 10-19/1024"
    assert partial.headers["cache-control"] == "no-store"

    for value in ("bytes=-", "bytes=0-1,3-4", "bytes=" + "9" * 100 + "-"):
        invalid = client.get(f"/{created['share_id']}/audio", headers={"Range": value})
        assert invalid.status_code == 416
        assert invalid.headers["content-range"] == "bytes */1024"
        assert invalid.headers["cache-control"] == "no-store"


def test_gateway_rate_limit_is_bounded_and_opaque(tmp_path):
    client = TestClient(create_app(
        tmp_path / "store", "hmac-secret", cookie_secure=False,
        public_base_url="https://share.example",
    ))
    responses = [client.get("/AAAAAAAAAAAAAAAAAAAAAAAA") for _ in range(31)]
    assert all(response.status_code == 200 for response in responses[:30])
    assert responses[-1].status_code == 429
    assert responses[-1].text == "Недействительная или истёкшая ссылка"
    assert responses[-1].headers["cache-control"] == "no-store"


def test_deployment_is_dedicated_and_cannot_mount_archive_or_publish_viewer(tmp_path):
    root = Path(__file__).resolve().parents[1]
    project_root = root.parent
    compose = (project_root / "deploy/public-share/docker-compose.yml").read_text()
    assert "app.public_gateway:app" in compose
    assert "/archive" not in compose
    assert "ports:" not in compose
    assert "read_only: true" in compose
    assert "no-new-privileges:true" in compose
    assert "PUBLIC_SHARE_STORE" in compose
    assert "PUBLIC_SHARE_STORES" in compose
    assert "/shares/tenant-alpha:ro" in compose
    assert "public_share_secret" in compose
    assert "tenant-b" not in compose
    assert "PUBLIC_SHARE_GATEWAY_STATE" in compose
    assert ":/state" in compose
    assert "PUBLIC_SHARE_BASE_URL" in compose

    private_compose = (root / "docker-compose.yml").read_text()
    assert "read_only: true" in private_compose
    assert "PUBLIC_SHARE_STORE_HOST" in private_compose
    assert "PUBLIC_SHARE_SECRET_FILE" in private_compose
    assert "PUBLIC_SHARE_SECRET:" not in private_compose
    assert ":/public-shares" in private_compose

    app_js = (root / "app/static/app.js").read_text()
    assert "data-public-share" in app_js
    assert "const names = ['metadata', 'summary', 'transcript', 'mindmap', 'images', 'audio']" in app_js
    assert "public-share-${id}" in app_js
    assert '<option value="1h">1 час</option>' in app_js
    assert '<option value="24h" selected>24 часа</option>' in app_js
    assert '<option value="7d">7 дней</option>' in app_js
    assert "/public-shares" in app_js


def test_public_gateway_cookie_opt_out_is_exact_and_development_only(monkeypatch):
    for value in (None, "0", "false", "no", "true", "development", "1"):
        if value is None:
            monkeypatch.delenv("PUBLIC_SHARE_DEVELOPMENT_INSECURE_COOKIE", raising=False)
        else:
            monkeypatch.setenv("PUBLIC_SHARE_DEVELOPMENT_INSECURE_COOKIE", value)
        assert public_gateway.cookie_secure_from_env() is (value != "1")

def test_public_share_base_url_is_explicit_validated_and_domain_neutral(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    project_root = root.parent
    checked = [
        root / "app/main.py",
        root / "app/public_gateway.py",
        root / "docker-compose.yml",
        project_root / "deploy/public-share/docker-compose.yml",
        project_root / "deploy/public-share/README.md",
    ]
    assert all("0ud.io" not in path.read_text() for path in checked)

    invalid = (
        "",
        "http://share.example",
        "https://user@share.example",
        "https://share.example/path",
        "https://share.example:invalid",
        "https://share.example\\attacker.example",
    )
    for value in invalid:
        with pytest.raises(RuntimeError, match="invalid PUBLIC_SHARE_BASE_URL"):
            public_share.validate_base_url(value)
    assert public_share.validate_base_url("https://share.example/") == "https://share.example"
    assert (
        public_share.validate_base_url("https://SHARE.EXAMPLE:443/")
        == "https://share.example"
    )

    monkeypatch.delenv("PUBLIC_SHARE_BASE_URL", raising=False)
    gateway = TestClient(create_app(tmp_path / "store", "hmac-secret", cookie_secure=False))
    denied = gateway.post(
        "/AAAAAAAAAAAAAAAAAAAAAAAA/session",
        json={"secret": "unconfigured"},
        headers={"Origin": "https://share.example"},
    )
    assert denied.status_code == 404


def test_public_share_configuration_works_for_each_provisioned_tenant(monkeypatch):
    monkeypatch.setattr(main, "PUBLIC_SHARE_ENABLED", True)
    monkeypatch.setattr(main, "PUBLIC_SHARE_SECRET", "configured-secret")
    monkeypatch.setattr(main, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    for tenant in ("owner", "tenant-b", "tenant-c", "tenant-d"):
        monkeypatch.setattr(main, "TENANT_ID", tenant)
        assert main._public_share_configured() is True

    monkeypatch.setattr(main, "TENANT_ID", "")
    assert main._public_share_configured() is False


def test_owner_mints_explicit_share_export_and_revokes_it(tmp_path, monkeypatch):
    archive = tmp_path / "archive.db"
    conn = sqlite3.connect(archive)
    conn.execute(
        """CREATE TABLE recordings(
             id TEXT PRIMARY KEY,name TEXT,start_at TEXT,duration_ms INTEGER,
             lang TEXT,asr_engine TEXT,asr_transcript TEXT,plaud_transcript TEXT,
             summary TEXT,summary_json TEXT,asr_meta_json TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO recordings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "rec1", "Встреча", "2026-08-15T12:00:00Z", 123000, "ru", "asr",
            "Транскрипт", "", "# Итог", json.dumps({"title": "Итог"}), "{}",
        ),
    )
    conn.commit()
    conn.close()
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "rec1.mp3").write_bytes(b"audio")
    png = b"\x89PNG\r\n\x1a\n" + b"owner-visual" * 20
    mindmap_png = tmp_path / "mindmap.png"
    summary_card_png = tmp_path / "summary-card.png"
    mindmap_png.write_bytes(png)
    summary_card_png.write_bytes(png + b"card")

    def open_db():
        db = sqlite3.connect(archive)
        db.row_factory = sqlite3.Row
        return db

    monkeypatch.setattr(main, "db", open_db)
    monkeypatch.setattr(main, "AUDIO_DIR", str(audio))
    monkeypatch.setattr(main, "_mindmap_png", lambda _rec_id: str(mindmap_png))
    monkeypatch.setattr(main, "_summary_card_png", lambda _rec_id: str(summary_card_png))
    monkeypatch.setattr(main, "PUBLIC_SHARE_STORE", str(tmp_path / "shares"))
    monkeypatch.setattr(main, "PUBLIC_SHARE_SECRET", "owner-public-secret")
    monkeypatch.setattr(main, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    monkeypatch.setattr(main, "PUBLIC_SHARE_ENABLED", True)
    monkeypatch.setattr(main, "TENANT_ID", "owner")
    monkeypatch.setattr(main, "SECRET", "owner-cookie-secret")
    owner = TestClient(main.app)
    owner.cookies.set(main.COOKIE_NAME, main.expected_token())

    body = {
        "ttl": "1h",
        "content": {
            "metadata": True,
            "summary": True,
            "transcript": False,
            "mindmap": True,
            "images": True,
            "audio": True,
        },
    }
    minted = owner.post(
        "/api/recordings/rec1/public-shares",
        json=body,
        headers={"Host": "attacker.example", "X-CSRF-Token": main.csrf_token()},
    )
    assert minted.status_code == 201
    result = minted.json()
    assert result["url"].startswith("https://share.example/")
    assert "attacker.example" not in result["url"]
    assert result["expires_in_seconds"] == 3600
    exported = json.loads(
        (
            Path(main.PUBLIC_SHARE_STORE)
            / "exports"
            / result["share_id"]
            / "content.json"
        ).read_text()
    )
    assert "summary" in exported and "transcript" not in exported
    export_dir = Path(main.PUBLIC_SHARE_STORE) / "exports" / result["share_id"]
    assert (export_dir / "mindmap.png").read_bytes() == png
    assert (export_dir / "summary-card.png").read_bytes() == png + b"card"

    cross_revoke = owner.delete(
        f"/api/public-shares/{result['share_id']}",
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert cross_revoke.status_code == 403
    assert (
        Path(main.PUBLIC_SHARE_STORE) / "exports" / result["share_id"]
    ).exists()

    revoked = owner.delete(
        f"/api/public-shares/{result['share_id']}",
        headers={"X-CSRF-Token": main.csrf_token()},
    )
    assert revoked.status_code == 200
    assert revoked.json() == {"revoked": True}
    assert not (
        Path(main.PUBLIC_SHARE_STORE) / "exports" / result["share_id"]
    ).exists()

    cross_site = owner.post(
        "/api/recordings/rec1/public-shares",
        json=body,
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert cross_site.status_code == 403

    monkeypatch.setattr(main, "PUBLIC_SHARE_BASE_URL", "")
    exports = Path(main.PUBLIC_SHARE_STORE) / "exports"
    existing = {path.name for path in exports.iterdir()} if exports.exists() else set()
    unconfigured = owner.post(
        "/api/recordings/rec1/public-shares",
        json=body,
        headers={"X-CSRF-Token": main.csrf_token()},
    )
    assert unconfigured.status_code == 404
    assert ({path.name for path in exports.iterdir()} if exports.exists() else set()) == existing


def test_owner_reconcile_revokes_when_archive_record_disappears(tmp_path, monkeypatch):
    created = _create(tmp_path)
    monkeypatch.setattr(main, "PUBLIC_SHARE_STORE", str(tmp_path / "store"))
    monkeypatch.setattr(main, "PUBLIC_SHARE_SECRET", "hmac-secret")
    monkeypatch.setattr(main, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    monkeypatch.setattr(main, "PUBLIC_SHARE_ENABLED", True)
    monkeypatch.setattr(main, "TENANT_ID", "owner")
    archive = tmp_path / "empty-archive.db"
    with sqlite3.connect(archive) as conn:
        conn.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY)")
    monkeypatch.setattr(main, "db", lambda: sqlite3.connect(archive))
    assert main._reconcile_public_shares() == 1
    assert public_share.verify(
        tmp_path / "store", "hmac-secret", created["share_id"], created["secret"]
    ) is None


def test_owner_reconcile_keeps_ingested_record_with_archived_at(tmp_path, monkeypatch):
    created = _create(tmp_path)
    monkeypatch.setattr(main, "PUBLIC_SHARE_STORE", str(tmp_path / "store"))
    monkeypatch.setattr(main, "PUBLIC_SHARE_SECRET", "hmac-secret")
    monkeypatch.setattr(main, "PUBLIC_SHARE_BASE_URL", "https://share.example")
    monkeypatch.setattr(main, "PUBLIC_SHARE_ENABLED", True)
    monkeypatch.setattr(main, "TENANT_ID", "owner")
    archive = tmp_path / "archive.db"
    with sqlite3.connect(archive) as conn:
        conn.execute(
            "CREATE TABLE recordings("
            "id TEXT PRIMARY KEY, archived_at TEXT, archived_local_at TEXT, deleted_at TEXT)"
        )
        conn.execute(
            "INSERT INTO recordings(id, archived_at, archived_local_at, deleted_at) "
            "VALUES(?, ?, NULL, NULL)",
            ("internal-rec-1", "2026-08-15T00:00:00Z"),
        )
    monkeypatch.setattr(main, "db", lambda: sqlite3.connect(archive))

    assert main._reconcile_public_shares() == 0
    assert public_share.verify(
        tmp_path / "store", "hmac-secret", created["share_id"], created["secret"]
    ) is not None
