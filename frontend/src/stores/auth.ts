import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

// 出海 AI 短视频审核平台 · 角色
//   creator  创作者：上传视频/海报、查看审核进度、发起申诉
//   reviewer 审核员：人工复核、审核看板、处理申诉
//   admin    管理员：以上全部
export type UserRole = 'creator' | 'reviewer' | 'admin'

interface UserInfo {
  userId: string
  role: UserRole
  tenantId: string
  username?: string
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem('edu-agent-token'))
  const user = ref<UserInfo | null>((() => {
    try {
      return JSON.parse(localStorage.getItem('edu-agent-user') ?? 'null')
    } catch {
      return null
    }
  })())

  const isLoggedIn = computed(() => !!token.value)

  // 创作者（admin 恒有权）
  const isCreator = computed(() =>
    user.value?.role === 'creator' || user.value?.role === 'admin'
  )

  // 审核员（admin 恒有权）
  const isReviewer = computed(() =>
    user.value?.role === 'reviewer' || user.value?.role === 'admin'
  )

  function login(accessToken: string, userInfo: UserInfo) {
    token.value = accessToken
    user.value = userInfo
    localStorage.setItem('edu-agent-token', accessToken)
    localStorage.setItem('edu-agent-user', JSON.stringify(userInfo))
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem('edu-agent-token')
    localStorage.removeItem('edu-agent-user')
  }

  return { token, user, isLoggedIn, isCreator, isReviewer, login, logout }
})
