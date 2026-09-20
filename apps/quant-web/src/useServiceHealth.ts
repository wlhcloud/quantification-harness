import { useCallback, useEffect, useRef, useState } from "react";

/**
 * 三个 Python 服务的健康探测（走 vite 代理，同源请求）。
 *
 * 原先这段逻辑内联在 ProApp 的 HealthStrip 里，且是**一次性** fetch：
 * 服务后启动/中途挂掉，页面上的徽标永远停在首次结果。这里抽成 hook 并加上轮询，
 * 同时把 9101 的能力开关（minuteSyncEnabled）带出来，供同步中心/数据中心禁用对应入口。
 */

export type HealthState = "loading" | "ok" | "error" | "offline";

export interface ServiceHealth {
  state: HealthState;
  /** 9101 health 返回的能力开关：false 时 /sync/minute* 一律 410 */
  minuteSyncEnabled?: boolean;
  authEnabled?: boolean;
}

export interface ServiceHealthMap {
  sync: ServiceHealth;
  engine: ServiceHealth;
  query: ServiceHealth;
  refresh: () => void;
  allOk: boolean;
}

const PENDING: ServiceHealth = { state: "loading" };

/** 各服务的 health 路径（经 vite 代理到 9101/9102/9103 的 /api/v1/health）。 */
const ENDPOINTS: Array<{ key: keyof Omit<ServiceHealthMap, "refresh" | "allOk">; url: string }> = [
  { key: "sync", url: "/api/sync/health" },
  { key: "engine", url: "/api/engine/health" },
  { key: "query", url: "/api/dq/health" },
];

export function useServiceHealth(pollMs = 30_000): ServiceHealthMap {
  const [health, setHealth] = useState<Record<string, ServiceHealth>>({
    sync: PENDING, engine: PENDING, query: PENDING,
  });
  // 轮询间隔内避免重复请求；同时用于组件卸载后丢弃过期响应
  const mountedRef = useRef(true);

  const probe = useCallback(async () => {
    await Promise.all(ENDPOINTS.map(async ({ key, url }) => {
      let next: ServiceHealth;
      try {
        const response = await fetch(url);
        if (!response.ok) {
          next = { state: "error" };
        } else {
          const body = await response.json() as { status?: string; minuteSyncEnabled?: boolean; authEnabled?: boolean };
          next = {
            state: body.status === "ok" ? "ok" : "error",
            minuteSyncEnabled: body.minuteSyncEnabled,
            authEnabled: body.authEnabled,
          };
        }
      } catch {
        next = { state: "offline" };
      }
      if (mountedRef.current) setHealth((current) => ({ ...current, [key]: next }));
    }));
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void probe();
    if (!pollMs) return () => { mountedRef.current = false; };
    const timer = window.setInterval(() => void probe(), pollMs);
    return () => { mountedRef.current = false; window.clearInterval(timer); };
  }, [probe, pollMs]);

  const refresh = useCallback(() => { void probe(); }, [probe]);

  return {
    sync: health.sync ?? PENDING,
    engine: health.engine ?? PENDING,
    query: health.query ?? PENDING,
    refresh,
    allOk: health.sync?.state === "ok" && health.engine?.state === "ok" && health.query?.state === "ok",
  };
}

/** HealthState → antd Badge 的 status 取值。 */
export const healthBadgeStatus = (state: HealthState): "success" | "processing" | "error" | "warning" =>
  state === "ok" ? "success" : state === "loading" ? "processing" : state === "offline" ? "warning" : "error";

export const healthText = (state: HealthState): string =>
  state === "ok" ? "在线" : state === "loading" ? "检测中" : state === "offline" ? "不可达" : "异常";
