<template>
  <div class="video-submit">
    <!-- 上传表单 -->
    <el-card>
      <template #header><span>🎬 上传视频 / 海报</span></template>

      <el-form label-width="110px" style="max-width: 640px">
        <el-form-item label="内容类型" required>
          <el-radio-group v-model="form.content_type">
            <el-radio-button value="episode">正片短剧</el-radio-button>
            <el-radio-button value="clip">投流切片</el-radio-button>
            <el-radio-button value="poster">封面海报</el-radio-button>
          </el-radio-group>
          <div class="form-tip">{{ contentTypeTip }}</div>
        </el-form-item>

        <el-form-item v-if="form.content_type === 'episode'" label="所属剧集" required>
          <el-select
            v-model="form.series_id"
            placeholder="选择剧集（可先创建）"
            style="width: 100%"
            filterable
          >
            <el-option
              v-for="s in seriesList"
              :key="s.series_id"
              :label="`${s.title}（${s.episode_count} 集）`"
              :value="s.series_id"
            />
          </el-select>
          <el-button text type="primary" size="small" @click="showCreateSeries = true">
            + 新建剧集
          </el-button>
        </el-form-item>

        <el-form-item v-if="form.content_type === 'episode'" label="集号" required>
          <el-input-number v-model="form.episode_no" :min="1" :max="999" />
        </el-form-item>

        <el-form-item label="标题" required>
          <el-input v-model="form.title" maxlength="256" placeholder="视频标题" />
        </el-form-item>

        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" placeholder="视频描述" />
        </el-form-item>

        <el-form-item label="标签">
          <el-select
            v-model="form.tags"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="输入后回车添加，如 romance, werewolf"
            style="width: 100%"
          />
        </el-form-item>

        <el-form-item label="目标区域" required>
          <el-select v-model="form.target_region" style="width: 200px">
            <el-option v-for="r in metaOptions.regions" :key="r.value" :label="r.label" :value="r.value" />
          </el-select>
          <div class="form-tip">审核规则按目标市场加载（欧美 / 中东 / 东南亚标准不同）</div>
        </el-form-item>

        <el-form-item :label="form.content_type === 'poster' ? '海报文件' : '视频文件'" required>
          <div class="file-picker">
            <el-button :icon="UploadFilled" @click="triggerFilePicker">
              点击选择{{ form.content_type === 'poster' ? '图片' : '视频' }}文件
            </el-button>
            <input
              ref="fileInputRef"
              type="file"
              :accept="acceptExtensions"
              style="display: none"
              @change="handleFileChange"
            />
            <span v-if="selectedFile" class="file-name">{{ selectedFile.name }}</span>
            <el-button v-if="selectedFile" text type="danger" size="small" @click="clearFile">移除</el-button>
          </div>
          <div class="form-tip">{{ extensionTip }}</div>
        </el-form-item>

        <el-form-item>
          <el-button type="primary" :loading="submitting" :disabled="!canSubmit" @click="handleSubmit">
            提交审核
          </el-button>
          <span v-if="lastSubmissionId" class="form-tip" style="margin-left: 12px">
            已提交：{{ lastSubmissionId }}，AI 审核中，可在下方列表查看进度
          </span>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 新建剧集对话框 -->
    <el-dialog v-model="showCreateSeries" title="新建剧集" width="480px">
      <el-form label-width="90px">
        <el-form-item label="剧名" required>
          <el-input v-model="newSeries.title" maxlength="256" />
        </el-form-item>
        <el-form-item label="简介">
          <el-input v-model="newSeries.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="题材">
          <el-select v-model="newSeries.genre" placeholder="选择题材" clearable style="width: 100%">
            <el-option v-for="g in GENRES" :key="g" :label="g" :value="g" />
          </el-select>
        </el-form-item>
        <el-form-item label="目标区域">
          <el-select v-model="newSeries.target_region" style="width: 100%">
            <el-option v-for="r in metaOptions.regions" :key="r.value" :label="r.label" :value="r.value" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateSeries = false">取消</el-button>
        <el-button type="primary" :loading="creatingSeries" @click="handleCreateSeries">创建</el-button>
      </template>
    </el-dialog>

    <!-- 我的提交记录 -->
    <el-card style="margin-top: 16px">
      <template #header>
        <span>我的提交</span>
        <el-button style="float: right" :icon="Refresh" circle size="small" @click="fetchSubmissions" />
      </template>
      <el-empty v-if="!submissions.length" description="暂无提交记录" />
      <el-table v-else :data="submissions" size="small">
        <el-table-column label="标题" min-width="180">
          <template #default="{ row }">
            <el-link type="primary" :underline="false" @click="goDetail(row.submission_id)">
              {{ row.video_title }}
            </el-link>
            <div v-if="row.series_title" class="sub-text">{{ row.series_title }} · 第 {{ row.episode_no }} 集</div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="100">
          <template #default="{ row }: { row: SubmissionItem }">
            <el-tag size="small" :type="row.content_type === 'clip' ? 'warning' : row.content_type === 'poster' ? 'success' : 'primary'">
              {{ contentTypeLabel[row.content_type] }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="target_region" label="区域" width="70" />
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="statusMeta(row.status).type">{{ statusMeta(row.status).label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="安全分" width="90">
          <template #default="{ row }">
            <span v-if="row.weighted_score !== null">{{ row.weighted_score.toFixed(2) }}</span>
            <el-tag v-else type="info" size="small">—</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="submitted_at" label="提交时间" width="170" />
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button
              v-if="row.status === 'published_rejected' || row.status === 'auto_rejected'"
              text type="warning" size="small"
              @click="openAppeal(row)"
            >申诉</el-button>
            <el-button
              v-if="row.status === 'ai_processing' || row.status === 'pending_review'"
              text type="primary" size="small"
              @click="fetchSubmissions"
            >刷新状态</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 申诉对话框 -->
    <el-dialog v-model="showAppeal" title="发起申诉" width="480px">
      <p v-if="appealTarget" class="sub-text">
        对「{{ appealTarget.video_title }}」（{{ statusMeta(appealTarget.status).label }}）发起申诉
      </p>
      <el-input
        v-model="appealReason"
        type="textarea"
        :rows="4"
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
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { UploadFilled, Refresh } from '@element-plus/icons-vue'
import {
  videoApi,
  contentTypeLabel,
  statusMeta,
  type MetaOptions,
  type SeriesItem,
  type SubmissionItem,
} from '@/api/video'

const router = useRouter()

const GENRES = ['romance', 'werewolf', 'billionaire', 'mafia', 'fantasy', 'revenge', 'comedy', 'suspense']

const metaOptions = ref<MetaOptions>({
  content_types: [],
  regions: [{ value: 'US', label: 'US' }],
  poster_extensions: [],
  video_extensions: [],
})
const seriesList = ref<SeriesItem[]>([])
const submissions = ref<SubmissionItem[]>([])
const selectedFile = ref<File | null>(null)
const fileInputRef = ref<HTMLInputElement | null>(null)
const submitting = ref(false)
const lastSubmissionId = ref('')
const showCreateSeries = ref(false)
const creatingSeries = ref(false)
const showAppeal = ref(false)
const appealing = ref(false)
const appealReason = ref('')
const appealTarget = ref<SubmissionItem | null>(null)

const form = reactive({
  content_type: 'episode' as 'episode' | 'clip' | 'poster',
  series_id: '',
  episode_no: 1,
  title: '',
  description: '',
  tags: [] as string[],
  target_region: 'US',
})

const newSeries = reactive({
  title: '',
  description: '',
  genre: '',
  target_region: 'US',
})

const contentTypeTip = computed(() => ({
  episode: '正片短剧：完整一集，全维度审核',
  clip: '投流切片：投放 TikTok/Meta 的广告素材，标准最严、抽帧更密',
  poster: '封面海报：静态图片，仅视觉轨 + 元数据审核',
}[form.content_type]))

const acceptExtensions = computed(() =>
  form.content_type === 'poster'
    ? metaOptions.value.poster_extensions.join(',')
    : metaOptions.value.video_extensions.join(','),
)

const extensionTip = computed(() =>
  form.content_type === 'poster'
    ? `海报格式：${metaOptions.value.poster_extensions.join(' / ')}`
    : `视频格式：${metaOptions.value.video_extensions.join(' / ')}，最大时长 10 分钟`,
)

const canSubmit = computed(() => {
  if (!form.title.trim() || !selectedFile.value) return false
  if (form.content_type === 'episode' && (!form.series_id || form.episode_no < 1)) return false
  return true
})

// 轮询进行中的提交
let pollTimer: ReturnType<typeof setInterval> | null = null

function triggerFilePicker() {
  fileInputRef.value?.click()
}

function handleFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  selectedFile.value = input.files?.[0] ?? null
}

function clearFile() {
  selectedFile.value = null
  if (fileInputRef.value) fileInputRef.value.value = ''
}

async function fetchSeries() {
  try {
    seriesList.value = (await videoApi.listSeries()).items
  } catch { /* 列表失败不阻塞表单 */ }
}

async function fetchSubmissions() {
  try {
    submissions.value = (await videoApi.mySubmissions()).items
    // 有进行中的任务则启动轮询
    const processing = submissions.value.some(s =>
      s.status === 'ai_processing' || s.status === 'pending_review')
    if (processing && !pollTimer) {
      pollTimer = setInterval(fetchSubmissions, 5000)
    } else if (!processing && pollTimer) {
      clearInterval(pollTimer)
      pollTimer = null
    }
  } catch { /* 忽略轮询失败 */ }
}

async function handleCreateSeries() {
  if (!newSeries.title.trim()) {
    ElMessage.warning('请填写剧名')
    return
  }
  creatingSeries.value = true
  try {
    const { series_id } = await videoApi.createSeries({ ...newSeries })
    ElMessage.success('剧集已创建')
    showCreateSeries.value = false
    Object.assign(newSeries, { title: '', description: '', genre: '' })
    await fetchSeries()
    form.series_id = series_id
  } finally {
    creatingSeries.value = false
  }
}

async function handleSubmit() {
  if (!selectedFile.value) return
  submitting.value = true
  try {
    const fd = new FormData()
    fd.append('title', form.title.trim())
    fd.append('description', form.description)
    fd.append('tags', JSON.stringify(form.tags))
    fd.append('target_region', form.target_region)
    fd.append('content_type', form.content_type)
    fd.append('series_id', form.series_id)
    fd.append('episode_no', String(form.episode_no))
    fd.append('file', selectedFile.value)

    const res = await videoApi.submit(fd)
    lastSubmissionId.value = res.submission_id
    ElMessage.success('已提交，AI 审核中')
    Object.assign(form, { title: '', description: '', tags: [] })
    clearFile()
    await fetchSubmissions()
  } finally {
    submitting.value = false
  }
}

function openAppeal(row: SubmissionItem) {
  appealTarget.value = row
  appealReason.value = ''
  showAppeal.value = true
}

function goDetail(submissionId: string) {
  router.push(`/video/submissions/${submissionId}`)
}

async function handleAppeal() {
  if (!appealTarget.value) return
  appealing.value = true
  try {
    await videoApi.createAppeal(appealTarget.value.submission_id, appealReason.value.trim())
    ElMessage.success('申诉已提交，审核员将在 1-2 个工作日内处理')
    showAppeal.value = false
  } finally {
    appealing.value = false
  }
}

onMounted(async () => {
  try {
    metaOptions.value = await videoApi.metaOptions()
  } catch { /* 选项加载失败用默认值 */ }
  await Promise.all([fetchSeries(), fetchSubmissions()])
})

onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer)
})
</script>

<style scoped>
.file-picker {
  display: flex;
  align-items: center;
  gap: 12px;
}
.file-name {
  font-size: 13px;
  color: var(--el-text-color-regular);
}
.form-tip {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.5;
  margin-top: 4px;
  width: 100%;
}
.sub-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
