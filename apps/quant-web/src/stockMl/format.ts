/**
 * 股票 ML 子模块的展示口径适配层。
 *
 * 实现已统一到 `src/format.ts`（全站唯一实现）；本文件只固化本模块的历史口径：
 * - 百分比**不带正号**（收益列已用颜色区分正负，再加号会挤）；
 * - 数值默认 **4 位**小数（RankIC / ICIR 量级小，2 位看不出差别）。
 *
 * 之所以保留这一层而不是让调用点直接传参：口径是模块级约定，
 * 集中在这里声明一次，比散落在 6 个组件的调用处更容易审计。
 */
import { fileBaseName as baseName, fmtDate, fmtNum as num, fmtPct as pct, quoteUrl as url } from "../format";

/** 百分比：入参是比率（0.1234 → "12.34%"），不带正号。 */
export const fmtPct = (v: number | null | undefined, digits = 2) => pct(v, digits, false);

/** 数值：默认 4 位小数。 */
export const fmtNum = (v: number | null | undefined, digits = 4) => num(v, digits);

/** 日期：兼容紧凑日期与 ISO 时间戳，统一成 YYYY-MM-DD。 */
export const dateText = fmtDate;

/** 只取路径里的文件名（后端给的是 Windows 绝对路径 modelFile）。 */
export const fileBaseName = baseName;

/** 东财行情页链接。 */
export const quoteUrl = url;
