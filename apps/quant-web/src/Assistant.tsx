import { PageContainer } from "@ant-design/pro-components";

/**
 * 量化助手：内嵌 DSH Agent 宿主（3081）。
 *
 * 原先硬编码 `http://127.0.0.1:3081`：一旦按 apps/quant-web/.env 把 VITE_API_HOST 指向
 * 远端后端（例如 192.168.1.89），iframe 仍指向**访问者本机**，必然白屏。
 * 现在默认取当前页面同主机名的 3081 端口，可用 VITE_ASSISTANT_URL 覆盖。
 *
 * 这里刻意不走 vite 代理：DSH 是挂在 `/` 的 SPA，代理到子路径会导致它引用的
 * `/assets/*` 被本应用的 dev server 接管。同主机端口直连没有这个问题。
 */
const ASSISTANT_URL =
  (import.meta.env.VITE_ASSISTANT_URL as string | undefined) ??
  `${window.location.protocol}//${window.location.hostname}:3081`;

export default function Assistant() {
  return (
    <PageContainer
      title="量化助手"
      subTitle="嵌入式智能体，用于对话与执行量化任务"
    >
      <iframe
        src={ASSISTANT_URL}
        title="量化助手"
        style={{
          width: "100%",
          height: "calc(100vh - 180px)",
          border: "none",
          borderRadius: 6,
          background: "#fff",
        }}
      />
    </PageContainer>
  );
}
