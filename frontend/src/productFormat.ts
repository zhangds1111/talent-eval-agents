export type MarginTone = "good" | "warn" | "bad" | "none";

/** 毛利率色阶：≥40% 健康，25–40% 需关注，<25% 偏低。 */
export function marginTone(percent: number | null | undefined): MarginTone {
  if (percent == null || Number.isNaN(percent)) return "none";
  if (percent >= 40) return "good";
  if (percent >= 25) return "warn";
  return "bad";
}

export type CertificationStatus = "evidenced" | "missing" | "expired" | "model_mismatch";

const STATUS_RULES: Array<[CertificationStatus, RegExp]> = [
  ["model_mismatch", /model_mismatch|型号不符|型号不匹配/],
  ["expired", /expired|已过期/],
  ["missing", /\bmissing\b|缺有效证据|缺少|未提供/],
  ["evidenced", /evidenced|已有有效证据|证据有效/],
];

export const CERTIFICATION_LABELS: Record<CertificationStatus, string> = {
  evidenced: "有效",
  missing: "缺失",
  expired: "已过期",
  model_mismatch: "型号不符",
};

/** 从报告条目文字中识别认证状态（具体状态优先于泛化的"缺失"）。 */
export function certificationStatus(text: string): CertificationStatus | null {
  for (const [status, pattern] of STATUS_RULES) {
    if (pattern.test(text)) return status;
  }
  return null;
}

export type ReportSectionKind = "program" | "risk" | null;

export function reportSectionKind(heading: string): ReportSectionKind {
  if (/程序核算/.test(heading)) return "program";
  if (/风险|缺失/.test(heading)) return "risk";
  return null;
}
