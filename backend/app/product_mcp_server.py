from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from pydantic import Field

from app.query_plan import FilterCondition
from app.product_tools import ProductToolContext, ProductToolService, ToolExecutor


ContextProvider = Callable[[], ProductToolContext]
REQUIRED_SCOPE = "product:read"


def product_context_from_token(token: AccessToken) -> ProductToolContext:
    claims = token.claims or {}
    tenant_id = str(claims.get("tenant_id", "")).strip()
    raw_permission_scopes = claims.get("permission_scopes", [])
    if not isinstance(raw_permission_scopes, (list, tuple)):
        raise PermissionError("Access Token 的 permission_scopes 必须是列表")
    permission_scopes = tuple(
        str(item).strip() for item in raw_permission_scopes if str(item).strip()
    )
    if not tenant_id:
        raise PermissionError("Access Token 缺少 tenant_id")
    if not permission_scopes:
        raise PermissionError("Access Token 缺少 permission_scopes")
    return ProductToolContext(
        tenant_id=tenant_id,
        permission_scopes=permission_scopes,
        actor_id=token.subject or token.client_id,
        run_id=str(claims.get("run_id", "unknown")),
    )


def authenticated_product_context() -> ProductToolContext:
    token = get_access_token()
    if token is None:
        raise PermissionError("缺少经过验证的 MCP Access Token")
    return product_context_from_token(token)


def build_product_mcp_server(
    service: ProductToolService,
    executor: ToolExecutor,
    *,
    context_provider: ContextProvider,
    token_verifier: TokenVerifier | None = None,
    auth: AuthSettings | None = None,
) -> MCPServer:
    server = MCPServer(
        "Product Selection Capability Service",
        version="2.0.0",
        instructions="提供受租户与权限约束的选品商品筛选、材料证据检索与程序核算（毛利、必需认证缺口）能力。",
        token_verifier=token_verifier,
        auth=auth,
    )

    @server.tool(structured_output=True)
    def filter_products(
        filters: Annotated[list[FilterCondition], Field(max_length=20)],
    ) -> dict[str, Any]:
        """按结构化条件（市场、品类、售价、成本、毛利率等）筛选授权租户内的在售 SKU。"""
        context = context_provider()
        return executor.execute(
            "filter_products",
            lambda: service.filter_skus(filters, context=context),
            context=context,
            arguments={"filters": filters},
        )

    @server.tool(structured_output=True)
    def search_product_evidence(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        sku_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        """在指定 SKU 的商品材料中检索证据并返回 Evidence Pack 2.0。"""
        context = context_provider()
        return executor.execute(
            "search_product_evidence",
            lambda: service.search_sku_evidence(query, sku_ids, context=context),
            context=context,
            arguments={"query": query, "sku_ids": sku_ids},
        )

    @server.tool(structured_output=True)
    def get_product_profiles(
        sku_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        """批量读取授权租户内 SKU 的基础信息（名称、市场、品类、子类）。"""
        context = context_provider()
        return executor.execute(
            "get_product_profiles",
            lambda: service.get_sku_profiles(sku_ids, context=context),
            context=context,
            arguments={"sku_ids": sku_ids},
        )

    @server.tool(structured_output=True)
    def check_product_compliance(
        sku_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        """程序计算 SKU 的毛利与「市场×品类」必需认证缺口；只表述有/缺证据，不下合规结论。"""
        context = context_provider()
        return executor.execute(
            "check_product_compliance",
            lambda: service.check_product_compliance(sku_ids, context=context),
            context=context,
            arguments={"sku_ids": sku_ids},
        )

    @server.resource(
        "product://skus/{sku_id}/profile",
        title="SKU 基础档案",
        mime_type="application/json",
    )
    def product_profile(sku_id: str) -> dict[str, Any]:
        items = service.get_sku_profiles([sku_id], context=context_provider())
        if not items:
            raise ResourceNotFoundError("SKU 不存在或当前身份无权访问")
        return items[0]

    @server.resource(
        "product://policies/certification-rules",
        title="市场×品类必需认证规则",
        mime_type="application/json",
    )
    def certification_rules() -> list[dict[str, Any]]:
        return service.list_certification_rules(context=context_provider())

    @server.prompt(name="product_assessment", title="选品评估与推荐")
    def product_assessment(request_text: str) -> str:
        return (
            "按照跨境电商选品流程处理以下需求：先用 filter_products 做结构化筛选，"
            "再用 check_product_compliance 获取程序核算的毛利与认证缺口，"
            "最后用 search_product_evidence 检索材料证据；只表述有/缺证据，不下合规结论。\n"
            f"需求：{request_text}"
        )

    return server
