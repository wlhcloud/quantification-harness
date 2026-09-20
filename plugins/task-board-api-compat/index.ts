/** Compatibility face for task-board 0.3.x on the current Typert-based DSH Host. */
import { randomUUID } from 'node:crypto'
import type { Context } from '@deepseek-ai/cordis'

interface RpcRequest<T> { rpcId: string; payload: T }
interface SessionEvent { type: string; seq: number; time: number; data: unknown }
interface LiveSession { id: string; events: SessionEvent[] }
interface SessionController {
  list(request: Record<string, unknown>, signal: AbortSignal): Promise<{ items: unknown[] }>
  create(request: Record<string, unknown>): Promise<unknown>
  rename(request: Record<string, unknown>): Promise<unknown>
  prompt(request: Record<string, unknown>, signal: AbortSignal): Promise<unknown>
}
interface SessionStore { get(id: string): LiveSession | undefined }
interface AgentRegistry { get(id: string): { status: string } | undefined }
interface Workspace { id: string; path?: string; title?: string }
interface WorkspaceRegistry { list(): Workspace[] }
interface AgentPresets { remoteExportList(): Promise<unknown> }

declare module '@deepseek-ai/cordis' {
  interface Context {
    sessionController: SessionController
    sessions: SessionStore
    agents: AgentRegistry
    workspaceRegistry: WorkspaceRegistry
    agentPresets: AgentPresets
    apiProxy: unknown
  }
}

export const name = 'dsh-quant-task-board-api-compat'
export const inject = ['sessionController', 'sessions', 'agents', 'workspaceRegistry', 'agentPresets']

const success = <T>(rpcId: string, value: T) => ({ rpcId, result: { ok: true as const, value } })
const failure = (rpcId: string, error: unknown) => ({
  rpcId,
  result: {
    ok: false as const,
    error: { code: 'compat-error', message: error instanceof Error ? error.message : String(error) },
  },
})

export function apply(ctx: Context): void {
  const run = async <T, R>(request: RpcRequest<T>, operation: (payload: T) => Promise<R>): Promise<unknown> => {
    try { return success(request.rpcId, await operation(request.payload)) }
    catch (error) { return failure(request.rpcId, error) }
  }
  const apiProxy = {
    sessions: {
      list: (request: RpcRequest<Record<string, unknown>>) => run(request, payload => ctx.sessionController.list(payload, new AbortController().signal)),
      create: (request: RpcRequest<Record<string, unknown>>) => run(request, payload => ctx.sessionController.create(payload)),
      rename: (request: RpcRequest<Record<string, unknown>>) => run(request, payload => ctx.sessionController.rename(payload)),
      prompt: (request: RpcRequest<Record<string, unknown>>) => run(request, payload => ctx.sessionController.prompt({ requestId: `task-board-${randomUUID()}`, ...payload }, new AbortController().signal)),
      history: (request: RpcRequest<{ sessionId: string; maxMessages?: number; beforeSeq?: number }>) => run(request, async payload => {
        const session = ctx.sessions.get(payload.sessionId)
        if (session === undefined) throw new Error(`session is not live: ${payload.sessionId}`)
        const before = payload.beforeSeq ?? Number.MAX_SAFE_INTEGER
        const limit = Math.max(1, Math.min(500, payload.maxMessages ?? 100))
        const events = session.events.filter(event => event.seq < before).slice(-limit).reverse().map(event => ({ event }))
        const oldest = events.at(-1)?.event.seq
        return { events, hasMore: oldest !== undefined && session.events.some(event => event.seq < oldest) }
      }),
    },
    workspace: {
      list: (request: RpcRequest<Record<string, never>>) => run(request, async () => ({
        items: ctx.workspaceRegistry.list().map(item => ({ workspaceId: item.id, title: item.title ?? item.path ?? item.id, path: item.path })),
      })),
    },
    agentPresets: {
      list: (request: RpcRequest<Record<string, never>>) => run(request, () => ctx.agentPresets.remoteExportList()),
    },
  }
  ctx.provide('apiProxy', apiProxy)
}
