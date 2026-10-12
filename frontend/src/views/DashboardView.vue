<template>
  <div class="dashboard">
    <div class="welcome">
      <h2>欢迎回来，{{ auth.user?.username ?? auth.user?.userId }}</h2>
      <p>出海 AI 短视频审核平台</p>
    </div>

    <!-- 主入口（按角色突出展示） -->
    <el-row :gutter="16" style="margin-bottom: 16px">
      <el-col :span="24">
        <el-card
          v-if="primary"
          class="ai-assistant-card"
          shadow="hover"
          @click="router.push(primary.route)"
        >
          <div class="ai-card-content">
            <div class="ai-card-left">
              <span class="ai-icon">{{ primary.icon }}</span>
              <div>
                <div class="ai-title">{{ primary.title }}</div>
                <div class="ai-desc">{{ primary.desc }}</div>
              </div>
            </div>
            <el-button type="primary" size="default">{{ primary.action }} →</el-button>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 功能入口 -->
    <el-row :gutter="16" class="feature-cards">
      <el-col :span="8" v-for="card in featureCards" :key="card.route">
        <el-card
          class="feature-card"
          shadow="hover"
          @click="router.push(card.route)"
        >
          <div class="card-icon">{{ card.icon }}</div>
          <div class="card-title">{{ card.title }}</div>
          <div class="card-desc">{{ card.desc }}</div>
          <el-button type="primary" plain size="small" style="margin-top: 12px">
            {{ card.action }}
          </el-button>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

// 主入口卡：审核员优先看复核队列，创作者优先看上传
const primary = computed(() => {
  if (auth.isReviewer) {
    return {
      icon: '🛡️',
      title: '人工复核队列',
      desc: 'AI 预审核已完成，等待你复核确认 — 查看三轨证据链与六维度风险评分，做出最终决策',
      action: '开始复核',
      route: '/video/reviews',
    }
  }
  if (auth.isCreator) {
    return {
      icon: '🎬',
      title: '上传视频 / 海报',
      desc: '支持正片短剧、投流切片、封面海报三类内容，提交后自动触发 AI 三轨审核',
      action: '立即上传',
      route: '/video/submit',
    }
  }
  return null
})

// 功能入口：按角色显隐
const featureCards = computed(() => {
  const cards = []

  if (auth.isCreator) {
    cards.push({
      icon: '🎬',
      title: '视频上传',
      desc: '正片 / 投流切片 / 封面海报，提交即触发 AI 审核',
      action: '去上传',
      route: '/video/submit',
    })
  }

  if (auth.isReviewer) {
    cards.push(
      {
        icon: '🛡️',
        title: '人工复核',
        desc: '复核 AI 预审核结论，approve / modify / reject 三级决策',
        action: '去复核',
        route: '/video/reviews',
      },
      {
        icon: '📊',
        title: '审核看板',
        desc: '三级分流占比、平均评分、审核时效与申诉统计',
        action: '看数据',
        route: '/video/dashboard',
      },
    )
  }

  return cards
})
</script>

<style scoped>
.dashboard {
  max-width: 1100px;
}
.welcome {
  margin-bottom: 24px;
}
.welcome h2 {
  margin: 0 0 4px;
  font-size: 22px;
}
.welcome p {
  margin: 0;
  color: #8c8c8c;
}
.ai-assistant-card {
  cursor: pointer;
  background: linear-gradient(135deg, #f0f7ff 0%, #e6f4ff 100%);
  border: 1px solid #bae0ff;
  transition: transform 0.2s;
}
.ai-assistant-card:hover { transform: translateY(-2px); }
.ai-card-content {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 0;
}
.ai-card-left {
  display: flex;
  align-items: center;
  gap: 16px;
}
.ai-icon { font-size: 36px; }
.ai-title {
  font-size: 17px;
  font-weight: 600;
  color: #1677ff;
  margin-bottom: 4px;
}
.ai-desc {
  font-size: 13px;
  color: #595959;
}
.feature-card {
  cursor: pointer;
  text-align: center;
  padding: 8px 0;
  transition: transform 0.2s;
}
.feature-card:hover {
  transform: translateY(-2px);
}
.card-icon {
  font-size: 40px;
  margin-bottom: 12px;
}
.card-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 8px;
}
.card-desc {
  font-size: 13px;
  color: #8c8c8c;
  line-height: 1.5;
  min-height: 48px;
}
</style>
