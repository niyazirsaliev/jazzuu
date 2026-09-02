from pathlib import Path


def test_service_worker_release_replaces_older_shell_and_caches_identity_selector():
    """An already-installed older PWA must activate this shell on its next open.

    The detail label controls can survive in an installed older shell even after
    the app bundle changes. A distinct cache identity makes install/activate
    delete that shell without asking a family member to clear data or reinstall.
    """
    root = Path(__file__).resolve().parents[1]
    sw = (root / "app/static/sw.js").read_text()
    app = (root / "app/static/app.js").read_text()
    assert "zapisi-shell-v24" in sw
    assert "zapisi-shell-v22" not in sw
    assert "'/identity-selector.js'" in sw
    assert "self.skipWaiting()" in sw
    assert "self.clients.claim()" in sw
    assert "mindmap_revision" in app
    assert "summary_card_revision" in app
    assert "Скачать аудио" in app
    assert "SHELL_PATHS.has(url.pathname)" in sw


def test_poll_control_is_visible_as_an_icon_not_a_russian_text_button():
    root = Path(__file__).resolve().parents[1]
    app = (root / "app/static/app.js").read_text()
    poll_markup = app.rsplit("const sourcePoll =", 1)[1].split("const right =", 1)[0]
    assert 'aria-label="Проверить PLAUD"' in poll_markup
    assert ">Проверить PLAUD<" not in poll_markup
    assert "w-full" not in poll_markup
    assert "<svg" in poll_markup
    assert "min-w-11" in poll_markup and "min-h-11" in poll_markup
