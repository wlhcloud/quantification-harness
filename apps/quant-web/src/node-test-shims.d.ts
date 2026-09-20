/**
 * 单测用的极简环境声明。
 *
 * 前端单测刻意保持"零新依赖"：跑法是 `node --import tsx --test src/*.test.ts`，
 * 断言用 Node 内置的 `node:test` / `node:assert/strict`。但仓库没有装 `@types/node`
 * （装了就得进 package.json 并改 lockfile），于是 `tsc -b` 会报
 * TS2591 "Cannot find name 'node:assert/strict'" —— 也就是 CI 的 web job 会红。
 *
 * 这里只声明测试实际用到的那几个 API，运行时由 Node 内置模块提供，与声明无关。
 * 新增用法时补一行即可（漏声明会直接编译报错，不会静默放过）。
 */
declare module "node:test" {
  export function test(name: string, fn: () => void | Promise<void>): void;
  export function describe(name: string, fn: () => void): void;
  export function it(name: string, fn: () => void | Promise<void>): void;
}

declare module "node:assert/strict" {
  interface AssertStrict {
    equal(actual: unknown, expected: unknown, message?: string): void;
    deepEqual(actual: unknown, expected: unknown, message?: string): void;
    notDeepEqual(actual: unknown, expected: unknown, message?: string): void;
    match(value: string, pattern: RegExp, message?: string): void;
    ok(value: unknown, message?: string): void;
    throws(fn: () => unknown, message?: string): void;
  }
  const assert: AssertStrict;
  export default assert;
}
