import pytest

from local_http import local_server


@pytest.fixture(scope="session")
def lab_url(tmp_path_factory):
    with local_server(tmp_path_factory.mktemp("v2board-http"), "v2lab.server:app") as (url, _):
        yield url


@pytest.fixture(autouse=True)
def isolated_panel_auth():
    from app.integrations.v2board import reset_panel_auth

    reset_panel_auth()
    yield
    reset_panel_auth()


@pytest.fixture(autouse=True)
def allow_only_the_explicit_local_lab(request, monkeypatch):
    # Real-network integration tests explicitly authorize only their own lab
    # origin. Production code has no implicit loopback/private-network bypass.
    if "lab_url" in request.fixturenames:
        from app.core.config import settings
        monkeypatch.setattr(settings, "panel_private_origins", request.getfixturevalue("lab_url"))
