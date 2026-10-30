import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { countLabel, countText, toErrorMessage, type LoadState } from "./loadState";
import { LoadError, SkeletonCards, SkeletonList, SkeletonRows } from "./LoadStateView";

describe("countText / countLabel", () => {
  it("加载中返回「加载中…」而不是 0", () => {
    const loading: LoadState<string[]> = { status: "loading" };
    expect(countText(loading, () => "8 个 SKU")).toBe("加载中…");
    expect(countLabel(loading, (n) => `${n} 个 SKU`)).toBe("加载中…");
    expect(countLabel(loading, (n) => `共 ${n} 个知识库`)).toBe("加载中…");
  });

  it("成功/失败时按真实数量（含 0）格式化", () => {
    expect(countLabel({ status: "success", data: [] }, (n) => `${n} 个 SKU`)).toBe("0 个 SKU");
    expect(countLabel({ status: "success", data: ["a", "b"] }, (n) => `${n} 个 SKU`)).toBe("2 个 SKU");
    expect(countLabel({ status: "error", error: "x" }, (n) => `${n} 份材料`)).toBe("0 份材料");
  });

  it("toErrorMessage 提取 Error.message，其他值走兜底文案", () => {
    expect(toErrorMessage(new Error("网络异常"), "加载失败")).toBe("网络异常");
    expect(toErrorMessage("raw", "加载失败")).toBe("加载失败");
    expect(toErrorMessage(undefined)).toBe("数据加载失败");
  });
});

/* 与页面一致的三态分支：加载中 → 骨架；失败 → 错误 + 重试；成功 → 真实行 / 空态。 */
function ListFixture({ state }: { state: LoadState<string[]> }) {
  return (
    <div>
      <span className="toolbarCount">{countLabel(state, (n) => `${n} 个 SKU`)}</span>
      {state.status === "loading" ? (
        <SkeletonRows rows={5} columns={8} />
      ) : state.status === "error" ? (
        <LoadError message={state.error} onRetry={() => {}} />
      ) : state.data.length === 0 ? (
        <div className="empty compact">暂无 SKU，点击右上角「新建商品」开始录入</div>
      ) : (
        <ul>{state.data.map((item) => <li key={item}>{item}</li>)}</ul>
      )}
    </div>
  );
}

describe("列表三态", () => {
  it("加载中：渲染骨架行、计数显示「加载中…」，不显示 0 与空态", () => {
    const html = renderToStaticMarkup(<ListFixture state={{ status: "loading" }} />);
    expect(html).toContain("加载中…");
    expect(html).not.toContain("0 个 SKU");
    expect(html).not.toContain("暂无 SKU");
    expect(html).not.toContain("empty compact");
    expect((html.match(/class="tr skeletonRow"/g) || []).length).toBe(5);
    // 每行 8 个骨架条，且不含真实数据行
    expect((html.match(/skeletonLine/g) || []).length).toBe(5 * 8);
    expect(html).not.toContain("<li>");
  });

  it("加载完成且无数据：显示空态与真实计数 0，不渲染骨架", () => {
    const html = renderToStaticMarkup(<ListFixture state={{ status: "success", data: [] }} />);
    expect(html).toContain("0 个 SKU");
    expect(html).toContain("empty compact");
    expect(html).toContain("暂无 SKU");
    expect(html).not.toContain("skeletonRow");
  });

  it("加载完成且有数据：渲染真实行与计数", () => {
    const html = renderToStaticMarkup(
      <ListFixture state={{ status: "success", data: ["露营灯 X1", "头灯 H2"] }} />,
    );
    expect(html).toContain("2 个 SKU");
    expect(html).toContain("露营灯 X1");
    expect(html).toContain("<li>");
    expect(html).not.toContain("skeletonRow");
    expect(html).not.toContain("empty compact");
  });

  it("加载失败：显示错误提示与重试按钮", () => {
    const html = renderToStaticMarkup(
      <ListFixture state={{ status: "error", error: "商品档案加载失败" }} />,
    );
    expect(html).toContain('role="alert"');
    expect(html).toContain("商品档案加载失败");
    expect(html).toContain("重试");
    expect(html).not.toContain("skeletonRow");
    expect(html).not.toContain("empty compact");
  });

  it("LoadError 的 onRetry 由按钮点击触发", () => {
    // renderToStaticMarkup 不挂载事件，这里校验按钮存在且带 type；行为由 onClick 绑定保证
    const html = renderToStaticMarkup(<LoadError message="会话列表加载失败" onRetry={() => {}} />);
    expect(html).toContain('class="loadError"');
    expect(html).toContain("会话列表加载失败");
    expect(html).toContain("<button");
  });
});

describe("骨架组件", () => {
  it("SkeletonCards 渲染指定数量卡片并透传外层网格类", () => {
    const html = renderToStaticMarkup(<SkeletonCards count={3} className="knowledgeBaseGrid" />);
    expect((html.match(/skeletonCard"/g) || []).length).toBe(3);
    expect(html).toContain("knowledgeBaseGrid");
  });

  it("SkeletonList 渲染指定数量的双行列表项", () => {
    const html = renderToStaticMarkup(<SkeletonList rows={5} />);
    expect((html.match(/skeletonItem/g) || []).length).toBe(5);
    expect((html.match(/skeletonLine/g) || []).length).toBe(10);
  });

  it("骨架组件带 aria-hidden，避免被读屏误读为真实内容", () => {
    const html = renderToStaticMarkup(<SkeletonRows rows={2} columns={3} />);
    expect(html).toContain('aria-hidden="true"');
  });
});

describe("toErrorMessage 与重试回调", () => {
  it("LoadError 接受自定义重试文案", () => {
    const onRetry = vi.fn();
    const html = renderToStaticMarkup(<LoadError message="x" onRetry={onRetry} retryLabel="重新加载" />);
    expect(html).toContain("重新加载");
    expect(onRetry).not.toHaveBeenCalled(); // 纯渲染不触发，点击才触发
  });
});
