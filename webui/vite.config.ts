import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  build: {
    outDir: '../api/webui',
    emptyOutDir: true,
  },
  server: {
    // 明确绑定 IPv4：Node 17+ 解析 localhost 优先走 IPv6，
    // 部分 Windows 机器 IPv6 回环不可用会导致浏览器无法访问
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8080',
        changeOrigin: true,
        ws: true,  // 启用 WebSocket 代理
      },
    },
  },
})
