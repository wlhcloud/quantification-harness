import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// 后端地址：默认是本机的三个 Python 服务（纯本机开发行为不变）。
// 后端部署到远端服务器时，在 apps/quant-web/.env 里设 VITE_API_HOST=192.168.1.89 即可，
// 不用改这个文件。浏览器始终同源访问 /api/*，因此不涉及 CORS。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  // 优先级：.env 里的 VITE_API_HOST > 进程环境变量 > 本机
  const host = env.VITE_API_HOST || process.env.VITE_API_HOST || '127.0.0.1'
  const target = (port: number) => `http://${host}:${port}`

  return {
    plugins: [react()],
    build: { rolldownOptions: { output: { codeSplitting: { groups: [
      { name: 'react-vendor', test: /node_modules[\\/]react|node_modules[\\/]react-dom/ },
      { name: 'antd-vendor', test: /node_modules[\\/]antd|node_modules[\\/]@ant-design/ },
    ] } } } },
    server: { proxy: {
      // 注意：rewrite 用 String.replace，只替换**第一处**前缀，所以
      //   /api/sync/sync/tools  -> /api/v1/sync/tools   （后端确实在 /api/v1/sync/... 下）
      //   /api/sync/records     -> /api/v1/records      （后端这些端点在 /api/v1/... 下，没有 sync 段）
      // 后端同步服务的路径是混着的（main.py 里 /api/v1/records 与 /api/v1/sync/tools 并存），
      // 因此 api.ts 里对应调用会「多带一段 /sync」。这不是笔误，不要"顺手修掉"。
      '/api/dq': { target: target(9103), changeOrigin: true, rewrite: path => path.replace('/api/dq', '/api/v1') },
      '/api/sync': { target: target(9101), changeOrigin: true, ws: true, rewrite: path => path.replace('/api/sync', '/api/v1') },
      '/api/engine': { target: target(9102), changeOrigin: true, rewrite: path => path.replace('/api/engine', '/api/v1') },
      '/api/settings': { target: target(9101), changeOrigin: true, rewrite: path => path.replace('/api/settings', '/api/v1/settings') },
    } },
  }
})
