import socket

import pytest

from engine.network_policy import is_public_destination, validate_public_http_url
from engine.schemas import RunConfig


def test_public_url_validation_rejects_non_web_and_private_literals():
    assert validate_public_http_url("https://example.com/path") == "https://example.com/path"
    assert validate_public_http_url("https://8.8.8.8/") == "https://8.8.8.8/"
    with pytest.raises(ValueError):
        validate_public_http_url("file:///tmp/site.html")
    with pytest.raises(ValueError):
        validate_public_http_url("http://127.0.0.1/")
    with pytest.raises(ValueError):
        validate_public_http_url("http://169.254.169.254/latest/meta-data")


async def test_dns_resolution_rejects_any_private_answer(monkeypatch):
    def private_answer(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", private_answer)
    assert not await is_public_destination("https://example.com")


async def test_dns_resolution_accepts_public_answers(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    assert await is_public_destination("https://example.com")


def test_hosted_manager_fails_closed_without_private_supervisor(tmp_path):
    from api.manager import RunManager
    from engine.config import Settings

    manager = RunManager(
        Settings(hosted_mode=True, data_dir=tmp_path / "artifacts"), RunConfig()
    )
    with pytest.raises(ValueError, match="SUPERVISOR_URL"):
        manager.start_run("https://example.com")
