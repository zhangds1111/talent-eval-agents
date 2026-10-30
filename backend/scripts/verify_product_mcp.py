from __future__ import annotations

import json
from datetime import date
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time

import anyio
import httpx2
from langchain_core.messages import AIMessage
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import uvicorn

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models import (  # noqa: E402
    Base,
    CertificationRule,
    EmployeeProfile,
    ProductAttributes,
    ProductCertification,
)
from app.product_mcp_client import build_mcp_tool_graph  # noqa: E402
from app.product_mcp_server import (  # noqa: E402
    authenticated_product_context,
    build_product_mcp_server,
)
from app.product_tools import ProductToolContext, ProductToolService, ToolExecutor  # noqa: E402


class DemoTokenVerifier:
    def __init__(self, tokens: dict[str, AccessToken]) -> None:
        self.tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        return self.tokens.get(token)


def seed_demo_products(engine) -> None:
    """隔离 SQLite 演示数据：tenant-a 一个美国站锂电露营灯，tenant-b 一个越权 SKU。"""
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        lamp = EmployeeProfile(
            employee_no="SKU-DEMO-001",
            tenant_id="tenant-a",
            name="演示露营灯（虚构）",
            region="美国",
            current_position="充电式露营灯",
            department="户外照明",
        )
        other = EmployeeProfile(
            employee_no="SKU-OTHER-001",
            tenant_id="tenant-b",
            name="其他租户商品",
            region="美国",
            current_position="充电式露营灯",
            department="户外照明",
        )
        db.add_all([lamp, other])
        db.flush()
        db.add_all(
            [
                ProductAttributes(
                    product_id=lamp.id, model_no="DL-1", currency="USD", target_price=20, landed_cost=11,
                    is_electrical=True, has_lithium_battery=True,
                ),
                CertificationRule(market="美国", category="*", applies_when="electrical",
                                  certification="FCC Part 15", rationale="演示规则"),
                CertificationRule(market="*", category="*", applies_when="lithium_battery",
                                  certification="UN38.3", rationale="演示规则"),
                ProductCertification(product_id=lamp.id, certification="UN38.3", document_no="FICT-DEMO-1",
                                     applicable_model="DL-1", valid_until=date(2099, 1, 1), source_material="demo.md"),
            ]
        )
        db.commit()


def build_demo_service(database_path: Path) -> ProductToolService:
    engine = create_engine(f"sqlite:///{database_path}")
    seed_demo_products(engine)

    def session_factory() -> Session:
        return Session(engine)

    return ProductToolService(session_factory=session_factory)


def demo_context() -> ProductToolContext:
    return ProductToolContext(
        tenant_id="tenant-a",
        permission_scopes=("hr_private",),
        actor_id="lesson-13",
        run_id="verify-13",
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def verify_protocol(server) -> dict[str, object]:
    # async with Client(server) as client:
    #     print("[auto]", client.protocol_version)

    # async with Client(server, mode="legacy") as client:
    #     print("[legacy]", client.protocol_version)

    async with Client(server) as client:
        tools = await client.list_tools()
        templates = await client.list_resource_templates()
        resource = await client.read_resource("product://skus/SKU-DEMO-001/profile")
        prompt = await client.get_prompt(
            "product_assessment",
            {"request_text": "美国站户外照明，毛利不低于 40%"},
        )
        return {
            "protocol": client.protocol_version,
            "tools": [item.name for item in tools.tools],
            "resource_templates": [item.uri_template for item in templates.resource_templates],
            "resource_sku": json.loads(resource.contents[0].text)["sku_id"],
            "prompt_role": prompt.messages[0].role,
        }


async def verify_auth(service: ProductToolService) -> dict[str, int]:
    resource = "http://127.0.0.1:18081/mcp"
    common_claims = {"tenant_id": "tenant-a", "permission_scopes": ["hr_private"]}
    verifier = DemoTokenVerifier(
        {
            "missing-scope": AccessToken(
                token="missing-scope",
                client_id="product-chat",
                scopes=[],
                resource=resource,
                claims=common_claims,
            ),
            "wrong-audience": AccessToken(
                token="wrong-audience",
                client_id="product-chat",
                scopes=["product:read"],
                resource="https://other.example.com/mcp",
                claims=common_claims,
            ),
            "true-audience": AccessToken(
                token="true-audience",
                client_id="product-chat",
                scopes=["product:read"],
                resource=resource,
                claims=common_claims,
            ),
        }
    )
    server = build_product_mcp_server(
        service,
        ToolExecutor(),
        context_provider=authenticated_product_context,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url="https://auth.example.com", # 谁签发 token
            resource_server_url=resource, # token 发给谁，即 audience
            validate_token_resource=True,
            required_scopes=["product:read"],
        ),
    )
    transport = httpx2.ASGITransport(app=server.streamable_http_app())
    true_audience_status: int | None = None

    async def capture_true_audience_response(response: httpx2.Response) -> None:
        nonlocal true_audience_status
        if response.request.headers.get("Mcp-Method") == "tools/list":
            true_audience_status = int(response.status_code)

    async with server.session_manager.run():
        async with httpx2.AsyncClient(transport=transport, base_url=resource) as client:
            missing = await client.post(".", json={})
            missing_scope = await client.post(
                ".",
                json={},
                headers={"Authorization": "Bearer missing-scope"},
            )
            wrong_audience = await client.post(
                ".",
                json={},
                headers={"Authorization": "Bearer wrong-audience"},
            )
        async with httpx2.AsyncClient(
            transport=transport,
            base_url=resource,
            headers={"Authorization": "Bearer true-audience"},
            event_hooks={"response": [capture_true_audience_response]},
        ) as authenticated_client:
            async with Client(
                streamable_http_client(resource, http_client=authenticated_client)
            ) as mcp_client:
                await mcp_client.list_tools()

    if true_audience_status is None:
        raise RuntimeError("正确 audience 的 MCP 能力发现没有产生 HTTP 响应")

    return {
        "missing_token": int(missing.status_code),
        "missing_scope": int(missing_scope.status_code),
        "wrong_audience": int(wrong_audience.status_code),
        "true_audience": true_audience_status,
    }


async def verify_langgraph(server) -> dict[str, object]:
    from langchain.mcp import MCPAdapter

    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(server.streamable_http_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not uvicorn_server.started and time.monotonic() < deadline:
        await anyio.sleep(0.05)
    if not uvicorn_server.started:
        raise RuntimeError("MCP HTTP 服务未在 10 秒内启动")

    try:
        async with MCPAdapter(url) as adapter:
            tools = await adapter.list_tools()
            graph = build_mcp_tool_graph(tools)
            mermaid = graph.get_graph().draw_mermaid()
            print(f"[graph mermaid]\n{mermaid}")
            result = await graph.ainvoke(
                {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "filter_products",
                                    "args": {
                                        "filters": [
                                            {"field": "region", "operator": "eq", "value": "US"}
                                        ]
                                    },
                                    "id": "verify-13",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    ]
                }
            )
            tool_message = result["messages"][-1]
            payload = tool_message.artifact["structured_content"]
            return {
                "discovered_tools": [tool.name for tool in tools],
                "sku_ids": payload["data"],
                "tool_status": tool_message.status,
            }
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=10)


async def main() -> None:
    with TemporaryDirectory(prefix="product-mcp-") as directory:
        service = build_demo_service(Path(directory) / "product.db")
        server = build_product_mcp_server(
            service,
            ToolExecutor(),
            context_provider=demo_context,
        )
        protocol = await verify_protocol(server)
        auth = await verify_auth(service)
        langgraph = await verify_langgraph(server)

    print("[protocol]", json.dumps(protocol, ensure_ascii=False))
    print("\n[auth]", json.dumps(auth, ensure_ascii=False))
    print("\n[langgraph]", json.dumps(langgraph, ensure_ascii=False))


if __name__ == "__main__":
    anyio.run(main)
