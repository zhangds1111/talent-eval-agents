/* 列表/区块三态：加载中 / 成功（含空数据）/ 失败。
   统一约定：加载中绝不代表数据为空，计数处显示「加载中…」而非 0。 */

export type LoadState<T> =
  | { status: "loading" }
  | { status: "success"; data: T }
  | { status: "error"; error: string };

export function toErrorMessage(reason: unknown, fallback = "数据加载失败"): string {
  return reason instanceof Error && reason.message ? reason.message : fallback;
}

/** 计数文案：加载中固定显示「加载中…」；成功/失败时交回调按真实数量格式化。 */
export function countText(state: LoadState<unknown>, ready: () => string): string {
  return state.status === "loading" ? "加载中…" : ready();
}

/** 数组列表的计数简写。 */
export function countLabel<T>(state: LoadState<T[]>, format: (count: number) => string): string {
  return countText(state, () => format(state.status === "success" ? state.data.length : 0));
}
