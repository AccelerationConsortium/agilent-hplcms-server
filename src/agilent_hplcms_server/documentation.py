"""Markdown agent documentation served from resources shipped in the package.

The lab's standard documentation surface, the same shape as the other device
services (agilent-cytation-server, torry-pines-shaker-server, sense-every-zone,
mt-xpr-balance-server): a Markdown agent guide, a Markdown API reference, and a
plain-text ``/llms.txt`` index, so an agent — or the dashboard's API reference
page — can discover how to drive this instrument without reading the repo.

This is additive. The versioned JSON guide at ``GET /docs/agent``
(:mod:`agilent_hplcms_server.agent_docs`) is unchanged and remains the
authority on execution policy, submission workflow order, sample addressing,
dispatch semantics and cancellation; the Markdown documents cross-reference it
rather than restating it.
"""

from __future__ import annotations

from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter(tags=["documentation"])


class MarkdownResponse(PlainTextResponse):
    media_type = "text/markdown"


def _document(name: str) -> str:
    return files("agilent_hplcms_server").joinpath("docs", name).read_text(encoding="utf-8")


@router.get("/agent-docs", response_class=MarkdownResponse, summary="Agent guide (Markdown)")
async def agent_docs() -> str:
    return _document("AGENT_GUIDE.md")


@router.get(
    "/agent-docs/api-reference",
    response_class=MarkdownResponse,
    summary="API reference (Markdown)",
)
async def api_reference() -> str:
    return _document("API_REFERENCE.md")


@router.get("/llms.txt", response_class=PlainTextResponse, summary="Discovery index for agents")
async def llms_txt() -> str:
    # Links are relative on purpose: the service is mounted behind a prefix on
    # the dashboard's documentation proxy, and an absolute "/agent-docs" would
    # resolve off the mount.
    return (
        "# Agilent UPLC-MS (STATUS_SPEC v1.2 device service)\n\n"
        "## Documentation\n\n"
        "- [Agent guide](agent-docs): health vs activity, claims and the workflow "
        "lock, the sample/sequence submission model, servicing, preconditions, "
        "refusal codes.\n"
        "- [API reference](agent-docs/api-reference): every route with its gate, "
        "body and refusal codes.\n"
        "- [OpenAPI](openapi.json): request/response schemas.\n"
        "- `docs/agent`: the versioned JSON agent guide — execution policy, "
        "submission workflow order, sample addressing, dispatch and cancellation.\n\n"
        "## Live status\n\n"
        "Read the service's `GET /status` through the lab-skills SDK or dashboard; "
        "live status is not a documentation-proxy resource. Read allowed_actions "
        "before acting.\n"
    )
