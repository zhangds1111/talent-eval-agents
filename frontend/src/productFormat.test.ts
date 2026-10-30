import { describe, expect, it } from "vitest";

import { certificationStatus, marginTone, reportSectionKind } from "./productFormat";

describe("productFormat", () => {
  it("grades gross margin", () => {
    expect(marginTone(44)).toBe("good");
    expect(marginTone(30)).toBe("warn");
    expect(marginTone(12.5)).toBe("bad");
    expect(marginTone(null)).toBe("none");
  });

  it("detects certification status with specific states first", () => {
    expect(certificationStatus("FCC Part 15：缺有效证据：型号不符（CL-100）")).toBe("model_mismatch");
    expect(certificationStatus("UN38.3：缺有效证据：已过期")).toBe("expired");
    expect(certificationStatus("缺少 SDS/MSDS 电池安全文件")).toBe("missing");
    expect(certificationStatus("毛利率 44.0%")).toBeNull();
  });

  it("classifies report sections", () => {
    expect(reportSectionKind("程序核算（非模型生成）")).toBe("program");
    expect(reportSectionKind("风险与缺失")).toBe("risk");
    expect(reportSectionKind("主要依据")).toBeNull();
  });
});
