import "./skeleton.css";

/** 表格骨架行：复用真实 .tr 的网格列宽（columns 与列数一致），尺寸与真实行相同。 */
export function SkeletonRows({ rows, columns }: { rows: number; columns: number }) {
  const widths = [72, 46, 58, 40, 64, 52, 38, 60];
  return (
    <div className="skeletonRows" aria-hidden="true">
      {Array.from({ length: rows }, (_, row) => (
        <div className="tr skeletonRow" key={row}>
          {Array.from({ length: columns }, (_, column) => (
            <span className="skeletonLine" key={column} style={{ width: `${widths[column % widths.length]}%` }} />
          ))}
        </div>
      ))}
    </div>
  );
}

/** 非表格列表骨架（对话列表等），每项两行。 */
export function SkeletonList({ rows }: { rows: number }) {
  return (
    <div className="skeletonList" aria-hidden="true">
      {Array.from({ length: rows }, (_, row) => (
        <div className="skeletonItem" key={row}>
          <span className="skeletonLine wide" />
          <span className="skeletonLine short" />
        </div>
      ))}
    </div>
  );
}

/** 卡片骨架（指标卡 / 知识库卡）：图标块 + 两行；外层 className 复用真实网格（如 dashboardMetrics）。 */
export function SkeletonCards({ count, className = "" }: { count: number; className?: string }) {
  return (
    <div className={`skeletonCards ${className}`.trim()} aria-hidden="true">
      {Array.from({ length: count }, (_, index) => (
        <div className="skeletonCard" key={index}>
          <span className="skeletonBlock skeletonIcon" />
          <span className="skeletonStack">
            <span className="skeletonLine" style={{ width: "46%" }} />
            <span className="skeletonLine" style={{ width: "64%" }} />
          </span>
        </div>
      ))}
    </div>
  );
}

/** 加载失败：错误提示 + 重试按钮（复用现有请求函数）。 */
export function LoadError({
  message,
  onRetry,
  retryLabel = "重试",
}: {
  message: string;
  onRetry: () => void;
  retryLabel?: string;
}) {
  return (
    <div className="loadError" role="alert">
      <span>{message}</span>
      <button type="button" onClick={onRetry}>
        {retryLabel}
      </button>
    </div>
  );
}
