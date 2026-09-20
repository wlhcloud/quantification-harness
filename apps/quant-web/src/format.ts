/**
 * 全站统一的展示格式化工具（纯函数，可单测）。
 *
 * 背景：DataCenter / BacktestPage / FactorsPage / ModelsPage / SimPage / EtfQuantPage /
 * SelectionPage / StockSimPage 各自复制了一份 fmtNum / fmtPct / fmtMoney，
 * 默认小数位与正负号规则各不相同，同一个数字在不同页面显示口径不一致。
 * 这里收敛成唯一实现，默认值取多数页面的口径；需要其它精度的页面显式传参。
 *
 * 约定：
 * - 所有函数对 null / undefined / NaN 一律返回 "—"；
 * - 名字里带 `fmtPct` 的入参是**比率**（0.1234 → "+12.34%"）；
 * - 入参本身已经是百分数的用 `fmtPercent`（50 → "50.00%"）；
 * - 带 `Trim` 后缀的裁剪尾随 0（1.5 → "1.5"），不带的是定长小数（1.5 → "1.5000"）。
 */

import dayjs from "dayjs";

const isBlank = (v: number | null | undefined): boolean => v == null || Number.isNaN(v);

/** 数值：默认 2 位小数。 */
export const fmtNum = (v: number | null | undefined, digits = 2): string =>
  isBlank(v) ? "—" : Number(v).toFixed(digits);

/** 数值（裁剪尾随 0）：1.5 → "1.5"，1.234567 → "1.2346"（digits=4）。
 *  DataCenter 的因子分位分布用的是这个口径，与 fmtNum 的定长小数刻意区分。 */
export const fmtNumTrim = (v: number | null | undefined, digits = 4): string => {
  if (isBlank(v)) return "—";
  const factor = 10 ** digits;
  return String(Math.round(Number(v) * factor) / factor);
};

/** 比率 → 百分比。signed=false 时不带正号（例如"占比"类指标）。 */
export const fmtPct = (v: number | null | undefined, digits = 2, signed = true): string =>
  isBlank(v) ? "—" : `${signed && (v as number) >= 0 ? "+" : ""}${((v as number) * 100).toFixed(digits)}%`;

/** 已经是百分数的值 → 百分比文本（不乘 100）。 */
export const fmtPercent = (v: number | null | undefined, digits = 2): string =>
  isBlank(v) ? "—" : `${Number(v).toFixed(digits)}%`;

/** 已经是百分数的值 → 百分比文本（裁剪尾随 0）：98.5 → "98.5%"，100 → "100%"。 */
export const fmtPercentTrim = (v: number | null | undefined, digits = 2): string => {
  if (isBlank(v)) return "—";
  const factor = 10 ** digits;
  return `${Math.round(Number(v) * factor) / factor}%`;
};

/** 千分位整数。 */
export const fmtInt = (v: number | null | undefined): string =>
  isBlank(v) ? "—" : Math.round(Number(v)).toLocaleString("zh-CN");

/** 金额：¥ + 千分位 + 2 位小数。 */
export const fmtMoney = (v: number | null | undefined): string =>
  isBlank(v) ? "—" : `¥${Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/** 日期：兼容 "20260911" 与 "2026-09-11T…" 两种上游格式，统一成 YYYY-MM-DD。 */
export const fmtDate = (v: string | null | undefined): string => {
  if (!v) return "—";
  const s = String(v);
  if (s.includes("T")) return s.slice(0, 10);
  if (s.includes("-")) return s.slice(0, 10);
  return s.length >= 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s;
};

/** 日期时间：后端时间戳是 UTC ISO（带 Z），按**浏览器本地时区**渲染成 YYYY-MM-DD HH:mm:ss。
 *  注意不能用字符串切片：那会把 UTC 时间当本地时间显示，东八区会差 8 小时。 */
export const fmtDateTime = (v: string | null | undefined): string =>
  v ? dayjs(v).format("YYYY-MM-DD HH:mm:ss") : "—";

/** 日期时间（分钟精度）：→ YYYY-MM-DD HH:mm。 */
export const fmtMinute = (v: string | null | undefined): string =>
  v ? dayjs(v).format("YYYY-MM-DD HH:mm") : "—";

/** 东财行情页链接（6 开头按沪市，其余按深市）。 */
export const quoteUrl = (code: string): string =>
  `https://quote.eastmoney.com/${code.startsWith("6") ? "sh" : "sz"}${code.slice(0, 6)}.html`;

/** 只取路径里的文件名（后端给的是 Windows 绝对路径）。 */
export const fileBaseName = (path: string | null | undefined): string =>
  path ? (path.split("\\").pop() ?? path) : "—";

/** 把任意值渲染成单行文本（表格里的参数/错误列用）。 */
const toText = (value: unknown): string => {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

/**
 * 同步/引擎 job 的参数摘要：`{a:1,b:"x"}` → "a=1 · b=x"。
 * 兼容"参数被存成 JSON 字符串"和"已经是对象"两种后端返回。
 */
export const fmtParamsText = (value: unknown): string => {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") {
    const raw = value.trim();
    if (!raw) return "—";
    try {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === "object") return fmtParamsText(parsed);
    } catch {
      return raw;
    }
    return raw;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    // React 元素会被 JSON.stringify 成循环结构，直接判掉（原实现同款防御）
    if (entries.some(([key]) => key === "$$typeof")) return "—";
    return entries.map(([key, item]) => `${key}=${toText(item)}`).join(" · ") || "—";
  }
  return String(value) || "—";
};
