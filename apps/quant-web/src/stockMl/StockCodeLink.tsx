import { quoteUrl } from "./format";

/** 股票代码 → 东财行情页链接（持仓表 / 候选池表两处原本各写一遍同样的 <a>） */
export function StockCodeLink({ code }: { code: string }) {
  return (
    <a href={quoteUrl(code)} target="_blank" rel="noreferrer"
      style={{ color: "#1677ff", fontFamily: "monospace" }}>
      {code}
    </a>
  );
}
