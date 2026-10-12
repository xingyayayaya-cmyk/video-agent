# backend/agents/video_review/nodes.py
# =============================================================================
# 视频审核 Agent 的全部 LangGraph 节点实现
#
# 整体流水线（与 graph.py 一一对应）：
#     1. extract_video_assets   —— ffmpeg 抽帧 + 音频分离 + ASR 转文字
#     2. load_video_meta        —— 从 DB 加载视频元数据（标题/描述/标签）
#     3. run_three_tracks       —— 三轨并行审核（视觉 / 音频 / 元数据）
#     4. aggregate_assessment   —— 汇总三轨，计算风险评分与自动决策
#     5. analyze_risk           —— 六维度风险分析与区域合规标记
#     6. notify_reviewer        —— 提交状态改为 pending_review（中风险）
#     7. reviewer_review        —— HitL 暂停，等待审核员确认
#     8. apply_reviewer_decision —— 合并审核员修改到最终结论
#     9. publish_results        —— 写入 DB 并发布审核结果
#
# 设计要点：
#   - 视觉轨走 GPT-4o Vision（多模态）；文本轨走 GPT-4o；区域轨走 DeepSeek
#   - 低风险自动通过、高风险自动拒绝、中风险进人工复核
#   - 任一步失败尽量降级，不阻断审核流水线
# =============================================================================

import asyncio
import base64
import copy
import json
import os
import uuid

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import interrupt
from sqlalchemy import text

from backend.agents.video_review.state import (
    VideoReviewState,
    VisualTrackResult,
    AudioTrackResult,
    MetadataTrackResult,
    RiskAssessmentReport,
    RiskDimension,
)
from backend.agents.video_review.prompts import (
    SYSTEM_PROMPT,
    VISUAL_PROMPT,
    AUDIO_PROMPT,
    METADATA_PROMPT,
    AGGREGATE_PROMPT,
    get_region_guideline,
)
from backend.core.llm_factory import get_llm, get_structured_llm
from backend.core.logger import get_logger
from backend.core.video_processor import (
    extract_frames,
    extract_keyframes,
    extract_audio,
    transcribe_audio,
    get_video_info,
    cleanup_frames,
    cleanup_audio,
    cleanup_frame_path,
    is_poster_image,
    prepare_image_frames,
)
from backend.core.rulepacks import build_review_context
from backend.dependencies import AsyncSessionLocal
from backend.config import get_settings

logger = get_logger(__name__)
settings = get_settings()


# ──────────────────────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────────────────────

def _get_message_content(msg) -> str:
    """统一提取 LLM 消息文本内容。"""
    if hasattr(msg, "text") and not callable(getattr(msg, "text", None)):
        return msg.text
    if isinstance(msg.content, str):
        return msg.content
    return str(msg.content)


def _encode_image_b64(image_path: str) -> str:
    """将图片文件编码为 base64 字符串。"""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _parse_json_response(text: str) -> dict:
    """清洗并解析 LLM 返回的 JSON。

    兼容三种常见污染：
      1. ```json 围栏包裹
      2. JSON 前后夹带解释性文字（Kimi K2.6 常见：正文 + 结尾补充说明）
      3. 开头是说明文字、JSON 埋在中间
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # 截取第一个 {...} 平衡块（raw_decode 会停在 JSON 结束处，忽略尾部杂讯）
        start = cleaned.find("{")
        if start == -1:
            raise
        obj, _ = json.JSONDecoder().raw_decode(cleaned[start:])
        return obj


# ──────────────────────────────────────────────────────────────────────────────
# 节点1：extract_video_assets — 抽帧 + 音频分离 + ASR
# ──────────────────────────────────────────────────────────────────────────────

async def extract_video_assets_node(state: VideoReviewState) -> dict:
    """【节点1】使用 ffmpeg 从视频中抽帧、分离音频、ASR 转文字。

    按内容类型分支（对齐需求文档 §1）：
        episode 正片：场景切换 + 保底间隔混合抽帧
        clip 投流切片：抽帧密度翻倍（2s 间隔），广告素材标准更严
        poster 封面海报：单帧图片路径，跳过音频轨

    失败降级：
        - ffprobe 失败 → 空元信息，继续后续步骤
        - 抽帧失败 → 空帧列表，视觉轨将跳过
        - 音频分离失败 → audio_path=None，音频轨跳过
        - ASR 失败 → 空转录文本，音频轨仅做元数据级别审核
    """
    video_path = state["video_file_path"]
    target_region = state.get("target_region", settings.review_default_region)
    content_type = state.get("content_type", "episode")

    # ── 1. 获取视频元信息 ──
    video_meta = await get_video_info(video_path)
    video_meta["content_type"] = content_type  # 供下游 prompt / 日志使用
    logger.info(
        "extract_assets.video_info",
        duration=video_meta.get("duration", 0),
        resolution=f"{video_meta.get('width', 0)}x{video_meta.get('height', 0)}",
        has_audio=video_meta.get("has_audio", False),
        content_type=content_type,
    )

    # 时长校验
    max_duration = settings.video_max_duration
    if video_meta.get("duration", 0) > max_duration:
        logger.warning(
            "extract_assets.duration_exceeds_limit",
            duration=video_meta["duration"],
            max=max_duration,
        )

    # ── 2. 抽帧（按内容类型分支）──
    if content_type == "poster" or is_poster_image(video_path):
        frames = await prepare_image_frames(video_path)
    else:
        # clip 用更密间隔；scene_threshold>0 时启用场景切换检测防闪帧规避
        interval = (
            settings.video_clip_interval
            if content_type == "clip"
            else settings.video_frame_interval
        )
        frames = await extract_keyframes(
            video_path,
            interval=interval,
            max_frames=settings.video_max_frames,
            scene_threshold=settings.video_scene_threshold,
        )
        if not frames:
            # 降级：场景检测整体失败时退回固定间隔抽帧
            frames = await extract_frames(
                video_path, interval=interval, max_frames=settings.video_max_frames,
            )

    # ── 3. 分离音频（poster 跳过）──
    audio_path = None
    if content_type != "poster" and video_meta.get("has_audio", False):
        audio_path = await extract_audio(video_path)

    # ── 4. ASR 转文字 ──
    audio_transcript = ""
    if audio_path:
        audio_transcript = await transcribe_audio(audio_path)
        # 转录完成后立即清理音频临时文件（帧文件在视觉轨用完后清理）
        cleanup_audio(audio_path)
        audio_path = None  # 已清理，不再保留路径

    logger.info(
        "extract_assets.done",
        frames=len(frames),
        transcript_length=len(audio_transcript),
        region=target_region,
        content_type=content_type,
    )

    return {
        "video_meta": video_meta,
        "frames": frames,
        "audio_path": None,  # 音频已转文字，不再保留路径
        "audio_transcript": audio_transcript,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 节点2：load_video_meta — 从 DB 加载视频元数据
# ──────────────────────────────────────────────────────────────────────────────

async def load_video_meta_node(state: VideoReviewState) -> dict:
    """【节点2】从数据库加载视频的标题、描述、标签等元数据。

    这些元数据来自创作者上传时填写的信息，
    供元数据轨（第三轨）审核使用。
    """
    video_id = state["video_id"]

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, title, description, tags, creator_id,
                       duration, file_path, file_size
                FROM videos
                WHERE id = :video_id
            """),
            {"video_id": video_id},
        )
        row = result.mappings().fetchone()

    if not row:
        logger.warning("load_video_meta.not_found", video_id=video_id)
        return {"video_meta": {**state.get("video_meta", {}), "title": "", "description": "", "tags": []}}

    # 合并 DB 元数据到 video_meta
    tags_str = row.get("tags") or "[]"
    try:
        tags = json.loads(tags_str) if isinstance(tags_str, str) else tags_str
        if not isinstance(tags, list):
            tags = [str(tags)]
    except (json.JSONDecodeError, TypeError):
        tags = []

    existing_meta = state.get("video_meta", {})
    updated_meta = {
        **existing_meta,
        "title": row.get("title", "") or "",
        "description": row.get("description", "") or "",
        "tags": tags,
        "creator_id": str(row.get("creator_id", "")),
        "file_size": row.get("file_size", 0),
    }

    logger.info(
        "load_video_meta.done",
        video_id=video_id,
        title=updated_meta["title"][:50],
        tags_count=len(tags),
    )

    return {"video_meta": updated_meta}


# ──────────────────────────────────────────────────────────────────────────────
# 第一轨：视觉审核（GPT-4o Vision）
# ──────────────────────────────────────────────────────────────────────────────

async def _run_visual_track(
    frames: list[dict],
    video_meta: dict,
    target_region: str,
    extra_context: str = "",
) -> dict:
    """视觉轨：使用 GPT-4o Vision 逐帧审核。

    并行策略：
        - 帧数 <= 5：全部并行
        - 帧数 > 5：分批，每批 5 帧并行，避免 API 限速

    返回 VisualTrackResult 的 dict 形式。
    """
    if not frames:
        return {
            "total_frames_analyzed": 0,
            "violations": [],
            "overall_risk": "none",
            "overall_confidence": 1.0,
            "summary": "No frames extracted. Visual track skipped.",
        }

    duration = video_meta.get("duration", 0)
    width = video_meta.get("width", 0)
    height = video_meta.get("height", 0)

    prompt_text = VISUAL_PROMPT.format(
        target_region=target_region,
        duration=duration,
        width=width,
        height=height,
        extra_context=extra_context,
    )

    BATCH_SIZE = 5
    all_violations = []
    total_analyzed = 0

    # 分批处理
    batches = [frames[i:i + BATCH_SIZE] for i in range(0, len(frames), BATCH_SIZE)]

    for batch_idx, batch in enumerate(batches):
        # 构建多模态消息（文本 prompt + 每帧图片）
        content_parts = [{"type": "text", "text": prompt_text}]

        for j, frame in enumerate(batch):
            frame_idx = batch_idx * BATCH_SIZE + j
            b64_img = _encode_image_b64(frame["frame_path"])
            content_parts.append({
                "type": "text",
                "text": f"--- Frame {frame_idx} (t={frame['timestamp']}s) ---",
            })
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"},
            })

        try:
            llm = get_llm("video_visual", temperature=0)
            response = await llm.ainvoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=content_parts),
            ])
            raw = _get_message_content(response)
            result = _parse_json_response(raw)
            violations = result.get("violations", [])
            all_violations.extend(violations)
            total_analyzed += len(batch)
        except Exception as e:
            logger.warning(
                "visual_track.batch_failed",
                batch_idx=batch_idx,
                error=str(e),
            )
            # 降级：该批帧标记为待复核
            for j, frame in enumerate(batch):
                frame_idx = batch_idx * BATCH_SIZE + j
                all_violations.append({
                    "frame_index": frame_idx,
                    "timestamp": frame["timestamp"],
                    "category": "safe",
                    "severity": "none",
                    "confidence": 0.0,
                    "description": "Frame analysis failed, flagged for manual review",
                    "detected_elements": [],
                })
            total_analyzed += len(batch)

    # 汇总视觉轨结果
    has_high = any(v.get("severity") == "high" for v in all_violations)
    has_medium = any(v.get("severity") == "medium" for v in all_violations)
    has_low = any(v.get("severity") == "low" for v in all_violations)

    if has_high:
        overall_risk = "high"
    elif has_medium:
        overall_risk = "medium"
    elif has_low:
        overall_risk = "low"
    else:
        overall_risk = "none"

    confidences = [v.get("confidence", 0) for v in all_violations if v.get("confidence") is not None]
    overall_confidence = sum(confidences) / len(confidences) if confidences else 0.5

    return {
        "total_frames_analyzed": total_analyzed,
        "violations": all_violations,
        "overall_risk": overall_risk,
        "overall_confidence": round(overall_confidence, 2),
        "summary": f"Analyzed {total_analyzed} frames. Overall risk: {overall_risk}. Found {len([v for v in all_violations if v.get('severity') not in ('none', 'low')])} significant violations.",
    }


# ──────────────────────────────────────────────────────────────────────────────
# 第二轨：音频审核（GPT-4o 文本审核，基于 ASR 转录文本）
# ──────────────────────────────────────────────────────────────────────────────

async def _run_audio_track(
    transcript: str,
    target_region: str,
    extra_context: str = "",
) -> dict:
    """音频轨：使用 GPT-4o 审核 ASR 转录文本。

    返回 AudioTrackResult 的 dict 形式。
    """
    if not transcript or not transcript.strip():
        return {
            "transcript": "",
            "language_detected": "unknown",
            "violations": [],
            "overall_risk": "none",
            "overall_confidence": 1.0,
            "summary": "No audio transcript available. Audio track skipped.",
        }

    prompt_text = AUDIO_PROMPT.format(
        target_region=target_region,
        transcript=transcript[:8000],  # 截断超长转录
        extra_context=extra_context,
    )

    try:
        llm = get_llm("video_text", temperature=0)
        response = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt_text),
        ])
        raw = _get_message_content(response)
        result = _parse_json_response(raw)
        return result
    except Exception as e:
        logger.warning("audio_track.failed", error=str(e))
        return {
            "transcript": transcript,
            "language_detected": "unknown",
            "violations": [{
                "category": "platform_guideline",
                "severity": "low",
                "confidence": 0.0,
                "text_segment": "",
                "description": "Audio review failed, flagged for manual review",
            }],
            "overall_risk": "low",
            "overall_confidence": 0.0,
            "summary": "Audio review failed. Manual review recommended.",
        }


# ──────────────────────────────────────────────────────────────────────────────
# 第三轨：元数据审核（GPT-4o 文本审核）
# ──────────────────────────────────────────────────────────────────────────────

async def _run_metadata_track(
    video_meta: dict,
    target_region: str,
    extra_context: str = "",
) -> dict:
    """元数据轨：使用 GPT-4o 审核标题/描述/标签。

    返回 MetadataTrackResult 的 dict 形式。
    """
    title = video_meta.get("title", "")
    description = video_meta.get("description", "")
    tags = video_meta.get("tags", [])

    prompt_text = METADATA_PROMPT.format(
        target_region=target_region,
        title=title,
        description=description,
        tags=", ".join(tags) if isinstance(tags, list) else str(tags),
        extra_context=extra_context,
    )

    try:
        llm = get_llm("video_text", temperature=0)
        response = await llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt_text),
        ])
        raw = _get_message_content(response)
        result = _parse_json_response(raw)
        return result
    except Exception as e:
        logger.warning("metadata_track.failed", error=str(e))
        return {
            "title": title,
            "description": description,
            "tags": tags,
            "violations": [{
                "category": "platform_guideline",
                "severity": "low",
                "confidence": 0.0,
                "text_segment": "",
                "description": "Metadata review failed, flagged for manual review",
            }],
            "overall_risk": "low",
            "overall_confidence": 0.0,
            "summary": "Metadata review failed. Manual review recommended.",
        }


# ──────────────────────────────────────────────────────────────────────────────
# 节点3：run_three_tracks — 三轨并行审核
# ──────────────────────────────────────────────────────────────────────────────

async def run_three_tracks_node(state: VideoReviewState) -> dict:
    """【节点3】三轨并行审核入口。

    分轨规则：
        视觉轨 → GPT-4o Vision 逐帧审核
        音频轨 → GPT-4o 文本审核（ASR 转录文本）
        元数据轨 → GPT-4o 文本审核（标题/描述/标签）

    asyncio.gather(return_exceptions=True)：
        某一轨抛异常不会中断其他轨。
    """
    frames = state.get("frames", [])
    video_meta = state.get("video_meta", {})
    audio_transcript = state.get("audio_transcript", "")
    target_region = state.get("target_region", settings.review_default_region)
    content_type = state.get("content_type", "episode")

    # 区域 × 内容类型二维规则上下文（投流切片附带广告平台规则，封面附带海报规则）
    extra_context = build_review_context(target_region, content_type)

    logger.info(
        "three_tracks.start",
        frames=len(frames),
        has_transcript=bool(audio_transcript),
        region=target_region,
        content_type=content_type,
    )

    # 三轨并行启动
    raw = await asyncio.gather(
        _run_visual_track(frames, video_meta, target_region, extra_context),
        _run_audio_track(audio_transcript, target_region, extra_context),
        _run_metadata_track(video_meta, target_region, extra_context),
        return_exceptions=True,
    )

    # 异常轨降级为空结果
    visual_result = raw[0] if not isinstance(raw[0], Exception) else {
        "total_frames_analyzed": 0, "violations": [],
        "overall_risk": "none", "overall_confidence": 0.0,
        "summary": f"Visual track failed: {raw[0]}",
    }
    audio_result = raw[1] if not isinstance(raw[1], Exception) else {
        "transcript": audio_transcript, "language_detected": "unknown",
        "violations": [], "overall_risk": "none", "overall_confidence": 0.0,
        "summary": f"Audio track failed: {raw[1]}",
    }
    metadata_result = raw[2] if not isinstance(raw[2], Exception) else {
        "title": video_meta.get("title", ""), "description": video_meta.get("description", ""),
        "tags": video_meta.get("tags", []), "violations": [],
        "overall_risk": "none", "overall_confidence": 0.0,
        "summary": f"Metadata track failed: {raw[2]}",
    }

    # 记录失败信息
    for name, exc in zip(["visual", "audio", "metadata"], raw):
        if isinstance(exc, Exception):
            logger.error("three_tracks.track_failed", track=name, error=str(exc))

    logger.info(
        "three_tracks.done",
        visual_risk=visual_result.get("overall_risk"),
        audio_risk=audio_result.get("overall_risk"),
        metadata_risk=metadata_result.get("overall_risk"),
    )

    return {
        "visual_results": visual_result,
        "audio_results": audio_result,
        "metadata_results": metadata_result,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 节点4：aggregate_assessment — 汇总三轨，计算风险评分
# ──────────────────────────────────────────────────────────────────────────────

async def aggregate_assessment_node(state: VideoReviewState) -> dict:
    """【节点4】汇总三轨结果，使用 LLM 计算综合风险评分。

    核心步骤：
        1. 取出三轨结果
        2. 拼接 Prompt，调用 DeepSeek 综合分析
        3. 解析六维度风险评分 + 加权总分 + 自动决策
        4. 自动决策逻辑：
           - weighted_score >= auto_approve_threshold → auto_approved
           - weighted_score < auto_reject_threshold → auto_rejected
           - 否则 → pending_review
    """
    target_region = state.get("target_region", settings.review_default_region)
    visual = state.get("visual_results", {})
    audio = state.get("audio_results", {})
    metadata = state.get("metadata_results", {})

    prompt_text = AGGREGATE_PROMPT.format(
        target_region=target_region,
        visual_results=json.dumps(visual, ensure_ascii=False, indent=2)[:6000],
        audio_results=json.dumps(audio, ensure_ascii=False, indent=2)[:4000],
        metadata_results=json.dumps(metadata, ensure_ascii=False, indent=2)[:3000],
        auto_approve_threshold=settings.review_auto_approve_threshold,
        auto_reject_threshold=settings.review_auto_reject_threshold,
        extra_context=build_review_context(
            target_region, state.get("content_type", "episode")
        ),
    )

    # 附带区域合规指南
    region_guide = get_region_guideline(target_region)
    prompt_text += f"\n\nRegional Compliance Reference:\n{region_guide}"

    try:
        structured_llm = get_structured_llm("video_aggregate", RiskAssessmentReport)
        report: RiskAssessmentReport = await structured_llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt_text),
        ])

        assessment = report.model_dump()

    except Exception as e:
        logger.warning("aggregate_assessment.llm_failed", error=str(e))
        # 降级：用规则引擎做简单判断
        assessment = _rule_based_aggregate(visual, audio, metadata, target_region)

    logger.info(
        "aggregate_assessment.done",
        weighted_score=assessment.get("weighted_score"),
        decision=assessment.get("decision"),
        overall_risk=assessment.get("overall_risk"),
    )

    return {"pre_review_assessment": assessment}


def _rule_based_aggregate(
    visual: dict,
    audio: dict,
    metadata: dict,
    region: str,
) -> dict:
    """降级方案：用规则引擎做简单风险评分（LLM 失败时用）。"""
    visual_risk = visual.get("overall_risk", "none")
    audio_risk = audio.get("overall_risk", "none")
    metadata_risk = metadata.get("overall_risk", "none")

    risk_map = {"high": 0.1, "medium": 0.4, "low": 0.7, "none": 1.0}
    visual_score = risk_map.get(visual_risk, 0.5)
    audio_score = risk_map.get(audio_risk, 0.5)
    metadata_score = risk_map.get(metadata_risk, 0.5)

    # 加权平均：视觉权重最高
    weighted = visual_score * 0.5 + audio_score * 0.3 + metadata_score * 0.2

    if weighted >= settings.review_auto_approve_threshold:
        decision = "auto_approved"
        overall_risk = "none"
    elif weighted < settings.review_auto_reject_threshold:
        decision = "auto_rejected"
        overall_risk = "high"
    else:
        decision = "pending_review"
        overall_risk = "medium" if weighted < 0.5 else "low"

    return {
        "dimensions": [],
        "weighted_score": round(weighted, 2),
        "overall_risk": overall_risk,
        "overall_confidence": 0.5,
        "decision": decision,
        "summary": "Rule-based fallback assessment (LLM unavailable). Manual review recommended.",
        "region_flags": [f"region:{region}"] if region != "US" else [],
    }


# ──────────────────────────────────────────────────────────────────────────────
# 节点5：analyze_risk — 六维度风险详细分析
# ──────────────────────────────────────────────────────────────────────────────

async def analyze_risk_node(state: VideoReviewState) -> dict:
    """【节点5】基于汇总结果，细化六维度风险分析。

    如果节点4的 LLM 已产出 dimensions，直接使用；
    否则用规则从三轨 violations 聚合。
    """
    assessment = state.get("pre_review_assessment", {})
    dimensions = assessment.get("dimensions", [])

    if dimensions:
        # LLM 已产出维度分析，直接使用
        risk_summary = assessment.get("summary", "")
        logger.info("analyze_risk.using_llm_dimensions", count=len(dimensions))
    else:
        # 降级：规则聚合三轨 violations 到六维度
        dimensions = _aggregate_violations_to_dimensions(
            state.get("visual_results", {}),
            state.get("audio_results", {}),
            state.get("metadata_results", {}),
        )
        risk_summary = assessment.get("summary", "Risk analysis completed via rule-based aggregation.")
        logger.info("analyze_risk.using_rule_aggregation", count=len(dimensions))

    return {
        "risk_dimensions": dimensions,
        "risk_summary": risk_summary,
    }


def _aggregate_violations_to_dimensions(
    visual: dict,
    audio: dict,
    metadata: dict,
) -> list[dict]:
    """将三轨 violations 按六维度聚合（降级方案）。"""
    dimension_map = {
        "legal_compliance": [],
        "adult_content": [],
        "copyright_ip": [],
        "ad_fraud": [],
        "platform_guideline": [],
        "cultural_regional": [],
    }

    category_to_dimension = {
        "violence": "legal_compliance",
        "nudity": "adult_content",
        "copyright": "copyright_ip",
        "ad_fraud": "ad_fraud",
        "platform_guideline": "platform_guideline",
        "cultural": "cultural_regional",
    }

    # 收集所有 violations
    all_violations = []
    for v in visual.get("violations", []):
        all_violations.append(("visual", v))
    for v in audio.get("violations", []):
        all_violations.append(("audio", v))
    for v in metadata.get("violations", []):
        all_violations.append(("metadata", v))

    # 按维度归类
    for track, v in all_violations:
        category = v.get("category", "safe")
        dimension_key = category_to_dimension.get(category, "platform_guideline")
        if dimension_key in dimension_map:
            dimension_map[dimension_key].append(v)

    # 生成维度评分
    severity_score = {"high": 0.1, "medium": 0.4, "low": 0.7, "none": 1.0}
    dimensions = []
    for dim_name, violations in dimension_map.items():
        if not violations:
            dimensions.append({
                "dimension": dim_name,
                "risk_score": 1.0,
                "severity": "none",
                "confidence": 1.0,
                "violations_found": 0,
                "details": "No violations detected in this dimension.",
                "suggestions": "No action needed.",
            })
        else:
            scores = [severity_score.get(v.get("severity", "low"), 0.7) for v in violations]
            avg_score = sum(scores) / len(scores)
            max_severity = "high" if any(v.get("severity") == "high" for v in violations) else \
                           "medium" if any(v.get("severity") == "medium" for v in violations) else "low"
            dimensions.append({
                "dimension": dim_name,
                "risk_score": round(avg_score, 2),
                "severity": max_severity,
                "confidence": 0.6,
                "violations_found": len(violations),
                "details": "; ".join(v.get("description", "")[:100] for v in violations[:3]),
                "suggestions": f"Address {len(violations)} violation(s) related to {dim_name}.",
            })

    return dimensions


# ──────────────────────────────────────────────────────────────────────────────
# 节点6：notify_reviewer — 通知审核员（状态推进）
# ──────────────────────────────────────────────────────────────────────────────

async def notify_reviewer_node(state: VideoReviewState) -> dict:
    """【节点6】更新提交状态为 pending_review，等待审核员确认。

    自动决策逻辑：
        - auto_approved → 直接跳到发布（但在图中仍走 notify → reviewer → publish）
        - auto_rejected → 直接标记 rejected 并发布
        - pending_review → 状态改为 pending_review，进入 HitL 暂停
    """
    assessment = state.get("pre_review_assessment", {})
    decision = assessment.get("decision", "pending_review")
    submission_id = state["submission_id"]

    status_map = {
        "auto_approved": "auto_approved",
        "auto_rejected": "auto_rejected",
        "pending_review": "pending_review",
    }
    new_status = status_map.get(decision, "pending_review")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("""
                    UPDATE video_submissions
                    SET status = :status, updated_at = NOW()
                    WHERE id = :submission_id
                """),
                {"status": new_status, "submission_id": submission_id},
            )

    logger.info(
        "notify_reviewer.done",
        submission_id=submission_id,
        status=new_status,
        decision=decision,
    )

    return {"reviewer_notified": True}


# ──────────────────────────────────────────────────────────────────────────────
# 节点7：reviewer_review — Human-in-the-Loop 暂停点
# ──────────────────────────────────────────────────────────────────────────────

async def reviewer_review_node(state: VideoReviewState) -> dict:
    """【节点7】Human-in-the-Loop 核心节点。

    核心步骤：
        1. 组装展示数据（三轨结果 + 风险评估 + 帧时间轴）
        2. 判断自动决策：
           - auto_approved → 跳过 interrupt，直接 approve
           - auto_rejected → 跳过 interrupt，直接 reject
           - pending_review → interrupt 暂停，等待审核员恢复
        3. 返回 reviewer_decision

    decision 约定结构：
        { "action": "approve"|"modify"|"reject", "modifications": [...], "reviewer_id": "...", "review_notes": "..." }
    """
    assessment = state.get("pre_review_assessment", {})
    decision = assessment.get("decision", "pending_review")
    submission_id = state["submission_id"]

    # 自动决策直接跳过 interrupt
    if decision == "auto_approved":
        logger.info("reviewer_review.auto_approved", submission_id=submission_id)
        return {"reviewer_decision": {
            "action": "approve",
            "modifications": [],
            "reviewer_id": "system_auto",
            "review_notes": "Auto-approved: risk score above threshold",
        }}

    if decision == "auto_rejected":
        logger.info("reviewer_review.auto_rejected", submission_id=submission_id)
        return {"reviewer_decision": {
            "action": "reject",
            "modifications": [],
            "reviewer_id": "system_auto",
            "review_notes": "Auto-rejected: risk score below threshold",
        }}

    # 中风险 → interrupt 暂停，等待审核员
    display_data = {
        "submission_id": submission_id,
        "creator_id": state.get("creator_id", ""),
        "video_meta": state.get("video_meta", {}),
        "frames_count": len(state.get("frames", [])),
        "visual_results": state.get("visual_results", {}),
        "audio_results": state.get("audio_results", {}),
        "metadata_results": state.get("metadata_results", {}),
        "pre_review_assessment": assessment,
        "risk_dimensions": state.get("risk_dimensions", []),
        "risk_summary": state.get("risk_summary", ""),
        "target_region": state.get("target_region", "US"),
        "message": "Please review the AI assessment and approve, modify, or reject.",
    }

    reviewer_decision = interrupt(display_data)

    logger.info(
        "reviewer_review.resumed",
        submission_id=submission_id,
        action=reviewer_decision.get("action", "unknown") if isinstance(reviewer_decision, dict) else "unknown",
    )

    if not isinstance(reviewer_decision, dict):
        reviewer_decision = {
            "action": "approve",
            "modifications": [],
            "reviewer_id": "",
            "review_notes": "",
        }

    return {"reviewer_decision": reviewer_decision}


# ──────────────────────────────────────────────────────────────────────────────
# 节点8：apply_reviewer_decision — 合并审核员决策
# ──────────────────────────────────────────────────────────────────────────────

async def apply_reviewer_decision_node(state: VideoReviewState) -> dict:
    """【节点8】将审核员决策合并到最终结论中。

    action = approve → 沿用 AI 评分，标记通过
    action = modify  → 覆盖特定维度评分/决策
    action = reject  → 标记拒绝
    """
    decision = state.get("reviewer_decision") or {}
    action = decision.get("action", "approve")
    modifications = decision.get("modifications", [])

    assessment = copy.deepcopy(state.get("pre_review_assessment", {}))

    # 默认：final_decision 取 assessment 的 decision
    final_decision = "approved" if action == "approve" else "rejected" if action == "reject" else "approved"

    # modify 时按维度覆盖
    if action == "modify" and modifications:
        dimensions = assessment.get("dimensions", [])
        dim_map = {d.get("dimension"): d for d in dimensions}
        for mod in modifications:
            dim_name = mod.get("dimension")
            if dim_name and dim_name in dim_map:
                if "risk_score" in mod:
                    dim_map[dim_name]["risk_score"] = mod["risk_score"]
                if "severity" in mod:
                    dim_map[dim_name]["severity"] = mod["severity"]
                if "details" in mod:
                    dim_map[dim_name]["details"] = mod["details"]

        # 重新计算加权分
        weights = {
            "legal_compliance": 2.0,
            "adult_content": 2.0,
            "copyright_ip": 1.0,
            "ad_fraud": 1.5,
            "platform_guideline": 1.0,
            "cultural_regional": 1.5,
        }
        total_weight = sum(weights.values())
        weighted_sum = sum(
            d.get("risk_score", 0.5) * weights.get(d.get("dimension", ""), 1.0)
            for d in dimensions
        )
        assessment["weighted_score"] = round(weighted_sum / total_weight, 2)
        final_decision = "approved"  # modify 后默认通过（审核员已确认）

    if action == "reject":
        assessment["decision"] = "rejected"
        final_decision = "rejected"

    # 写入审核员信息
    assessment["reviewer_id"] = decision.get("reviewer_id", "")
    assessment["review_notes"] = decision.get("review_notes", "")
    assessment["final_decision"] = final_decision

    logger.info(
        "apply_reviewer_decision.done",
        action=action,
        final_decision=final_decision,
        modifications_count=len(modifications),
    )

    return {
        "pre_review_assessment": assessment,
        "final_decision": final_decision,
    }


# ──────────────────────────────────────────────────────────────────────────────
# 节点9：publish_results — 发布审核结果
# ──────────────────────────────────────────────────────────────────────────────

async def publish_results_node(state: VideoReviewState) -> dict:
    """【节点9】将最终审核结果写入数据库并发布。

    核心步骤：
        1. 读取 State — final_decision / assessment / risk_dimensions
        2. 开启事务，逐维度写入 video_reviews（先删后插，幂等）
        3. 更新 video_submissions — status → published + 审核结果 JSON
        4. 返回 structured_output 供 API / 前端展示
    """
    submission_id = state["submission_id"]
    assessment = state.get("pre_review_assessment", {})
    final_decision = state.get("final_decision", "approved")
    risk_dimensions = state.get("risk_dimensions", [])
    risk_summary = state.get("risk_summary", "")
    reviewer_id = (state.get("reviewer_decision") or {}).get("reviewer_id", "") or None

    # ── 写入 video_reviews（逐维度）──
    async with AsyncSessionLocal() as session:
        async with session.begin():

            # 先删后插
            await session.execute(
                text("DELETE FROM video_reviews WHERE submission_id = :sid"),
                {"sid": submission_id},
            )

            for dim in risk_dimensions:
                await session.execute(
                    text("""
                        INSERT INTO video_reviews (
                            id, submission_id, dimension,
                            risk_score, severity, confidence,
                            violations_found, details, suggestions,
                            reviewer_id, reviewed_at
                        ) VALUES (
                            :id, :sid, :dim,
                            :score, :sev, :conf,
                            :vcount, :details, :sug,
                            :reviewer_id, NOW()
                        )
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "sid": submission_id,
                        "dim": dim.get("dimension", ""),
                        "score": dim.get("risk_score", 0.5),
                        "sev": dim.get("severity", "none"),
                        "conf": dim.get("confidence", 0.0),
                        "vcount": dim.get("violations_found", 0),
                        "details": dim.get("details", ""),
                        "sug": dim.get("suggestions", ""),
                        "reviewer_id": reviewer_id,
                    },
                )

            # ── 更新 video_submissions 为已发布 ──
            status = "published_approved" if final_decision == "approved" else "published_rejected"
            await session.execute(
                text("""
                    UPDATE video_submissions
                    SET status = :status,
                        final_decision = :decision,
                        weighted_score = :score,
                        overall_risk = :risk,
                        risk_dimensions = :dims,
                        risk_summary = :summary,
                        published_at = NOW(),
                        updated_at = NOW()
                    WHERE id = :sid
                """),
                {
                    "sid": submission_id,
                    "status": status,
                    "decision": final_decision,
                    "score": assessment.get("weighted_score", 0.0),
                    "risk": assessment.get("overall_risk", "none"),
                    "dims": json.dumps(risk_dimensions, ensure_ascii=False),
                    "summary": risk_summary,
                },
            )

    logger.info(
        "publish_results.done",
        submission_id=submission_id,
        final_decision=final_decision,
        weighted_score=assessment.get("weighted_score", 0.0),
    )

    # ── 清理临时帧文件 ──
    frames = state.get("frames", [])
    if frames:
        cleanup_frames(frames)

    return {
        "published": True,
        "final_decision": final_decision,
        "structured_output": {
            "submission_id": submission_id,
            "final_decision": final_decision,
            "weighted_score": assessment.get("weighted_score", 0.0),
            "overall_risk": assessment.get("overall_risk", "none"),
            "risk_dimensions": risk_dimensions,
            "risk_summary": risk_summary,
            "published": True,
        },
    }
