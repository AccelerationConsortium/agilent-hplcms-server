"""The Markdown agent documentation surface (/agent-docs, /llms.txt).

The JSON guide at ``/docs/agent`` is covered by ``test_api_documentation.py``;
these tests cover the lab-standard Markdown surface added alongside it, and
assert the API reference stays exhaustive as routes change.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agilent_hplcms_server.api import create_app
from agilent_hplcms_server.documentation import router


@pytest.fixture()
def client() -> TestClient:
    """A client whose probe never touches the instrument PC.

    Documentation must be readable with no hardware, no OpenLab and no claim.
    """
    app = create_app(reader=lambda settings: {})
    return TestClient(app)


def test_agent_docs_routes(client: TestClient) -> None:
    r = client.get("/agent-docs")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "allowed_actions" in r.text and "activity" in r.text

    r = client.get("/agent-docs/api-reference")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "/control/run" in r.text and "/control/workflow/start" in r.text

    r = client.get("/llms.txt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert "(agent-docs/api-reference)" in r.text and "(openapi.json)" in r.text
    # The existing JSON guide stays discoverable from the index.
    assert "docs/agent" in r.text


def test_json_agent_guide_is_untouched(client: TestClient) -> None:
    """Adding the Markdown surface must not disturb GET /docs/agent."""
    r = client.get("/docs/agent")
    assert r.status_code == 200
    guide = r.json()
    assert guide["protocol_version"] == "1.2"
    assert guide["title"] == "Agilent UPLC-MS agent integration guide"


@pytest.mark.parametrize("prefix", ["", "/hplcms", "/api/equipment/test/documentation"])
def test_index_links_without_hardware(prefix: str) -> None:
    """`llms.txt` links must resolve under any mount prefix, so they stay relative."""
    service = FastAPI()
    service.include_router(router)
    app = FastAPI()
    app.mount(prefix or "/", service)
    with TestClient(app) as http:
        response = http.get(f"{prefix}/llms.txt")
        assert response.status_code == 200
        links = re.findall(r"\]\(([^)]+)\)", response.text)
        assert set(links) == {"agent-docs", "agent-docs/api-reference", "openapi.json"}
        for link in links:
            resolved = urljoin(str(response.url), link)
            assert resolved == f"http://testserver{prefix}/{link}"
            assert http.get(resolved).status_code == 200


def test_openapi_lists_documentation_routes(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/agent-docs", "/agent-docs/api-reference", "/llms.txt", "/docs/agent"} <= set(paths)


def test_api_reference_documents_every_route(client: TestClient) -> None:
    """The reference is only useful if it is exhaustive — so assert that it is."""
    reference = client.get("/agent-docs/api-reference").text
    paths = client.get("/openapi.json").json()["paths"]
    missing = [path for path in paths if path not in reference]
    assert not missing, f"undocumented routes: {missing}"
