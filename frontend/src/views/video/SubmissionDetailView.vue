<template>
  <div class="submission-detail" v-loading="loading">
    <template v-if="detail">
      <!-- 原视频回放 -->
      <el-card>
        <template #header>
          <div class="header-row">
            <span>🎬 {{ detail.video_meta?.title || '未命名' }}</span>
            <el-tag size="large" :type="statusMeta(detail.status).type">
              {{ statusMeta(detail.status).label }}
            </el-tag>
          </div>
        </template>

        <video
          v-if="detail.content_type !== 'poster'"
          class="media-player"
          :src="mediaUrl(submissionId)"
          controls
          preload="metadata"
        />
        <img
          v-else
          class="media-player poster"
          :src="mediaUrl(submissionId)"
          alt="海报"
        />

        <el-descriptions :column="3" size="small" border style="margin-top: 16px">
          <el-descriptions-item label="内容类型">
            {{ contentTypeLabel[detail.content_type || 'episode'] }}
          </el-descriptions-item>
          <el-descriptions-item label="目标区域">{{ detail.target_region || '—' }}</el-descriptions-item>
          <el-descriptions-item label="提交时间">{{ submittedAt }}</el-descriptions-item>
          <el-descriptions-item v-if="detail.weighted_score !== undefined" label="加权安全分">
            <b>{{ detail.weighted_score?.toFixed(2) }}</b>
          </el-descriptions-item>
          <el-descriptions-item label="描述" :span="detail.weighted_score !== undefined ? 2 : 3">
            {{ detail.video_meta?.description || '—' }}
          </el-descriptions-item>
          <el-descriptions-item v-if="detail.video_meta?.tags?.length" label="标签" :span="3">
            <el-tag v-for="t in detail.video_meta.tags" :key="t" size="small" style="margin-right: 4px">{{ t }}</el-tag>
          </el-descriptions-item>
        </el-descriptions>

        <!-- 操作区 -->
        <div class="actions">
          <el-button
            v-if="detail.status === 'ai_processing' || detail.status === 'pending_review'"
            :icon="Refresh" @click="fetchDetail"
          >刷新状态</el-button>
          <el-button
            v-if="detail.status === 'published_rejected' || detail.status === 'auto_rejected'"
            type="warning" @click="showAppeal = true"
          >发起申诉</el-button>
          <el-button @click="router.push('/video/submit')">返回上传页</el-button>
        </div>

        <el-alert
          v-if="detail.status === 'ai_processing'"
          title="AI 三轨审核进行中，通常 1~3 分钟完成，可点「刷新状态」查看最新进度"
          type="info" :closable="false" style="margin-top: 12px"
        />
        <el-alert
          v-else-if="detail.status === 'pending_review'"
          title="AI 预审核已完成，等待审核员人工复核"
          type="warning" :closable="false" style="margin-top: 12px"
        />
        <el-alert
          v-if="detail.risk_summary"
          :title="detail.risk_summary" type="info" :closable="false" style="margin-top: 12px"
        />
      </el-card>

      <!-- 已发布：六维度审核结论 -->
      <el-card v-if="detail.risk_dimensions?.length" style="margin-top: 16px">
        <template #header><span>📊 六维度审核结论</span></template>
        <el-table :data="detail.risk_dimensions" size="small">
          <el-table-column prop="dimension" label="维度" width="180" />
          <el-table-column label="安全分" width="200">
            <template #default="{ row }">
              <el-progress
                :percentage="Math.round(row.risk_score * 100)"
                :status="row.risk_score < 0.3 ? 'exception' : row.risk_score < 0.6 ? 'warning' : 'success'"
                :stroke-width="10"
              />
            </template>
          </el-table-column>
          <el-table-column prop="details" label="详情" min-width="240" show-overflow-tooltip />
          <el-table-column prop="suggestions" label="修改建议" min-width="200" show-overflow-tooltip />
        </el-table>
      </el-card>
    </template>

    <el-result
      v-else-if="!loading"
      icon="warning" title="无法加载提交详情" sub-title="记录不存在或无权查看"
    >
      <template #extra>
        <el-button type="primary" @click="router.push('/video/submit')">返回上传页</el-button>
      </template>
    </el-result>

    <!-- 申诉对话框 -->
    <el-dialog v-model="showAppeal" title="发起申诉" width="480px">
      <el-input
        v-model="appealReason" type="textarea" :rows="4"
        placeholder="说明你认为误判的理由（英文为佳，会展示给审核员）"
      />
      <template #footer>
        <el-button @click="showAppeal = false">取消</el-button>
        <el-button type="primary" :loading="appealing" :disabled="!appealReason.trim()" @click="handleAppeal">
          提交申诉
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import {
  videoApi,
  mediaUrl,
  contentTypeLabel,
  statusMeta,
  type SubmissionResult,
} from '@/api/video'

const route = useRoute()
const router = useRouter()
const submissionId = route.params.submissionId as string

const loading = ref(true)
const detail = ref<SubmissionResult | null>(null)
const showAppeal = ref(false)
const appealing = ref(false)
const appealReason = ref('')

// mySubmission 的返回里没有 submitted_at，用列表跳过来时不可靠 —— 这里只展示已有字段
const submittedAt = computed(() => (route.query.submitted_at as string) || '—')

let pollTimer: ReturnType<typeof setInterval> | null = null

async function fetchDetail() {
  try {
    detail.value = await videoApi.mySubmission(submissionId)
    // AI 审核中则 5 秒轮询
    if (detail.value.status === 'ai_processing' && !pollTimer) {
      pollTimer = setInterval(fetchDetail, 5000)
    } else if (detail.value.status !== 'ai_processing' && pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  } catch {
    detail.value = null
  } finally {
    loading.value = false
  }
}

async function handleAppeal() {
  appealing.value = true
  try {
    await videoApi.createAppeal(submissionId, appealReason.value.trim())
    ElMessage.success('申诉已提交，审核员将在 1-2 个工作日内处理')
    showAppeal.value = false
  } finally {
    appealing.value = false
  }
}

onMounted(fetchDetail)
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.media-player {
  width: 100%;
  max-height: 420px;
  background: #000;
  border-radius: 8px;
  display: block;
}
.media-player.poster {
  width: auto;
  max-width: 100%;
  max-height: 420px;
  margin: 0 auto;
  background: transparent;
}
.actions {
  margin-top: 16px;
  display: flex;
  gap: 8px;
}
</style>
