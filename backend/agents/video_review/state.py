# backend/agents/video_review/state.py
# =============================================================================
# 视频审核 Agent 完整 State
#
# 基于 ExamState 模式改造，核心变化：
#   - exam_id/submission_id/word_file_path → video_id/submission_id/video_file_path
#   - parsed_questions → video_meta + frames + audio_transcript
#   - objective/subjective/code 三轨 → visual/audio/metadata 三轨
#   - weak_points → risk_dimensions（六大风险维度评分）
#   - teacher_decision → reviewer_decision（审核员决策）
# =============================================================================

from typing import Annotated, Optional
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field


# ──────────────────────────────────────────────────────────────────────────────
# 结构化输出 Schema
# ──────────────────────────────────────────────────────────────────────────────

class FrameViolation(BaseModel):
    """单帧违规检测结果（Vision LLM 输出）"""
    frame_index:       int = Field(description="帧序号（从0开始）")
    timestamp:         float = Field(description="帧对应的时间戳（秒）")
    category:          str = Field(description="违规类别：violence/nudity/copyright/ad_fraud/platform_guideline/cultural/safe")
    severity:          str = Field(description="严重程度：high/medium/low/none")
    confidence:        float = Field(description="置信度 0.0-1.0")
    description:       str = Field(description="违规描述（英文）")
    detected_elements: list[str] = Field(default_factory=list, description="检测到的具体违规元素")


class VisualTrackResult(BaseModel):
    """视觉轨审核结果"""
    total_frames_analyzed: int = Field(description="总分析帧数")
    violations:            list[FrameViolation] = Field(default_factory=list, description="检测到的违规列表")
    overall_risk:          str = Field(description="视觉轨整体风险：high/medium/low/none")
    overall_confidence:    float = Field(description="整体置信度 0.0-1.0")
    summary:               str = Field(description="视觉轨审核摘要（英文）")


class AudioViolation(BaseModel):
    """音频/文本违规检测结果"""
    category:    str = Field(description="违规类别")
    severity:    str = Field(description="严重程度：high/medium/low/none")
    confidence:  float = Field(description="置信度")
    text_segment: str = Field(description="触发的文本片段")
    description: str = Field(description="违规描述")


class AudioTrackResult(BaseModel):
    """音频轨审核结果"""
    transcript:        str = Field(default="", description="完整转录文本")
    language_detected: str = Field(default="en", description="检测到的语言")
    violations:        list[AudioViolation] = Field(default_factory=list, description="音频违规列表")
    overall_risk:      str = Field(description="音频轨整体风险")
    overall_confidence: float = Field(description="整体置信度")
    summary:           str = Field(description="音频轨审核摘要")


class MetadataTrackResult(BaseModel):
    """元数据轨审核结果"""
    title:             str = Field(default="", description="视频标题")
    description:       str = Field(default="", description="视频描述")
    tags:              list[str] = Field(default_factory=list, description="视频标签")
    violations:        list[AudioViolation] = Field(default_factory=list, description="元数据违规列表")
    overall_risk:      str = Field(description="元数据轨整体风险")
    overall_confidence: float = Field(description="整体置信度")
    summary:           str = Field(description="元数据轨审核摘要")


class RiskDimension(BaseModel):
    """单个风险维度评分"""
    dimension:      str = Field(description="维度名：legal_compliance/adult_content/copyright_ip/ad_fraud/platform_guideline/cultural_regional")
    risk_score:     float = Field(description="风险分 0.0-1.0（1.0=安全, 0.0=高风险）")
    severity:       str = Field(description="严重程度：high/medium/low/none")
    confidence:     float = Field(description="置信度")
    violations_found: int = Field(description="发现的违规数")
    details:        str = Field(description="详细说明（英文）")
    suggestions:    str = Field(description="修改建议（英文）")


class RiskAssessmentReport(BaseModel):
    """综合风险评估报告"""
    dimensions:        list[RiskDimension] = Field(description="六维度评分列表")
    weighted_score:   float = Field(description="加权总分 0.0-1.0（1.0=完全安全）")
    overall_risk:      str = Field(description="整体风险等级：high/medium/low/none")
    overall_confidence: float = Field(description="整体置信度")
    decision:          str = Field(description="自动决策：auto_approved/auto_rejected/pending_review")
    summary:           str = Field(description="综合审核摘要（英文，200字以内）")
    region_flags:      list[str] = Field(default_factory=list, description="触发的区域合规标记")


class ReviewerDecision(BaseModel):
    """审核员决策（interrupt 恢复时传入）"""
    action:        str = Field(description="approve / modify / reject")
    modifications: list[dict] = Field(default_factory=list, description="修改项")
    reviewer_id:   str = Field(description="审核员 ID")
    review_notes:  str = Field(default="", description="审核备注")
    region_override: Optional[str] = Field(default=None, description="区域合规覆盖")


# ──────────────────────────────────────────────────────────────────────────────
# State TypedDict
# ──────────────────────────────────────────────────────────────────────────────

class VideoReviewState(TypedDict):
    """视频审核 Agent 完整 State

    映射关系（原 ExamState → 新 VideoReviewState）：
        word_file_path    → video_file_path
        exam_id            → video_id
        parsed_questions   → video_meta + frames + audio_transcript
        objective/subjective/code → visual/audio/metadata
        pre_review_summary → pre_review_assessment
        weak_points        → risk_dimensions
        teacher_decision   → reviewer_decision
        final_results      → final_results
        published          → published
    """

    # ── 请求上下文 ──────────────────────────────────────────────
    messages:        Annotated[list[BaseMessage], add_messages]
    creator_id:      str         # 创作者/上传者 ID
    tenant_id:       str
    session_id:      str
    video_id:        str         # 视频 ID（videos 表）
    submission_id:   str         # 提交记录 ID（video_submissions 表）
    video_file_path: str         # 视频文件本地临时路径（poster 类型时为图片路径）
    target_region:   str         # 目标审核区域（US/EU/LATAM/MENA/SEA）
    content_type:    str         # 内容类型：episode 正片 / clip 投流切片 / poster 封面海报

    # ── 视频元信息 ──────────────────────────────────────────────
    video_meta:      dict        # ffprobe 获取的 {duration, width, height, fps, codec, has_audio}
    frames:          list[dict]  # 抽帧结果 [{frame_path, timestamp}, ...]
    audio_path:      Optional[str]  # 音频分离后 wav 路径（None 表示无音轨）
    audio_transcript: str        # ASR 转录文本

    # ── 三轨审核结果 ─────────────────────────────────────────────
    visual_results:    dict      # VisualTrackResult
    audio_results:     dict      # AudioTrackResult
    metadata_results:  dict      # MetadataTrackResult

    # ── 汇总预审核结果 ──────────────────────────────────────────
    pre_review_assessment: dict  # RiskAssessmentReport dict

    # ── 风险维度分析 ─────────────────────────────────────────────
    risk_dimensions:    list[dict]
    risk_summary:       str

    # ── Human-in-the-Loop ───────────────────────────────────────
    reviewer_notified:  bool
    reviewer_decision:  Optional[dict]

    # ── 最终结果 ────────────────────────────────────────────────
    final_results:     list[dict]
    final_decision:    str        # approved / rejected / pending_review
    published:         bool

    # ── 降级标记 ────────────────────────────────────────────────
    fallback_used:     bool
    structured_output: Optional[dict]
