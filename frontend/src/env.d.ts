/// <reference types="vite/client" />

// Vite 环境变量类型声明
// 用途：让 `import.meta.env.XXX` 在 TypeScript 下类型正确
// （此前缺失该文件，vue-tsc 会报 "Property 'env' does not exist on type 'ImportMeta'"）
interface ImportMetaEnv {
  /** 后端 API 基础地址（不设则走同源，由 vite dev server 代理转发） */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
