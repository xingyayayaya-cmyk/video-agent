// 出海 AI 短视频审核平台 — API 客户端
// 对应后端 backend/api/v1/video.py 的 13 个路由
import client from './client'

// ── 类型定义 ──────────────────────────────────────────────

export type ContentType = 'episode' | 'clip' | 'poster'
export type SubmissionStatus =
  | 'ai_processing'
  | 'auto_approved'
  | 'auto_rejected'
  | 'pending_review'
  | 'published_approved'
  | 'published_rejected'
  | 'failed'

export interface SeriesItem {
  series_id: string
  title: string
  description: string
  genre: string
  target_region: string
  status: string
  episode_count: number
  created_at: string | null
}

export interface MetaOptions {
  content_types: { value: ContentType; label: string }[]
  regions: { value: string; label: string }[]
  poster_extensions: string[]
  video_extensions: string[]
}

export interface SubmissionItem {
  submission_id: string
  video_id: string
  video_title: string
  series_title: string
  content_type: ContentType
  episode_no: number | null
  target_region: string
  status: SubmissionStatus
  final_decision: string
  weighted_score: number | null
  submitted_at: string | null
}

export interface RiskDimension {
  dimension: string
  risk_score: number
  severity: string
  confidence: number
  violations_found: number
  details: string
  suggestions: string
}

export interface PendingItem {
  submission_id: string
  video_id: string
  creator_name: string
  video_title: string
  series_title: string
  status: string
  content_type: ContentType
  episode_no: number | null
  target_region: string
  submitted_at: string | null
  pre_review: {
    weighted_score: number
    overall_risk: string
    decision: string
  }
  risk_dimensions: RiskDimension[]
}

export interface ReviewDetail {
  submission_id: string
  creator_id: string
  content_type: ContentType
  video_meta: Record<string, unknown>
  frames_count: number
  visual_results: {
    total_frames_analyzed?: number
    violations?: FrameViolation[]
    overall_risk?: string
    overall_confidence?: number
    summary?: string
  }
  audio_results: {
    transcript?: string
    language_detected?: string
    violations?: AudioViolation[]
    overall_risk?: string
    overall_confidence?: number
    summary?: string
  }
  metadata_results: {
    title?: string
    violations?: AudioViolation[]
    overall_risk?: string
    summary?: string
  }
  pre_review_assessment: {
    weighted_score?: number
    overall_risk?: string
    decision?: string
    region_flags?: string[]
  }
  risk_dimensions: RiskDimension[]
  risk_summary: string
  target_region: string
  review_context: string
}

export interface FrameViolation {
  frame_index: number
  timestamp: number
  category: string
  severity: string
  confidence: number
  description: string
  detected_elements: string[]
}

export interface AudioViolation {
  category: string
  severity: string
  confidence: number
  text_segment: string
  description: string
}

export interface SubmissionResult {
  submission_id: string
  status: SubmissionStatus | string
  final_decision?: string
  weighted_score?: number
  overall_risk?: string
  content_type?: ContentType
  risk_dimensions?: RiskDimension[]
  risk_summary?: string
  target_region?: string
  video_meta?: {
    title?: string
    description?: string
    tags?: string[]
  }
}

export interface DashboardStats {
  total: number
  auto_approved: number
  auto_rejected: number
  pending_review: number
  published: number
  failed: number
  rates: {
    auto_approve_rate: number
    auto_reject_rate: number
    manual_review_rate: number
  }
  avg_weighted_score: number | null
  avg_review_seconds: number | null
  by_content_type: Record<string, number>
  appeals: { total: number; overturned: number }
}

export interface AppealItem {
  appeal_id: string
  submission_id: string
  creator_name: string
  video_title: string
  content_type: ContentType
  reason: string
  status: 'open' | 'under_review' | 'upheld' | 'overturned'
  final_decision: string
  weighted_score: number | null
  resolution_note: string
  created_at: string | null
}

// ── API 方法 ──────────────────────────────────────────────

export const videoApi = {
  /** 表单选项（内容类型 / 区域 / 支持的扩展名） */
  metaOptions: () => client.get<MetaOptions>('/video/meta-options').then(r => r.data),

  /** 我的剧集列表 */
  listSeries: () => client.get<{ items: SeriesItem[] }>('/video/series').then(r => r.data),

  /** 创建剧集 */
  createSeries: (data: { title: string; description?: string; genre?: string; target_region?: string }) =>
    client.post<{ series_id: string }>('/video/series', data).then(r => r.data),

  /** 上传视频/海报（multipart） */
  submit: (form: FormData) =>
    client.post<{ submission_id: string; status: string }>('/video/submit', form, {
      timeout: 120_000,
    }).then(r => r.data),

  /** 我的提交列表 */
  mySubmissions: () =>
    client.get<{ items: SubmissionItem[] }>('/video/my-submissions').then(r => r.data),

  /** 单份提交状态/结果（轮询） */
  mySubmission: (submissionId: string) =>
    client.get<SubmissionResult>(`/video/my-submissions/${submissionId}`).then(r => r.data),

  /** 审核员：提交列表（默认待审；看板下钻传 status=auto_approved/auto_rejected/all 等） */
  pendingReviews: (status = 'pending_review') =>
    client.get<{ items: PendingItem[]; total: number }>('/video/pending-reviews', {
      params: { status },
    }).then(r => r.data),

  /** 审核员：AI 预审核详情 */
  reviewDetail: (submissionId: string) =>
    client.get<ReviewDetail>(`/video/submissions/${submissionId}/review`).then(r => r.data),

  /** 审核员：确认审核（approve / modify / reject） */
  confirm: (submissionId: string, data: {
    action: 'approve' | 'modify' | 'reject'
    modifications?: { dimension: string; risk_score: number; severity: string }[]
    review_notes?: string
  }) =>
    client.post<SubmissionResult>(`/video/submissions/${submissionId}/confirm`, data).then(r => r.data),

  /** 审核员：看板指标 */
  dashboardStats: () =>
    client.get<DashboardStats>('/video/dashboard/stats').then(r => r.data),

  /** 创作者：发起申诉 */
  createAppeal: (submissionId: string, reason: string) =>
    client.post<{ appeal_id: string; status: string }>(
      `/video/submissions/${submissionId}/appeal`, { reason },
    ).then(r => r.data),

  /** 审核员：申诉队列 */
  appeals: (statusFilter = 'open') =>
    client.get<{ items: AppealItem[]; total: number }>('/video/appeals', {
      params: { status_filter: statusFilter },
    }).then(r => r.data),

  /** 审核员：处理申诉 */
  resolveAppeal: (appealId: string, resolution: 'upheld' | 'overturned', note = '') =>
    client.post<{ appeal_id: string; status: string }>(
      `/video/appeals/${appealId}/resolve`, { resolution, note },
    ).then(r => r.data),
}

/**
 * 原视频/海报回放地址。
 * <video>/<img> 标签无法携带 Authorization 头，token 只能放 query param
 * （后端 /media 接口专门校验 query token；走 vite 代理，同源相对路径即可）。
 */
export function mediaUrl(submissionId: string): string {
  const token = localStorage.getItem('edu-agent-token') ?? ''
  return `/api/v1/video/submissions/${submissionId}/media?token=${encodeURIComponent(token)}`
}

/** 状态 → Element Plus 标签类型/文案 */
export function statusMeta(status: string): { type: 'success' | 'warning' | 'danger' | 'info' | 'primary'; label: string } {
  const map: Record<string, { type: 'success' | 'warning' | 'danger' | 'info' | 'primary'; label: string }> = {
    ai_processing: { type: 'primary', label: 'AI 审核中' },
    auto_approved: { type: 'success', label: '自动通过' },
    auto_rejected: { type: 'danger', label: '自动拒绝' },
    pending_review: { type: 'warning', label: '待人工复核' },
    published_approved: { type: 'success', label: '已通过' },
    published_rejected: { type: 'danger', label: '已拒绝' },
    failed: { type: 'info', label: '审核失败' },
  }
  return map[status] ?? { type: 'info', label: status }
}

/** 内容类型 → 中文标签 */
export const contentTypeLabel: Record<ContentType, string> = {
  episode: '正片',
  clip: '投流切片',
  poster: '封面海报',
}

/** 风险等级 → Element Plus 标签类型 */
export function riskTagType(risk: string | undefined): 'success' | 'warning' | 'danger' | 'info' {
  const map: Record<string, 'success' | 'warning' | 'danger' | 'info'> = {
    high: 'danger',
    medium: 'warning',
    low: 'success',
    none: 'info',
  }
  return map[risk ?? ''] ?? 'info'
}
