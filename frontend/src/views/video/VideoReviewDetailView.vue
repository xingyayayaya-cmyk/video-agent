<template>
  <div class="review-detail" v-loading="loading">
    <template v-if="detail">
      <!-- 原视频回放（复核时必须对照原片） -->
      <el-card>
        <template #header><span>🎬 原视频 · {{ detail.video_meta?.title || '未命名' }}</span></template>
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
      </el-card>

      <!-- 顶部：AI 结论总览 -->
      <el-card style="margin-top: 16px">
        <template #header>
          <div class="header-row">
            <span>🛡️ AI 预审核详情 · {{ detail.video_meta?.title || '未命名' }}</span>
            <el-tag :type="riskTagType(detail.pre_review_assessment.overall_risk)" size="large">
              整体风险：{{ detail.pre_review_assessment.overall_risk }}
            </el-tag>
          </div>
        </template>
        <el-descriptions :column="4" size="small" border>
          <el-descriptions-item label="内容类型">{{ contentTypeLabel[detail.content_type] }}</el-descriptions-item>
          <el-descriptions-item label="目标区域">{{ detail.target_region }}</el-descriptions-item>
          <el-descriptions-item label="加权安全分">
            <b>{{ detail.pre_review_assessment.weighted_score?.toFixed?.(2) ?? '—' }}</b>
          </el-descriptions-item>
          <el-descriptions-item label="分析帧数">{{ detail.frames_count }}</el-descriptions-item>
          <el-descriptions-item label="AI 决策">
            <el-tag size="small" :type="detail.pre_review_assessment.decision === 'auto_approved' ? 'success' : 'warning'">
              {{ detail.pre_review_assessment.decision }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="区域标记" :span="3">
            <el-tag
              v-for="f in detail.pre_review_assessment.region_flags || []"
              :key="f" size="small" type="warning" style="margin-right: 4px"
            >{{ f }}</el-tag>
            <span v-if="!(detail.pre_review_assessment.region_flags || []).length">—</span>
          </el-descriptions-item>
        </el-descriptions>
        <el-alert
          v-if="detail.risk_summary"
          :title="detail.risk_summary"
          type="info"
          :closable="false"
          style="margin-top: 12px"
        />
      </el-card>

      <!-- 证据链：视觉轨违规（帧 + 时间码） -->
      <el-card style="margin-top: 16px">
        <template #header>
          <div class="header-row">
            <span>🖼️ 视觉轨证据（{{ detail.visual_results.violations?.length || 0 }} 条）</span>
            <el-tag size="small" :type="riskTagType(detail.visual_results.overall_risk)">
              {{ detail.visual_results.overall_risk }}
            </el-tag>
          </div>
        </template>
        <el-empty
          v-if="!(detail.visual_results.violations || []).filter(v => v.severity !== 'none').length"
          description="视觉轨未检出有效违规"
        />
        <el-table
          v-else
          :data="(detail.visual_results.violations || []).filter(v => v.severity !== 'none')"
          size="small"
        >
          <el-table-column label="时间码" width="110">
            <template #default="{ row }">
              <el-tag size="small" type="info" class="timecode">{{ formatTs(row.timestamp) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="category" label="类别" width="140" />
          <el-table-column label="严重度" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.severity === 'high' ? 'danger' : row.severity === 'medium' ? 'warning' : 'info'">
                {{ row.severity }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="置信度" width="80">
            <template #default="{ row }">{{ row.confidence?.toFixed(2) }}</template>
          </el-table-column>
          <el-table-column prop="description" label="描述" min-width="260" show-overflow-tooltip />
          <el-table-column label="检出元素" min-width="160">
            <template #default="{ row }">
              <el-tag v-for="e in row.detected_elements || []" :key="e" size="small" style="margin-right: 4px">{{ e }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="detail.visual_results.summary" class="track-summary">
          {{ detail.visual_results.summary }}
        </div>
      </el-card>

      <!-- 音频轨 + 元数据轨 -->
      <el-row :gutter="16" style="margin-top: 16px">
        <el-col :span="12">
          <el-card>
            <template #header>
              <div class="header-row">
                <span>🎙️ 音频轨</span>
                <el-tag size="small" :type="riskTagType(detail.audio_results.overall_risk)">
                  {{ detail.audio_results.overall_risk }}
                </el-tag>
              </div>
            </template>
            <el-empty
              v-if="!(detail.audio_results.violations || []).filter(v => v.severity !== 'none').length"
              description="音频轨未检出有效违规"
            />
            <div v-for="(v, i) in (detail.audio_results.violations || []).filter(v => v.severity !== 'none')" :key="i" class="violation-item">
              <div class="violation-head">
                <el-tag size="small" :type="v.severity === 'high' ? 'danger' : 'warning'">{{ v.severity }}</el-tag>
                <el-tag size="small" type="info">{{ v.category }}</el-tag>
                <span class="sub-text">置信度 {{ v.confidence?.toFixed(2) }}</span>
              </div>
              <blockquote v-if="v.text_segment" class="text-segment">{{ v.text_segment }}</blockquote>
              <div class="sub-text">{{ v.description }}</div>
            </div>
            <div v-if="detail.audio_results.transcript" class="track-summary">
              <details>
                <summary>查看完整转录（{{ detail.audio_results.language_detected }}）</summary>
                <p class="transcript">{{ detail.audio_results.transcript }}</p>
              </details>
            </div>
          </el-card>
        </el-col>
        <el-col :span="12">
          <el-card>
            <template #header>
              <div class="header-row">
                <span>🏷️ 元数据轨</span>
                <el-tag size="small" :type="riskTagType(detail.metadata_results.overall_risk)">
                  {{ detail.metadata_results.overall_risk }}
                </el-tag>
              </div>
            </template>
            <el-empty
              v-if="!(detail.metadata_results.violations || []).filter(v => v.severity !== 'none').length"
              description="元数据未检出有效违规"
            />
            <div v-for="(v, i) in (detail.metadata_results.violations || []).filter(v => v.severity !== 'none')" :key="i" class="violation-item">
              <div class="violation-head">
                <el-tag size="small" :type="v.severity === 'high' ? 'danger' : 'warning'">{{ v.severity }}</el-tag>
                <el-tag size="small" type="info">{{ v.category }}</el-tag>
                <span class="sub-text">置信度 {{ v.confidence?.toFixed(2) }}</span>
              </div>
              <blockquote v-if="v.text_segment" class="text-segment">{{ v.text_segment }}</blockquote>
              <div class="sub-text">{{ v.description }}</div>
            </div>
            <div v-if="detail.metadata_results.summary" class="track-summary">
              {{ detail.metadata_results.summary }}
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 六维度评分 -->
      <el-card style="margin-top: 16px">
        <template #header><span>📊 六维度风险评分</span></template>
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
          <el-table-column label="严重度" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.severity === 'high' ? 'danger' : row.severity === 'medium' ? 'warning' : 'info'">
                {{ row.severity }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="details" label="详情" min-width="240" show-overflow-tooltip />
          <el-table-column prop="suggestions" label="修改建议" min-width="200" show-overflow-tooltip />
        </el-table>
      </el-card>

      <!-- 适用规则上下文 -->
      <el-card style="margin-top: 16px">
        <template #header><span>📜 适用审核规则（{{ detail.target_region }} × {{ contentTypeLabel[detail.content_type] }}）</span></template>
        <pre class="rule-context">{{ detail.review_context }}</pre>
      </el-card>

      <!-- 审核决策区 -->
      <el-card style="margin-top: 16px">
        <template #header><span>✅ 审核员决策</span></template>
        <el-radio-group v-model="action">
          <el-radio-button value="approve">通过（沿用 AI 结论）</el-radio-button>
          <el-radio-button value="modify">修改后通过</el-radio-button>
          <el-radio-button value="reject">拒绝</el-radio-button>
        </el-radio-group>

        <div v-if="action === 'modify'" style="margin-top: 12px">
          <div v-for="d in detail.risk_dimensions" :key="d.dimension" class="modify-row">
            <span class="dim-name">{{ d.dimension }}</span>
            <el-slider
              v-model="modifyScores[d.dimension]"
              :min="0" :max="1" :step="0.05"
              style="width: 260px"
            />
            <span class="sub-text">{{ modifyScores[d.dimension]?.toFixed(2) }}</span>
          </div>
        </div>

        <el-input
          v-model="reviewNotes"
          type="textarea"
          :rows="2"
          placeholder="审核备注（可选，会写入审计记录）"
          style="margin-top: 12px"
        />

        <div style="margin-top: 16px">
          <el-button type="primary" size="large" :loading="confirming" @click="handleConfirm">
            提交决策并发布
          </el-button>
          <el-button size="large" @click="router.back()">返回队列</el-button>
        </div>
      </el-card>
    </template>

    <el-result
      v-else-if="!loading"
      icon="warning"
      title="无法加载审核详情"
      sub-title="该提交可能尚未完成 AI 预审核或记录不存在"
    >
      <template #extra>
        <el-button type="primary" @click="router.push('/video/reviews')">返回待审队列</el-button>
      </template>
    </el-result>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  videoApi,
  mediaUrl,
  contentTypeLabel,
  riskTagType,
  type ReviewDetail,
} from '@/api/video'

const route = useRoute()
const router = useRouter()
const submissionId = route.params.submissionId as string

const loading = ref(true)
const confirming = ref(false)
const detail = ref<ReviewDetail | null>(null)
const action = ref<'approve' | 'modify' | 'reject'>('approve')
const reviewNotes = ref('')
const modifyScores = reactive<Record<string, number>>({})

function formatTs(seconds: number) {
  const m = Math.floor(seconds / 60)
  const s = Math.floor(seconds % 60)
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

async function fetchDetail() {
  loading.value = true
  try {
    const res = await videoApi.reviewDetail(submissionId)
    detail.value = res
    // 初始化维度修改滑块为 AI 评分
    for (const d of res.risk_dimensions) {
      modifyScores[d.dimension] = d.risk_score
    }
  } catch {
    detail.value = null
  } finally {
    loading.value = false
  }
}

async function handleConfirm() {
  const actionText = { approve: '通过', modify: '修改后通过', reject: '拒绝' }[action.value]
  try {
    await ElMessageBox.confirm(
      `确认${actionText}该内容？决策将立即发布并通知创作者。`,
      '提交审核决策',
      { type: 'warning' },
    )
  } catch {
    return
  }

  confirming.value = true
  try {
    const modifications = action.value === 'modify'
      ? Object.entries(modifyScores).map(([dimension, risk_score]) => ({
          dimension,
          risk_score,
          severity: risk_score < 0.3 ? 'high' : risk_score < 0.6 ? 'medium' : 'low',
        }))
      : []

    const res = await videoApi.confirm(submissionId, {
      action: action.value,
      modifications,
      review_notes: reviewNotes.value,
    })

    ElMessage.success(`已发布：${res.final_decision === 'approved' ? '通过' : '拒绝'}`)
    router.push('/video/reviews')
  } finally {
    confirming.value = false
  }
}

onMounted(fetchDetail)
</script>

<style scoped>
.media-player {
  width: 100%;
  max-height: 380px;
  background: #000;
  border-radius: 8px;
  display: block;
}
.media-player.poster {
  width: auto;
  max-width: 100%;
  max-height: 380px;
  margin: 0 auto;
  background: transparent;
}
.header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.timecode {
  font-family: monospace;
}
.track-summary {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--el-border-color);
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
.violation-item {
  padding: 8px 0;
  border-bottom: 1px dashed var(--el-border-color-lighter);
}
.violation-item:last-child {
  border-bottom: none;
}
.violation-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}
.text-segment {
  margin: 4px 0;
  padding: 6px 10px;
  border-left: 3px solid var(--el-color-warning);
  background: var(--el-fill-color-light);
  font-size: 12px;
  color: var(--el-text-color-regular);
}
.transcript {
  white-space: pre-wrap;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  max-height: 200px;
  overflow-y: auto;
}
.rule-context {
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  padding: 12px;
  border-radius: 6px;
  max-height: 320px;
  overflow-y: auto;
  margin: 0;
}
.modify-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 0;
}
.dim-name {
  width: 170px;
  font-size: 13px;
  font-family: monospace;
}
.sub-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
