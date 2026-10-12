<template>
  <div class="dashboard">
    <!-- 核心指标卡（点击下钻到对应状态列表） -->
    <el-row :gutter="16">
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card" @click="goList('all')">
          <div class="stat-num">{{ stats?.total ?? '—' }}</div>
          <div class="stat-label">累计提交</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card" @click="goList('auto_approved')">
          <div class="stat-num" style="color: var(--el-color-success)">{{ stats?.auto_approved ?? '—' }}</div>
          <div class="stat-label">自动通过（{{ pct(stats?.rates.auto_approve_rate) }}）</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card" @click="goList('auto_rejected')">
          <div class="stat-num" style="color: var(--el-color-danger)">{{ stats?.auto_rejected ?? '—' }}</div>
          <div class="stat-label">自动拒绝（{{ pct(stats?.rates.auto_reject_rate) }}）</div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover" class="stat-card" @click="goList('pending_review')">
          <div class="stat-num" style="color: var(--el-color-warning)">{{ stats?.pending_review ?? '—' }}</div>
          <div class="stat-label">待人工复核（{{ pct(stats?.rates.manual_review_rate) }}）</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 二级指标 + 申诉队列 -->
    <el-row :gutter="16" style="margin-top: 16px">
      <el-col :span="10">
        <el-card>
          <template #header><span>📈 运营指标</span></template>
          <el-descriptions :column="1" size="small" border>
            <el-descriptions-item label="平均安全分">
              {{ stats?.avg_weighted_score?.toFixed(3) ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="平均审核时长">
              {{ stats?.avg_review_seconds ? (stats.avg_review_seconds / 60).toFixed(1) + ' 分钟' : '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="已发布">{{ stats?.published ?? 0 }}</el-descriptions-item>
            <el-descriptions-item label="审核失败">{{ stats?.failed ?? 0 }}</el-descriptions-item>
            <el-descriptions-item label="内容类型分布">
              <el-tag
                v-for="(cnt, ct) in stats?.by_content_type || {}"
                :key="ct" size="small" style="margin-right: 6px"
              >{{ contentTypeLabel[ct as ContentType] || ct }}: {{ cnt }}</el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="申诉">
              共 {{ stats?.appeals.total ?? 0 }} 起，推翻 {{ stats?.appeals.overturned ?? 0 }} 起
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
      <el-col :span="14">
        <el-card>
          <template #header>
            <div class="header-row">
              <span>📮 申诉队列</span>
              <el-radio-group v-model="appealStatus" size="small" @change="fetchAppeals">
                <el-radio-button value="open">待处理</el-radio-button>
                <el-radio-button value="under_review">处理中</el-radio-button>
                <el-radio-button value="overturned">已推翻</el-radio-button>
                <el-radio-button value="upheld">已维持</el-radio-button>
              </el-radio-group>
            </div>
          </template>
          <el-empty v-if="!appeals.length" description="该状态下暂无申诉" />
          <div v-for="a in appeals" :key="a.appeal_id" class="appeal-item">
            <div class="appeal-head">
              <b>{{ a.video_title }}</b>
              <el-tag size="small">{{ contentTypeLabel[a.content_type] }}</el-tag>
              <el-tag size="small" type="info">{{ a.creator_name }}</el-tag>
              <el-tag size="small" :type="a.final_decision === 'rejected' ? 'danger' : 'warning'">
                AI 分 {{ a.weighted_score?.toFixed(2) ?? '—' }}
              </el-tag>
              <span class="sub-text">{{ a.created_at?.slice(0, 19).replace('T', ' ') }}</span>
            </div>
            <div class="appeal-reason">{{ a.reason }}</div>
            <div v-if="a.resolution_note" class="sub-text">处理备注：{{ a.resolution_note }}</div>
            <div v-if="a.status === 'open'" class="appeal-actions">
              <el-button size="small" type="success" @click="handleResolve(a, 'overturned')">推翻 → 改判通过</el-button>
              <el-button size="small" type="danger" @click="handleResolve(a, 'upheld')">维持拒绝</el-button>
              <el-button size="small" @click="goReview(a.submission_id)">查看审核详情</el-button>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  videoApi,
  contentTypeLabel,
  type DashboardStats,
  type AppealItem,
  type ContentType,
} from '@/api/video'

const router = useRouter()
const stats = ref<DashboardStats | null>(null)
const appeals = ref<AppealItem[]>([])
const appealStatus = ref('open')

function pct(rate: number | undefined) {
  return rate !== undefined ? (rate * 100).toFixed(1) + '%' : '—'
}

async function fetchStats() {
  try {
    stats.value = await videoApi.dashboardStats()
  } catch { /* 拦截器已提示 */ }
}

async function fetchAppeals() {
  try {
    appeals.value = (await videoApi.appeals(appealStatus.value)).items
  } catch { /* 拦截器已提示 */ }
}

function goReview(submissionId: string) {
  router.push(`/video/reviews/${submissionId}`)
}

function goList(status: string) {
  router.push({ path: '/video/reviews', query: { status } })
}

async function handleResolve(appeal: AppealItem, resolution: 'upheld' | 'overturned') {
  let note = ''
  try {
    const { value } = await ElMessageBox.prompt(
      resolution === 'overturned'
        ? '推翻 AI 拒绝结论，该内容将改判为「通过」。请填写处理依据：'
        : '维持拒绝结论。请填写处理依据：',
      '处理申诉',
      { inputPlaceholder: '处理依据（必填）', inputValidator: v => !!v?.trim() || '请填写处理依据' },
    )
    note = value
  } catch {
    return
  }

  try {
    await videoApi.resolveAppeal(appeal.appeal_id, resolution, note)
    ElMessage.success(resolution === 'overturned' ? '已推翻并改判通过' : '已维持拒绝')
    await Promise.all([fetchAppeals(), fetchStats()])
  } catch { /* 拦截器已提示 */ }
}

onMounted(() => {
  fetchStats()
  fetchAppeals()
})
</script>

<style scoped>
.stat-card {
  cursor: pointer;
  transition: transform 0.15s ease;
}
.stat-card:hover {
  transform: translateY(-2px);
}
.stat-num {
  font-size: 32px;
  font-weight: 600;
  text-align: center;
}
.stat-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
  text-align: center;
  margin-top: 4px;
}
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.appeal-item {
  padding: 10px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}
.appeal-item:last-child {
  border-bottom: none;
}
.appeal-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.appeal-reason {
  margin: 6px 0;
  font-size: 13px;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  padding: 8px 10px;
  border-radius: 4px;
}
.appeal-actions {
  margin-top: 8px;
  display: flex;
  gap: 8px;
}
.sub-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
