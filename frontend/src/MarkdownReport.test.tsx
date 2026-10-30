import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { MarkdownReport } from "./ProductChatPage";

describe("MarkdownReport", () => {
  it("renders GFM structure and keeps evidence references interactive", () => {
    const html = renderToStaticMarkup(
      <MarkdownReport
        text={[
          "# 候选人报告",
          "",
          "**结论**",
          "",
          "- 第一项",
          "- 第二项 [E1](/api/evidence/citations/chunk-1)",
          "",
          "| 候选人 | 得分 |",
          "| --- | ---: |",
          "| C001 | 80 |",
        ].join("\n")}
        onCitation={vi.fn()}
      />,
    );

    expect(html).toContain("<h1>候选人报告</h1>");
    expect(html).toContain("<strong>结论</strong>");
    expect(html).toContain("<ul>");
    expect(html).toContain("<table>");
    expect(html).toContain('class="citationLink"');
  });
});

describe("MarkdownReport product sections", () => {
  it("marks program and risk sections and adds certification badges", () => {
    const html = renderToStaticMarkup(
      <MarkdownReport
        text={["### 程序核算（非模型生成）", "", "- 必需认证 UN38.3：缺有效证据：已过期", "", "### 风险与缺失", "", "- 缺少 SDS 文件"].join("\n")}
        onCitation={vi.fn()}
      />,
    );
    expect(html).toContain('class="reportSection program"');
    expect(html).toContain('class="reportSection risk"');
    expect(html).toContain('class="statusBadge expired"');
    expect(html).toContain('class="statusBadge missing"');
  });
});
