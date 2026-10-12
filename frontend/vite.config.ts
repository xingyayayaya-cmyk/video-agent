import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 3000,
    // 公网隧道域名放行（Vite 5.4+ 的 DNS 重绑定防护默认拦截陌生 Host，不配置隧道访问会 403）
    allowedHosts: ['.trycloudflare.com', '.lhr.life'],
    proxy: {
      '/api': {
        // 用 127.0.0.1 而非 localhost：Node 17+ 不再重排 DNS 结果，
        // localhost 可能解析为 ::1，而后端 uvicorn 监听 127.0.0.1，会代理失败
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
