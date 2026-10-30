from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.shared.exceptions import MCPError
from mcp.types import TextResourceContents
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.product_mcp_server import (
    authenticated_product_context,
    build_product_mcp_server,
    product_context_from_token,
)
from app.product_tools import ProductToolContext, ProductToolService, ToolExecutor
from scripts.verify_product_mcp import build_demo_service, seed_demo_products, verify_auth


def _session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'product-mcp.db'}")
    seed_demo_products(engine)

    def factory():
        return Session(engine)

    return factory


def _context() -> ProductToolContext:
    return ProductToolContext(
        tenant_id="tenant-a",
        permission_scopes=("hr_private",),
        actor_id="mcp-course-client",
        run_id="run-13",
    )


def _server(tmp_path):
    def evidence_provider(**kwargs):
        return {
            "candidate_ids": kwargs["candidate_ids"],
            "evidence_packs": [
                {"schema_version": "2.0", "candidate_id": candidate_id, "requirements": []}
                for candidate_id in kwargs["candidate_ids"]
            ],
        }

    service = ProductToolService(
        session_factory=_session_factory(tmp_path),
        evidence_provider=evidence_provider,
    )
    return build_product_mcp_server(service, ToolExecutor(), context_provider=_context)


def test_server_advertises_product_tools_resources_and_prompt(tmp_path):
    async def scenario():
        async with Client(_server(tmp_path)) as client:
            tools = await client.list_tools()
            resources = await client.list_resources()
            templates = await client.list_resource_templates()
            prompts = await client.list_prompts()
            return tools, resources, templates, prompts

    tools, resources, templates, prompts = anyio.run(scenario)

    assert {item.name for item in tools.tools} == {
        "filter_products",
        "search_product_evidence",
        "get_product_profiles",
        "check_product_compliance",
    }
    assert {str(item.uri) for item in resources.resources} == {"product://policies/certification-rules"}
    assert {item.uri_template for item in templates.resource_templates} == {"product://skus/{sku_id}/profile"}
    assert [item.name for item in prompts.prompts] == ["product_assessment"]


def test_mcp_tools_reuse_existing_service_and_result_protocol(tmp_path):
    async def scenario():
        async with Client(_server(tmp_path)) as client:
            skus = await client.call_tool(
                "filter_products",
                {"filters": [
                    {"field": "region", "operator": "eq", "value": "美国"},
                    {"field": "gross_margin_percent", "operator": "gte", "value": 40},
                ]},
            )
            profiles = await client.call_tool("get_product_profiles", {"sku_ids": ["SKU-DEMO-001", "SKU-OTHER-001"]})
            compliance = await client.call_tool(
                "check_product_compliance", {"sku_ids": ["SKU-DEMO-001", "SKU-OTHER-001"]}
            )
            return skus, profiles, compliance

    skus, profiles, compliance = anyio.run(scenario)

    assert skus.is_error is False
    assert skus.structured_content["data"] == ["SKU-DEMO-001"]
    assert [item["candidate_id"] for item in profiles.structured_content["data"]] == ["SKU-DEMO-001"]
    facts = compliance.structured_content["data"]
    assert [item["candidate_id"] for item in facts] == ["SKU-DEMO-001"]  # 越权 SKU 不返回
    assert facts[0]["unit_economics"]["gross_margin_percent"] == 45.0
    assert {c["certification"]: c["status"] for c in facts[0]["certification_checks"]} == {
        "FCC Part 15": "missing",
        "UN38.3": "evidenced",
    }
    assert facts[0]["certification_gap_count"] == 1


def test_evidence_tool_preserves_evidence_pack_v2(tmp_path):
    async def scenario():
        async with Client(_server(tmp_path)) as client:
            return await client.call_tool(
                "search_product_evidence",
                {"query": "FCC 检测报告", "sku_ids": ["SKU-DEMO-001"]},
            )

    result = anyio.run(scenario)

    assert result.is_error is False
    assert result.structured_content["data"]["evidence_packs"][0]["schema_version"] == "2.0"


def test_resources_apply_the_same_tenant_boundary(tmp_path):
    async def scenario():
        async with Client(_server(tmp_path)) as client:
            profile = await client.read_resource("product://skus/SKU-DEMO-001/profile")
            rules = await client.read_resource("product://policies/certification-rules")
            with pytest.raises(MCPError):
                await client.read_resource("product://skus/SKU-OTHER-001/profile")
            return profile, rules

    profile, rules = anyio.run(scenario)

    assert isinstance(profile.contents[0], TextResourceContents)
    assert json.loads(profile.contents[0].text)["candidate_id"] == "SKU-DEMO-001"
    assert {item["certification"] for item in json.loads(rules.contents[0].text)} == {"FCC Part 15", "UN38.3"}


def test_prompt_renders_product_request(tmp_path):
    async def scenario():
        async with Client(_server(tmp_path)) as client:
            return await client.get_prompt("product_assessment", {"request_text": "美国站露营灯，毛利不低于 40%"})

    result = anyio.run(scenario)

    assert result.messages[0].role == "user"
    assert "美国站露营灯" in result.messages[0].content.text
    assert "check_product_compliance" in result.messages[0].content.text


def test_access_token_claims_become_trusted_product_context():
    token = AccessToken(
        token="course-token",
        client_id="product-chat",
        subject="teacher-demo",
        scopes=["product:read"],
        resource="http://127.0.0.1:18081/mcp",
        claims={
            "tenant_id": "tenant-a",
            "permission_scopes": ["hr_private"],
            "run_id": "run-13",
        },
    )

    context = product_context_from_token(token)

    assert context == ProductToolContext(
        tenant_id="tenant-a",
        permission_scopes=("hr_private",),
        actor_id="teacher-demo",
        run_id="run-13",
    )


@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"permission_scopes": ["hr_private"]}, "tenant_id"),
        ({"tenant_id": "tenant-a"}, "permission_scopes"),
        ({"tenant_id": "tenant-a", "permission_scopes": "hr_private"}, "permission_scopes"),
    ],
)
def test_access_token_requires_tenant_and_data_permissions(claims, message):
    token = AccessToken(
        token="course-token",
        client_id="product-chat",
        scopes=["product:read"],
        claims=claims,
    )

    with pytest.raises(PermissionError, match=message):
        product_context_from_token(token)


class _TokenVerifier:
    def __init__(self, tokens):
        self.tokens = tokens

    async def verify_token(self, token: str):
        return self.tokens.get(token)


def _protected_server(tmp_path):
    resource = "http://127.0.0.1:18081/mcp"
    tokens = {
        "valid": AccessToken(
            token="valid",
            client_id="product-chat",
            subject="teacher-demo",
            scopes=["product:read"],
            resource=resource,
            claims={"tenant_id": "tenant-a", "permission_scopes": ["hr_private"], "run_id": "run-13"},
        ),
        "missing-scope": AccessToken(
            token="missing-scope",
            client_id="product-chat",
            scopes=[],
            resource=resource,
            claims={"tenant_id": "tenant-a", "permission_scopes": ["hr_private"]},
        ),
        "wrong-audience": AccessToken(
            token="wrong-audience",
            client_id="product-chat",
            scopes=["product:read"],
            resource="https://other.example.com/mcp",
            claims={"tenant_id": "tenant-a", "permission_scopes": ["hr_private"]},
        ),
    }
    service = ProductToolService(session_factory=_session_factory(tmp_path))
    server = build_product_mcp_server(
        service,
        ToolExecutor(),
        context_provider=authenticated_product_context,
        token_verifier=_TokenVerifier(tokens),
        auth=AuthSettings(
            issuer_url="https://auth.example.com",
            resource_server_url=resource,
            validate_token_resource=True,
            required_scopes=["product:read"],
        ),
    )
    return server, resource


def test_streamable_http_enforces_token_scope_and_audience(tmp_path):
    async def scenario():
        server, resource = _protected_server(tmp_path)
        app = server.streamable_http_app()
        transport = httpx2.ASGITransport(app=app)
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=transport, base_url=resource) as http_client:
                missing = await http_client.post(".", json={})
                insufficient = await http_client.post(
                    ".", json={}, headers={"Authorization": "Bearer missing-scope"}
                )
                wrong_audience = await http_client.post(
                    ".", json={}, headers={"Authorization": "Bearer wrong-audience"}
                )
        return missing, insufficient, wrong_audience

    missing, insufficient, wrong_audience = anyio.run(scenario)

    assert missing.status_code == 401
    assert insufficient.status_code == 403
    assert wrong_audience.status_code == 401


def test_streamable_http_token_claims_reach_business_service(tmp_path):
    async def scenario():
        server, resource = _protected_server(tmp_path)
        app = server.streamable_http_app()
        transport = httpx2.ASGITransport(app=app)
        async with server.session_manager.run():
            async with httpx2.AsyncClient(
                transport=transport,
                base_url=resource,
                headers={"Authorization": "Bearer valid"},
            ) as http_client:
                async with Client(streamable_http_client(resource, http_client=http_client)) as client:
                    return await client.call_tool(
                        "get_product_profiles",
                        {"sku_ids": ["SKU-DEMO-001", "SKU-OTHER-001"]},
                    )

    result = anyio.run(scenario)

    assert result.is_error is False
    assert [item["candidate_id"] for item in result.structured_content["data"]] == ["SKU-DEMO-001"]


def test_verify_auth_uses_valid_capability_discovery_for_true_audience(tmp_path):
    service = build_demo_service(tmp_path / "verify-auth.db")

    result = anyio.run(verify_auth, service)

    assert result == {
        "missing_token": 401,
        "missing_scope": 403,
        "wrong_audience": 401,
        "true_audience": 200,
    }


def test_demo_http_server_exposes_tools_over_streamable_http(tmp_path):
    from scripts.run_product_mcp_http import build_demo_http_server

    async def scenario():
        server = build_demo_http_server(tmp_path / "product-http.db")
        transport = httpx2.ASGITransport(app=server.streamable_http_app(stateless_http=True))
        async with server.session_manager.run():
            async with httpx2.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:18081/mcp",
            ) as http_client:
                async with Client(
                    streamable_http_client(
                        "http://127.0.0.1:18081/mcp",
                        http_client=http_client,
                    )
                ) as client:
                    return await client.list_tools()

    result = anyio.run(scenario)

    assert {tool.name for tool in result.tools} == {
        "filter_products",
        "search_product_evidence",
        "get_product_profiles",
        "check_product_compliance",
    }


def test_demo_http_server_script_can_run_directly():
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_product_mcp_http.py"

    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--host" in result.stdout
    assert "--port" in result.stdout


def test_demo_http_server_stops_cleanly_on_keyboard_interrupt(monkeypatch, capsys):
    from scripts import run_product_mcp_http

    class StoppedServer:
        def run(self, *args, **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr(
        run_product_mcp_http,
        "build_demo_http_server",
        lambda database_path: StoppedServer(),
    )

    run_product_mcp_http.main([])

    assert "Product MCP 已停止" in capsys.readouterr().out
