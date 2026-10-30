from __future__ import annotations

import anyio
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from app.product_mcp_client import build_mcp_tool_graph


def test_mcp_tools_can_run_inside_langgraph_tool_node():
    @tool
    async def filter_skus(region: str) -> list[str]:
        """Filter skus by region."""
        return ["C001"] if region == "上海" else []

    graph = build_mcp_tool_graph([filter_skus])

    async def scenario():
        return await graph.ainvoke(
            {
                "messages": [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "filter_skus",
                                "args": {"region": "上海"},
                                "id": "call-13",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            }
        )

    result = anyio.run(scenario)

    message = result["messages"][-1]
    assert isinstance(message, ToolMessage)
    assert message.name == "filter_skus"
    assert message.content == '["C001"]'
