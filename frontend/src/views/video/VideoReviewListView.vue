<template>
  <div class="review-list">
    <el-card>
      <template #header>
        <span>🛡️ {{ pageTitle }}</span>
        <el-button style="float: right" :icon="Refresh" circle size="small" @click="fetchList" />
      </template>

      <el-empty v-if="!items.length" description="暂无待审任务，AI 自动分流已处理完毕" />
      <el-table v-else :data="items" size="default">
        <el-table-column label="视频" min-width="220">
          <template #default="{ row }">
            <div class="video-title">{{ row.video_title }}</div>
            <div class="sub-text">
              {{ row.series_title || '独立内容' }}
              <template v-if="row.episode_no"> · 第 {{ row.episode_no }} 集</template>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100">
          <template #default="{ row }: { row: PendingItem }">
            <el-tag size="small" :type="row.content_type === 'clip' ? 'warning' : row.content_type === 'poster' ? 'success' : 'primary'">
              {{ contentTypeLabel[row.content_type] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="creator_name" label="创作者" width="110" />
        <el-table-column prop="target_region" label="区域" width="70" />
        <el-table-column v-if="statusFilter !== 'pending_review'" label="状态" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="statusMeta(row.status).type">{{ statusMeta(row.status).label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="AI 预评分" width="100">
          <template #default="{ row }">
            <span :class="scoreClass(row.pre_review.weighted_score)">
              {{ row.pre_review.weighted_score?.toFixed?.(2) ?? '—' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="整体风险" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="riskTagType(row.pre_review.overall_risk)">
              {{ row.pre_review.overall_risk }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="高风险维度" min-width="200">
          <template #default="{ row }: { row: PendingItem }">
            <el-tag
              v-for="d in topRiskDims(row.risk_dimensions)"
              :key="d.dimension"
              size="small"
              :type="d.severity === 'high' ? 'danger' : 'warning'"
              style="margin-right: 4px"
            >{{ d.dimension }}</el-tag>
            <span v-if="!row.risk_dimensions.length" class="sub-text">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="submitted_at" label="提交时间" width="170" />
        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button v-if="statusFilter === 'pending_review'" type="primary" size="small" @click="goDetail(row.submission_id)">
              开始复核
            </el-button>
            <el-button v-else size="small" @click="goDetail(row.submission_id)">查看</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Refresh } from '@element-plus/icons-vue'
import { videoApi, contentTypeLabel, riskTagType, statusMeta, type PendingItem, type RiskDimension } from '@/api/video'

const route = useRoute()
const router = useRouter()
const items = ref<PendingItem[]>([])

// 看板下钻：?status=auto_approved / auto_rejected / all ...（默认待审队列）
const statusFilter = computed(() => (route.query.status as string) || 'pending_review')

const statusTitles: Record<string, string> = {
  pending_review: '待人工复核队列',
  auto_approved: '自动通过记录',
  auto_rejected: '自动拒绝记录',
  published_approved: '已发布（通过）',
  published_rejected: '已发布（拒绝）',
  ai_processing: 'AI 审核中',
  failed: '审核失败',
  all: '全部提交记录',
}
const pageTitle = computed(() => statusTitles[statusFilter.value] || '待人工复核队列')

function topRiskDims(dims: RiskDimension[]) {
  return dims.filter(d => d.severity === 'high' || d.severity === 'medium').slice(0, 3)
}

function scoreClass(score: number | undefined) {
  if (score === undefined) return ''
  if (score < 0.3) return 'score-low'
  if (score < 0.6) return 'score-mid'
  return 'score-high'
}

function goDetail(submissionId: string) {
  router.push(`/video/reviews/${submissionId}`)
}

async function fetchList() {
  try {
    items.value = (await videoApi.pendingReviews(statusFilter.value)).items
  } catch { /* 错误已由拦截器提示 */ }
}

onMounted(fetchList)
</script>

<style scoped>
.video-title {
  font-weight: 500;
}
.sub-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.score-low { color: var(--el-color-danger); font-weight: 600; }
.score-mid { color: var(--el-color-warning); font-weight: 600; }
.score-high { color: var(--el-color-success); font-weight: 600; }
</style>
