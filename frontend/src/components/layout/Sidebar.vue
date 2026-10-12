<template>
  <div class="sidebar">
    <div class="logo">
      <span>🎬 短视频审核</span>
    </div>
    <nav class="nav-list">
      <!-- 创作者端 -->
      <template v-if="auth.isCreator">
        <RouterLink to="/video/submit" class="nav-item" :class="{ 'nav-item--active': isActive('/video/submit') }">
          <el-icon><VideoCamera /></el-icon>
          <span>视频上传</span>
        </RouterLink>
      </template>

      <!-- 审核员端 -->
      <template v-if="auth.isReviewer">
        <RouterLink to="/video/reviews" class="nav-item" :class="{ 'nav-item--active': isActive('/video/reviews') }">
          <el-icon><View /></el-icon>
          <span>人工复核</span>
        </RouterLink>
        <RouterLink to="/video/dashboard" class="nav-item" :class="{ 'nav-item--active': isActive('/video/dashboard') }">
          <el-icon><DataAnalysis /></el-icon>
          <span>审核看板</span>
        </RouterLink>
      </template>
    </nav>
  </div>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'
import {
  VideoCamera, View, DataAnalysis,
} from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()

// 仅用于子路由高亮（纯视觉反馈，不参与导航逻辑）
// RouterLink 的 active-class 基于路由记录层级，不覆盖平级子路由（如 /video/reviews/:id）
function isActive(prefix: string) {
  return route.path === prefix || route.path.startsWith(prefix + '/')
}
</script>

<style scoped>
.sidebar {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.logo {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 16px;
  font-weight: 600;
  border-bottom: 1px solid #ffffff1a;
  flex-shrink: 0;
}

.nav-list {
  display: flex;
  flex-direction: column;
  padding: 4px 0;
  flex: 1;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 13px 20px;
  color: #ffffffa6;
  text-decoration: none;
  font-size: 14px;
  cursor: pointer;
  transition: background-color 0.2s, color 0.2s;
  user-select: none;
}

.nav-item:hover {
  background-color: #ffffff14;
  color: #fff;
}

.nav-item--active {
  background-color: #1677ff;
  color: #fff;
}

.nav-item .el-icon {
  font-size: 16px;
  flex-shrink: 0;
}

.nav-divider {
  border: none;
  border-top: 1px solid #ffffff1a;
  margin: 8px 0;
}
</style>
