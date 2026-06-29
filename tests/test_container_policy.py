"""Static guardrails for the hosted one-run worker contract."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_worker_container_is_disposable_and_unprivileged():
    service = yaml.safe_load((ROOT / "docker-compose.worker.yml").read_text())["services"][
        "crawl-worker"
    ]

    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert any(value.startswith("seccomp:") for value in service["security_opt"])
    assert "ports" not in service
    assert "network_mode" not in service
    assert service["pids_limit"] <= 256
    assert service["mem_limit"]
    assert service["cpus"]
    assert all("docker.sock" not in volume for volume in service["volumes"])
    assert len(service["volumes"]) == 1
    assert service["volumes"][0].endswith(":/job:rw")


def test_playwright_package_and_image_versions_are_pinned_together():
    project = (ROOT / "pyproject.toml").read_text()
    dockerfile = (ROOT / "Dockerfile.worker").read_text()

    assert '"playwright==1.60.0"' in project
    assert "playwright/python:v1.60.0-noble" in dockerfile
    assert "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in dockerfile
    assert "USER pwuser" in dockerfile


def test_only_private_supervisor_receives_runtime_socket():
    worker = (ROOT / "docker-compose.worker.yml").read_text()
    supervisor = (ROOT / "docker-compose.supervisor.yml").read_text()

    assert "docker.sock" not in worker
    assert "docker.sock" in supervisor
    assert '"127.0.0.1:8091:8091"' in supervisor
