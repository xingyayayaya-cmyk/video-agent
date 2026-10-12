# backend/api/v1/video.py
#
# 视频审核 API — 出海 AI 短视频平台（ReelShort 类）
#
# ── LangGraph 九节点（详见 backend/agents/video_review/graph.py）────────────
#
#   阶段一 · 视频预处理
#     [1] extract_video_assets   按内容类型分支抽帧（正片/切片/海报）+ 音频分离 + ASR
#     [2] load_video_meta        从 DB 加载标题/描述/标签
#
#   阶段二 · AI 审核（规则包：区域 × 内容类型 二维注入 prompt）
#     [3] run_three_tracks       三轨并行：视觉 GPT-4o / 音频 GPT-4o / 元数据 GPT-4o
#     [4] aggregate_assessment   汇总三轨，计算六维度风险评分与自动决策
#
#   阶段三 · 风险分析
#     [5] analyze_risk           六维度风险详细分析
#
#   阶段四 · Human-in-the-Loop
#     [6] notify_reviewer        状态 → auto_approved/auto_rejected/pending_review
#     [7] reviewer_review        interrupt 暂停（仅 pending_review），等待审核员
#     [8] apply_reviewer_decision  合并审核员修改，产出最终结论
#
#   阶段五 · 发布
#     [9] publish_results        写入 DB，状态 → published_approved/published_rejected
#
# ── 角色（RBAC）────────────────────────────────────────────────────
#     creator   创作者：POST /submit、GET /my-submissions、POST /appeal
#     reviewer  审核员：GET /pending-reviews、GET /submissions/{id}/review、
#                       POST /submissions/{id}/confirm、GET /dashboard/*、申诉处理
#     admin     管理员：以上全部
#
# ── API 交互流程 ────────────────────────────────────────────────────
#
#   创作者  POST /submit                      上传视频（正片/切片/海报），202 立即返回
#           ↓ 后台异步执行节点 1~6
#           节点7 interrupt 暂停（仅 pending_review）
#   审核员  GET  /pending-reviews             待审列表
#           GET  /submissions/{id}/review     AI 预审核详情（三轨+六维度）
#           POST /submissions/{id}/confirm    approve / modify / reject，恢复图执行
#   创作者  GET  /my-submissions/{id}         轮询状态，published 后返回完整结果
#   创作者  POST /submissions/{id}/appeal     对拒绝结果发起申诉
#   审核员  GET  /appeals                     申诉队列
#           POST /appeals/{id}/resolve        处理申诉（upheld / overturned）

import asyncio
import json
import os
import uuid

from fastapi import APIRouter, Depends, UploadFile, File, Form, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from langgraph.types import Command
from jose import JWTError, jwt
from sqlalchemy import text

from backend.agents.video_review.graph import build_video_review_graph
from backend.dependencies import get_current_user, require_roles, AsyncSessionLocal
from backend.core.memory import build_config, build_thread_id
from backend.core.logger import get_logger
from backend.core.rulepacks import (
    build_review_context,
    list_content_types,
    list_regions,
)
from backend.config import get_settings

router = APIRouter()
logger = get_logger(__name__)
settings = get_settings()

# 上传文件持久化目录：项目根 /uploads。
# 不能存系统 TEMP —— 详情页要回放原视频，TEMP 会被系统定时清理。
# 旧记录 file_path 仍指向 TEMP，media 接口会兼容（文件没了给 404 提示重新上传）。
UPLOAD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
    "uploads",
)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 模块级图实例（惰性编译）
#
# 为什么不在此处直接 build_video_review_graph()：
#   图编译时会把 checkpointer 绑定进去。若在 import 阶段就编译，拿到的只能是
#   MemorySaver（此时 lifespan 尚未执行 init_persistent_checkpointer）；
#   改为首次请求时惰性编译，即可绑定已就绪的 AsyncPostgresSaver，
#   保证人工复核状态跨服务重启不丢失。
_graph = None


def get_graph():
    """惰性获取（并缓存）编译后的视频审核图。"""
    global _graph
    if _graph is None:
        _graph = build_video_review_graph()
    return _graph

# 持有 background task 引用，防止 asyncio GC 回收未完成的任务
_background_tasks: set[asyncio.Task] = set()

# 支持的视频格式
_ALLOWED_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv")
# 海报支持的图片格式
_POSTER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
# 内容类型
_CONTENT_TYPES = ("episode", "clip", "poster")


def _resolve_region(target_region: str) -> str:
    """校验并归一化目标区域。"""
    region = (target_region or settings.review_default_region).upper()
    configured = [r.strip().upper() for r in settings.review_regions.split(",")]
    # 配置列表之外但规则包存在的区域也放行（规则包是规则的权威来源）
    if region not in configured and region not in list_regions():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的区域：{region}，可选：{', '.join(list_regions())}",
        )
    return region


# ──────────────────────────────────────────────────────────────────────────────
# 剧集管理
# ──────────────────────────────────────────────────────────────────────────────

class SeriesCreateRequest(BaseModel):
    """创建剧集请求体。"""
    title: str
    description: str = ""
    genre: str = ""
    target_region: str = "US"


@router.get("/series")
async def list_my_series(
    current_user: dict = Depends(get_current_user),
):
    """创作者查询自己的剧集列表（含每部剧的集数统计）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT s.id, s.title, s.description, s.genre, s.target_region,
                       s.status, s.created_at,
                       COUNT(v.id) AS episode_count
                FROM video_series s
                LEFT JOIN videos v ON v.series_id = s.id AND v.content_type = 'episode'
                WHERE s.creator_id = :creator_id
                GROUP BY s.id
                ORDER BY s.created_at DESC
            """),
            {"creator_id": current_user["user_id"]},
        )
        rows = result.mappings().all()

    return {
        "items": [
            {
                "series_id": str(r["id"]),
                "title": r["title"],
                "description": r["description"] or "",
                "genre": r["genre"] or "",
                "target_region": r["target_region"],
                "status": r["status"],
                "episode_count": int(r["episode_count"]),
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ]
    }


@router.post("/series", status_code=201)
async def create_series(
    req: SeriesCreateRequest,
    current_user: dict = Depends(require_roles("creator")),
):
    """创作者创建剧集。"""
    region = _resolve_region(req.target_region)
    series_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO video_series (
                        id, tenant_id, creator_id, title, description,
                        genre, target_region, status, created_at
                    ) VALUES (
                        :id, :tenant_id, :creator_id, :title, :description,
                        :genre, :region, 'active', NOW()
                    )
                """),
                {
                    "id": series_id,
                    "tenant_id": current_user["tenant_id"],
                    "creator_id": current_user["user_id"],
                    "title": req.title,
                    "description": req.description,
                    "genre": req.genre,
                    "region": region,
                },
            )

    logger.info("video.series_created", series_id=series_id, creator_id=current_user["user_id"])
    return {"series_id": series_id, "status": "active"}


# ──────────────────────────────────────────────────────────────────────────────
# 创作者上传
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/meta-options")
async def get_meta_options(
    current_user: dict = Depends(get_current_user),
):
    """内容类型与区域选项（登录即可读，供前端表单渲染）。"""
    return {
        "content_types": list_content_types(),
        "regions": [{"value": r, "label": r} for r in list_regions()],
        "poster_extensions": list(_POSTER_EXTENSIONS),
        "video_extensions": list(_ALLOWED_EXTENSIONS),
    }


@router.post("/submit", status_code=202)
async def submit_video(
    title:         str         = Form(...),
    description:   str         = Form(default=""),
    tags:          str         = Form(default="[]"),
    target_region: str         = Form(default="US"),
    content_type:  str         = Form(default="episode"),
    series_id:     str         = Form(default=""),
    episode_no:    int         = Form(default=0),
    file:          UploadFile  = File(...),
    current_user:  dict        = Depends(require_roles("creator")),
):
    """创作者上传视频，触发 AI 三轨审核（异步后台）。

    输入：
        title:         视频标题
        description:   视频描述
        tags:          JSON 数组字符串，如 ["drama", "romance"]
        target_region: 目标审核区域（US/EU/LATAM/MENA/SEA）
        content_type:  episode 正片 / clip 投流切片 / poster 封面海报
        series_id:     关联剧集 ID（正片必填，切片/海报选填）
        episode_no:    正片集号（正片必填）
        file:          视频文件（poster 可为 jpg/png/webp）
        current_user:  JWT 鉴权注入（creator 角色）

    输出（202 Accepted）：
        { "submission_id": str, "video_id": str, "status": "ai_processing", "message": str }
    """
    # ── 校验内容类型 ──
    content_type = (content_type or "episode").lower()
    if content_type not in _CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"content_type 只能为：{', '.join(_CONTENT_TYPES)}",
        )

    # ── 校验文件格式 ──
    filename = file.filename or ""
    allowed = _POSTER_EXTENSIONS if content_type == "poster" else _ALLOWED_EXTENSIONS
    if not any(filename.lower().endswith(ext) for ext in allowed):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{content_type} 类型仅支持以下格式：{', '.join(allowed)}",
        )

    # ── 校验目标区域 ──
    region = _resolve_region(target_region)

    # ── 校验剧集归属（正片必须挂在自有剧集下）──
    creator_id = current_user["user_id"]
    resolved_series_id = None
    if series_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT id, creator_id, target_region
                    FROM video_series WHERE id = :sid
                """),
                {"sid": series_id},
            )
            row = result.mappings().fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="剧集不存在")
        if str(row["creator_id"]) != creator_id and current_user["role"] != "admin":
            raise HTTPException(status_code=403, detail="只能上传到本人创建的剧集")
        resolved_series_id = series_id

    if content_type == "episode":
        if not resolved_series_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="正片（episode）必须关联剧集 series_id",
            )
        if episode_no <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="正片必须填写集号 episode_no（从 1 开始）",
            )

    submission_id = str(uuid.uuid4())
    video_id = str(uuid.uuid4())

    # 保存上传文件到持久化目录（原视频需在详情页回放，不能用系统 TEMP）
    tmp_path = os.path.join(UPLOAD_DIR, f"{submission_id}_{filename}")
    content = await file.read()
    with open(tmp_path, "wb") as f:
        f.write(content)

    # ── 解析 tags JSON ──
    try:
        tags_list = json.loads(tags) if isinstance(tags, str) else tags
        if not isinstance(tags_list, list):
            tags_list = [str(tags_list)]
    except (json.JSONDecodeError, TypeError):
        tags_list = []

    # ── 写入 videos 表 ──
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO videos (
                        id, tenant_id, creator_id, series_id, episode_no,
                        content_type, title, description, tags,
                        file_path, file_size, duration, status, created_at
                    ) VALUES (
                        :id, :tenant_id, :creator_id, :series_id, :episode_no,
                        :content_type, :title, :description, :tags,
                        :file_path, :file_size, 0, 'pending', NOW()
                    )
                """),
                {
                    "id": video_id,
                    "tenant_id": current_user["tenant_id"],
                    "creator_id": creator_id,
                    "series_id": resolved_series_id,
                    "episode_no": episode_no if content_type == "episode" else None,
                    "content_type": content_type,
                    "title": title,
                    "description": description,
                    "tags": json.dumps(tags_list, ensure_ascii=False),
                    "file_path": tmp_path,
                    "file_size": len(content),
                },
            )

    # ── 写入 video_submissions 表 ──
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO video_submissions (
                        id, tenant_id, video_id, creator_id, series_id,
                        episode_no, content_type,
                        target_region, status, submitted_at
                    ) VALUES (
                        :id, :tenant_id, :video_id, :creator_id, :series_id,
                        :episode_no, :content_type,
                        :region, 'ai_processing', NOW()
                    )
                """),
                {
                    "id": submission_id,
                    "tenant_id": current_user["tenant_id"],
                    "video_id": video_id,
                    "creator_id": creator_id,
                    "series_id": resolved_series_id,
                    "episode_no": episode_no if content_type == "episode" else None,
                    "content_type": content_type,
                    "region": region,
                },
            )

    # ── 构造初始 State ──
    config = build_config(creator_id, submission_id)

    initial_state = {
        "messages": [],
        "creator_id": creator_id,
        "tenant_id": current_user["tenant_id"],
        "session_id": submission_id,
        "video_id": video_id,
        "submission_id": submission_id,
        "video_file_path": tmp_path,
        "target_region": region,
        "content_type": content_type,
        "video_meta": {},
        "frames": [],
        "audio_path": None,
        "audio_transcript": "",
        "visual_results": {},
        "audio_results": {},
        "metadata_results": {},
        "pre_review_assessment": {},
        "risk_dimensions": [],
        "risk_summary": "",
        "reviewer_notified": False,
        "reviewer_decision": None,
        "final_results": [],
        "final_decision": "",
        "published": False,
        "fallback_used": False,
        "structured_output": None,
    }

    # ── 后台任务 done callback ──
    def _on_task_done(t: asyncio.Task):
        _background_tasks.discard(t)
        task_failed = not t.cancelled() and t.exception() is not None

        if task_failed:
            logger.error(
                "video.background_task_failed",
                submission_id=submission_id,
                error=str(t.exception()),
                exc_info=t.exception(),
            )

        async def _cleanup():
            if task_failed:
                try:
                    async with AsyncSessionLocal() as db_sess:
                        async with db_sess.begin():
                            await db_sess.execute(
                                text("""
                                    UPDATE video_submissions
                                    SET status = 'failed', updated_at = NOW()
                                    WHERE id = :sid AND status = 'ai_processing'
                                """),
                                {"sid": submission_id},
                            )
                except Exception as db_err:
                    logger.warning(
                        "video.status_reset_failed",
                        submission_id=submission_id,
                        error=str(db_err),
                    )
            # 注意：原视频文件刻意保留在 uploads/ 供详情页回放，不在此清理

        cleanup_task = asyncio.ensure_future(_cleanup())
        _background_tasks.add(cleanup_task)
        cleanup_task.add_done_callback(_background_tasks.discard)

    # ── 启动后台审核任务 ──
    task = asyncio.create_task(get_graph().ainvoke(initial_state, config=config))
    _background_tasks.add(task)
    task.add_done_callback(_on_task_done)

    logger.info(
        "video.submitted",
        submission_id=submission_id,
        video_id=video_id,
        creator_id=creator_id,
        region=region,
        content_type=content_type,
    )

    return {
        "submission_id": submission_id,
        "video_id": video_id,
        "status": "ai_processing",
        "message": "Video submitted. AI review in progress. You will be notified when complete.",
    }


@router.get("/my-submissions")
async def list_my_submissions(
    current_user: dict = Depends(get_current_user),
):
    """创作者查询自己的视频提交记录列表。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT vs.id AS submission_id, vs.video_id, vs.target_region,
                       vs.status, vs.submitted_at, vs.final_decision, vs.weighted_score,
                       vs.content_type, vs.episode_no,
                       v.title AS video_title, s.title AS series_title
                FROM video_submissions vs
                LEFT JOIN videos v ON v.id = vs.video_id
                LEFT JOIN video_series s ON s.id = vs.series_id
                WHERE vs.creator_id = :creator_id
                ORDER BY vs.submitted_at DESC
                LIMIT 20
            """),
            {"creator_id": current_user["user_id"]},
        )
        rows = result.mappings().all()

    return {
        "items": [
            {
                "submission_id": str(r["submission_id"]),
                "video_id": str(r["video_id"]),
                "video_title": r["video_title"] or "",
                "series_title": r["series_title"] or "",
                "content_type": r["content_type"] or "episode",
                "episode_no": r["episode_no"],
                "target_region": r["target_region"],
                "status": r["status"],
                "final_decision": r["final_decision"] or "",
                "weighted_score": float(r["weighted_score"]) if r["weighted_score"] else None,
                "submitted_at": r["submitted_at"].isoformat() if r["submitted_at"] else None,
            }
            for r in rows
        ]
    }


@router.get("/my-submissions/{submission_id}")
async def get_my_submission(
    submission_id: str,
    current_user:  dict = Depends(get_current_user),
):
    """创作者查询单份提交的审核状态与结果（支持轮询）。

    未发布（status 未包含 published）→ 只返回 status
    已发布 → 返回完整审核结果
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, video_id, status, final_decision, weighted_score,
                       overall_risk, risk_dimensions, risk_summary,
                       target_region, content_type
                FROM video_submissions
                WHERE id = :sid AND creator_id = :creator_id
            """),
            {"sid": submission_id, "creator_id": current_user["user_id"]},
        )
        row = result.mappings().fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="提交记录不存在")

    # 附加视频元信息（创作者详情页展示标题/描述/标签用）
    async with AsyncSessionLocal() as session:
        vres = await session.execute(
            text("SELECT title, description, tags FROM videos WHERE id = :vid"),
            {"vid": row["video_id"]},
        )
        vrow = vres.mappings().fetchone()
    video_meta = {
        "title": (vrow["title"] if vrow else "") or "",
        "description": (vrow["description"] if vrow else "") or "",
        "tags": vrow["tags"] if vrow and vrow["tags"] else [],
    }
    if isinstance(video_meta["tags"], str):
        try:
            video_meta["tags"] = json.loads(video_meta["tags"])
        except (json.JSONDecodeError, TypeError):
            video_meta["tags"] = []

    # 未发布：只返回状态
    if "published" not in (row["status"] or ""):
        return {
            "submission_id": submission_id,
            "status": row["status"],
            "content_type": row["content_type"] or "episode",
            "target_region": row["target_region"],
            "video_meta": video_meta,
        }

    # 已发布：返回完整结果
    risk_dims = row["risk_dimensions"]
    if isinstance(risk_dims, str):
        try:
            risk_dims = json.loads(risk_dims)
        except (json.JSONDecodeError, TypeError):
            risk_dims = []
    elif risk_dims is None:
        risk_dims = []

    # 查询逐维度审核记录
    async with AsyncSessionLocal() as session:
        reviews_result = await session.execute(
            text("""
                SELECT dimension, risk_score, severity, confidence,
                       violations_found, details, suggestions, reviewer_id
                FROM video_reviews
                WHERE submission_id = :sid
                ORDER BY dimension
            """),
            {"sid": submission_id},
        )
        reviews = reviews_result.mappings().all()

    return {
        "submission_id": submission_id,
        "status": row["status"],
        "video_meta": video_meta,
        "final_decision": row["final_decision"],
        "weighted_score": float(row["weighted_score"]) if row["weighted_score"] else 0.0,
        "overall_risk": row["overall_risk"],
        "target_region": row["target_region"],
        "content_type": row["content_type"] or "episode",
        "risk_dimensions": [
            {
                "dimension": r["dimension"],
                "risk_score": float(r["risk_score"]),
                "severity": r["severity"],
                "confidence": float(r["confidence"]),
                "violations_found": r["violations_found"],
                "details": r["details"],
                "suggestions": r["suggestions"],
            }
            for r in reviews
        ],
        "risk_summary": row["risk_summary"] or "",
    }


# ──────────────────────────────────────────────────────────────────────────────
# 原视频回放（详情页 <video>/<img> 标签用）
# ──────────────────────────────────────────────────────────────────────────────

# 扩展名 → Content-Type（按上传时校验过的格式集）
_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _verify_media_token(token: str | None) -> dict:
    """校验 query param 携带的 JWT。

    为什么单独一个校验：<video>/<img> 标签发起的请求无法带 Authorization: Bearer 头，
    只能把 token 放 URL query 里（本地内网使用，风险可控）。
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="无效的认证凭证",
    )
    if not token:
        raise credentials_exception
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        user_id = payload.get("sub")
        if not user_id:
            raise credentials_exception
        return {
            "user_id": user_id,
            "role": payload.get("role", "creator"),
            "tenant_id": payload.get("tenant_id", settings.default_tenant_id),
        }
    except JWTError:
        raise credentials_exception


@router.get("/submissions/{submission_id}/media")
async def get_submission_media(
    submission_id: str,
    token:        str | None = Query(default=None),
    range_header: str | None = Header(default=None, alias="Range"),
):
    """按 Range 流式返回原始视频/海报文件，支持拖动进度条（Chrome 播放大文件必需 206）。

    权限：admin / reviewer 任意；creator 仅本人的提交。
    """
    user = _verify_media_token(token)

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT vs.creator_id, v.file_path
                FROM video_submissions vs
                JOIN videos v ON v.id = vs.video_id
                WHERE vs.id = :sid
            """),
            {"sid": submission_id},
        )
        row = result.mappings().fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="提交记录不存在")
    if user["role"] not in ("admin", "reviewer") and str(row["creator_id"]) != user["user_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只能查看本人的提交")

    file_path = row["file_path"]
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(
            status_code=404,
            detail="原视频文件已不存在（早期版本存于系统临时目录，可能已被清理），请重新上传",
        )

    file_size = os.path.getsize(file_path)
    media_type = _MEDIA_TYPES.get(os.path.splitext(file_path)[1].lower(), "application/octet-stream")

    # 解析 Range 头：bytes=start-end / bytes=start- / bytes=-suffix
    start, end, resp_status = 0, file_size - 1, 200
    if range_header and range_header.startswith("bytes="):
        try:
            unit = range_header[len("bytes="):].split(",")[0]
            s, e = unit.split("-")
            if s:
                start = int(s)
                end = int(e) if e else file_size - 1
            else:
                start = max(file_size - int(e), 0)   # 后缀范围：取最后 N 字节
            end = min(end, file_size - 1)
            if 0 <= start <= end < file_size:
                resp_status = 206
        except (ValueError, IndexError):
            start, end, resp_status = 0, file_size - 1, 200

    content_length = end - start + 1
    headers = {"Accept-Ranges": "bytes"}
    if resp_status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    def _iter():
        with open(file_path, "rb") as f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(_iter(), status_code=resp_status, media_type=media_type, headers=headers)


# ──────────────────────────────────────────────────────────────────────────────
# 审核员工作台
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/pending-reviews")
async def get_pending_reviews(
    status: str = Query(default="pending_review"),
    current_user: dict = Depends(require_roles("reviewer")),
):
    """审核员获取提交列表（看板下钻用 status 过滤）。

    status 可选：pending_review（默认）/ auto_approved / auto_rejected /
    published_approved / published_rejected / ai_processing / failed / all
    """
    allowed_statuses = {
        "pending_review", "auto_approved", "auto_rejected",
        "published_approved", "published_rejected", "ai_processing", "failed", "all",
    }
    if status not in allowed_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"status 只能为：{', '.join(sorted(allowed_statuses))}",
        )

    where = "vs.tenant_id = :tenant_id"
    params: dict = {"tenant_id": current_user["tenant_id"]}
    if status != "all":
        where += " AND vs.status = :status"
        params["status"] = status

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(f"""
                SELECT
                    vs.id AS submission_id,
                    vs.video_id,
                    vs.creator_id,
                    vs.status,
                    vs.content_type,
                    vs.episode_no,
                    u.username AS creator_name,
                    v.title AS video_title,
                    s.title AS series_title,
                    vs.target_region,
                    vs.submitted_at
                FROM video_submissions vs
                JOIN users u ON u.id = vs.creator_id
                JOIN videos v ON v.id = vs.video_id
                LEFT JOIN video_series s ON s.id = vs.series_id
                WHERE {where}
                ORDER BY vs.submitted_at DESC
                LIMIT 200
            """),
            params,
        )
        rows = result.mappings().all()

    items = []
    for row in rows:
        submission_id = str(row["submission_id"])
        pre_review = {}
        risk_dims = []

        # 从 MemorySaver 读取 AI 预审核结果
        try:
            thread_id = await _get_thread_id(submission_id)
            config = {"configurable": {"thread_id": thread_id}}
            snapshot = await get_graph().aget_state(config)
            if snapshot and snapshot.values:
                sv = snapshot.values
                assessment = sv.get("pre_review_assessment", {})
                pre_review = {
                    "weighted_score": assessment.get("weighted_score", 0),
                    "overall_risk": assessment.get("overall_risk", "unknown"),
                    "decision": assessment.get("decision", "pending_review"),
                }
                risk_dims = sv.get("risk_dimensions", [])
        except Exception as _e:
            logger.warning(
                "video.pending_review_state_read_failed",
                submission_id=submission_id, error=str(_e),
            )

        items.append({
            "submission_id": submission_id,
            "video_id": str(row["video_id"]),
            "creator_name": row["creator_name"],
            "video_title": row["video_title"],
            "series_title": row["series_title"] or "",
            "status": row["status"],
            "content_type": row["content_type"] or "episode",
            "episode_no": row["episode_no"],
            "target_region": row["target_region"],
            "submitted_at": row["submitted_at"].isoformat() if row["submitted_at"] else None,
            "pre_review": pre_review,
            "risk_dimensions": risk_dims[:3],
        })

    return {"items": items, "total": len(items)}


@router.get("/submissions/{submission_id}/review")
async def get_submission_review(
    submission_id: str,
    current_user:  dict = Depends(require_roles("reviewer")),
):
    """审核员获取某份提交的 AI 预审核详情（图在节点7 interrupt 时的 State）。"""
    config = {"configurable": {"thread_id": await _get_thread_id(submission_id)}}

    try:
        state_snapshot = await get_graph().aget_state(config)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"找不到该提交的审核状态：{e}")

    if not state_snapshot or not state_snapshot.values:
        raise HTTPException(status_code=404, detail="审核尚未完成或记录不存在")

    sv = state_snapshot.values
    content_type = sv.get("content_type", "episode")
    return {
        "submission_id": submission_id,
        "creator_id": sv.get("creator_id", ""),
        "content_type": content_type,
        "video_meta": sv.get("video_meta", {}),
        "frames_count": len(sv.get("frames", [])),
        "visual_results": sv.get("visual_results", {}),
        "audio_results": sv.get("audio_results", {}),
        "metadata_results": sv.get("metadata_results", {}),
        "pre_review_assessment": sv.get("pre_review_assessment", {}),
        "risk_dimensions": sv.get("risk_dimensions", []),
        "risk_summary": sv.get("risk_summary", ""),
        "target_region": sv.get("target_region", "US"),
        # 注入该区域 × 内容类型的规则上下文，供审核员对照规则逐条复核
        "review_context": build_review_context(
            sv.get("target_region", "US"), content_type
        ),
    }


class ConfirmRequest(BaseModel):
    """审核员确认请求体"""
    action:        str = "approve"        # approve / modify / reject
    modifications: list[dict] = []        # modify 时的维度修改
    review_notes:  str = ""


@router.post("/submissions/{submission_id}/confirm")
async def confirm_review(
    submission_id: str,
    req:           ConfirmRequest,
    current_user:  dict = Depends(require_roles("reviewer")),
):
    """审核员确认审核结果，恢复 LangGraph interrupt，执行节点8~9并发布。

    action:
        approve → 沿用 AI 评分，标记通过
        modify  → 按 modifications 覆盖特定维度评分后通过
        reject  → 标记拒绝
    """
    if req.action not in ("approve", "modify", "reject"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="action 只能为 approve、modify 或 reject",
        )

    thread_id = await _get_thread_id(submission_id)
    config = {"configurable": {"thread_id": thread_id}}

    decision = {
        "action": req.action,
        "modifications": req.modifications,
        "reviewer_id": current_user["user_id"],
        "review_notes": req.review_notes,
    }

    try:
        result = await get_graph().ainvoke(Command(resume=decision), config=config)
    except Exception as e:
        logger.error("video.confirm_failed", submission_id=submission_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"发布失败：{e}",
        )

    structured = result.get("structured_output", {}) or {}

    logger.info(
        "video.confirmed",
        submission_id=submission_id,
        reviewer_id=current_user["user_id"],
        action=req.action,
    )

    return {
        "submission_id": submission_id,
        "status": "published_approved" if structured.get("final_decision") == "approved" else "published_rejected",
        "final_decision": structured.get("final_decision", ""),
        "weighted_score": structured.get("weighted_score", 0),
        "overall_risk": structured.get("overall_risk", "none"),
        "risk_dimensions": structured.get("risk_dimensions", []),
        "risk_summary": structured.get("risk_summary", ""),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 审核看板（运营指标）
# ──────────────────────────────────────────────────────────────────────────────

@router.get("/dashboard/stats")
async def get_dashboard_stats(
    current_user: dict = Depends(require_roles("reviewer")),
):
    """审核看板：三级分流占比、处理量、均分（对齐需求文档 §5 指标）。"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'auto_approved')    AS auto_approved,
                    COUNT(*) FILTER (WHERE status = 'auto_rejected')    AS auto_rejected,
                    COUNT(*) FILTER (WHERE status = 'pending_review')   AS pending_review,
                    COUNT(*) FILTER (WHERE status LIKE 'published%')    AS published,
                    COUNT(*) FILTER (WHERE status = 'failed')           AS failed,
                    AVG(weighted_score) FILTER (WHERE weighted_score IS NOT NULL) AS avg_score,
                    AVG(EXTRACT(EPOCH FROM (updated_at - submitted_at))) FILTER (
                        WHERE status LIKE 'published%' OR status LIKE 'auto_%'
                    ) AS avg_review_seconds
                FROM video_submissions
                WHERE tenant_id = :tenant_id
            """),
            {"tenant_id": current_user["tenant_id"]},
        )
        row = result.mappings().fetchone()

        content_split = await session.execute(
            text("""
                SELECT content_type, COUNT(*) AS cnt
                FROM video_submissions
                WHERE tenant_id = :tenant_id
                GROUP BY content_type
            """),
            {"tenant_id": current_user["tenant_id"]},
        )
        by_type = {r["content_type"]: int(r["cnt"]) for r in content_split.mappings().all()}

        # 审核员推翻 AI 自动结论的比例（申诉被 overturned = AI 拒绝被推翻）
        appeals = await session.execute(
            text("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'overturned') AS overturned
                FROM video_appeals
                WHERE tenant_id = :tenant_id
            """),
            {"tenant_id": current_user["tenant_id"]},
        )
        appeal_row = appeals.mappings().fetchone()

    total = int(row["total"] or 0)

    def _rate(n) -> float:
        return round(float(n or 0) / total, 4) if total else 0.0

    return {
        "total": total,
        "auto_approved": int(row["auto_approved"] or 0),
        "auto_rejected": int(row["auto_rejected"] or 0),
        "pending_review": int(row["pending_review"] or 0),
        "published": int(row["published"] or 0),
        "failed": int(row["failed"] or 0),
        "rates": {
            "auto_approve_rate": _rate(row["auto_approved"]),
            "auto_reject_rate": _rate(row["auto_rejected"]),
            "manual_review_rate": _rate(row["pending_review"]),
        },
        "avg_weighted_score": round(float(row["avg_score"]), 4) if row["avg_score"] else None,
        "avg_review_seconds": round(float(row["avg_review_seconds"]), 1) if row["avg_review_seconds"] else None,
        "by_content_type": by_type,
        "appeals": {
            "total": int(appeal_row["total"] or 0),
            "overturned": int(appeal_row["overturned"] or 0),
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# 申诉闭环
# ──────────────────────────────────────────────────────────────────────────────

class AppealRequest(BaseModel):
    """创作者申诉请求体。"""
    reason: str


@router.post("/submissions/{submission_id}/appeal", status_code=201)
async def create_appeal(
    submission_id: str,
    req:           AppealRequest,
    current_user:  dict = Depends(require_roles("creator")),
):
    """创作者对拒绝结果发起申诉（仅 rejected 状态可申诉）。"""
    if not req.reason.strip():
        raise HTTPException(status_code=400, detail="申诉理由不能为空")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, status, creator_id FROM video_submissions WHERE id = :sid
            """),
            {"sid": submission_id},
        )
        row = result.mappings().fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="提交记录不存在")
    if str(row["creator_id"]) != current_user["user_id"] and current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="只能申诉本人的提交")
    if "rejected" not in (row["status"] or ""):
        raise HTTPException(
            status_code=400,
            detail=f"当前状态（{row['status']}）不可申诉，仅被拒绝的提交可申诉",
        )

    # 幂等：同一提交已有未结申诉则直接返回
    #
    # 注意 SQLAlchemy 会话语义：AsyncSessionLocal 的 autobegin 会在首次
    # execute 时自动开启事务；若之后再调用 session.begin()，会抛
    #「a transaction is already begun on this Session」。
    # 因此这里统一用 execute + commit，不嵌套 begin()。
    async with AsyncSessionLocal() as session:
        dup = await session.execute(
            text("""
                SELECT id FROM video_appeals
                WHERE submission_id = :sid AND status IN ('open', 'under_review')
            """),
            {"sid": submission_id},
        )
        existing = dup.fetchone()
        if existing:
            return {"appeal_id": str(existing[0]), "status": "open", "message": "该提交已有进行中的申诉"}

        appeal_id = str(uuid.uuid4())
        await session.execute(
            text("""
                INSERT INTO video_appeals (
                    id, tenant_id, submission_id, creator_id, reason, status, created_at
                ) VALUES (
                    :id, :tenant_id, :sid, :creator_id, :reason, 'open', NOW()
                )
            """),
            {
                "id": appeal_id,
                "tenant_id": current_user["tenant_id"],
                "sid": submission_id,
                "creator_id": current_user["user_id"],
                "reason": req.reason.strip(),
            },
        )
        await session.commit()

    logger.info("video.appeal_created", appeal_id=appeal_id, submission_id=submission_id)
    return {"appeal_id": appeal_id, "status": "open"}


@router.get("/appeals")
async def list_appeals(
    status_filter: str = "open",
    current_user: dict = Depends(require_roles("reviewer")),
):
    """审核员获取申诉队列（默认看 open）。"""
    allowed_status = ("open", "under_review", "upheld", "overturned")
    if status_filter not in allowed_status:
        status_filter = "open"

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT a.id, a.submission_id, a.reason, a.status, a.created_at,
                       a.resolution_note,
                       u.username AS creator_name,
                       v.title AS video_title,
                       vs.final_decision, vs.weighted_score, vs.content_type
                FROM video_appeals a
                JOIN users u ON u.id = a.creator_id
                JOIN video_submissions vs ON vs.id = a.submission_id
                JOIN videos v ON v.id = vs.video_id
                WHERE a.tenant_id = :tenant_id AND a.status = :st
                ORDER BY a.created_at DESC
            """),
            {"tenant_id": current_user["tenant_id"], "st": status_filter},
        )
        rows = result.mappings().all()

    return {
        "items": [
            {
                "appeal_id": str(r["id"]),
                "submission_id": str(r["submission_id"]),
                "creator_name": r["creator_name"],
                "video_title": r["video_title"],
                "content_type": r["content_type"] or "episode",
                "reason": r["reason"],
                "status": r["status"],
                "final_decision": r["final_decision"] or "",
                "weighted_score": float(r["weighted_score"]) if r["weighted_score"] else None,
                "resolution_note": r["resolution_note"] or "",
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


class AppealResolveRequest(BaseModel):
    """审核员处理申诉请求体。"""
    resolution: str       # upheld（维持拒绝） / overturned（推翻，改判通过）
    note: str = ""


@router.post("/appeals/{appeal_id}/resolve")
async def resolve_appeal(
    appeal_id: str,
    req:       AppealResolveRequest,
    current_user: dict = Depends(require_roles("reviewer")),
):
    """审核员处理申诉：upheld 维持原判 / overturned 推翻并改判通过。"""
    if req.resolution not in ("upheld", "overturned"):
        raise HTTPException(status_code=400, detail="resolution 只能为 upheld 或 overturned")

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, submission_id, status
                FROM video_appeals WHERE id = :aid
            """),
            {"aid": appeal_id},
        )
        row = result.mappings().fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="申诉不存在")
    if row["status"] in ("upheld", "overturned"):
        raise HTTPException(status_code=400, detail="该申诉已处理完毕")

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                text("""
                    UPDATE video_appeals
                    SET status = :st, reviewer_id = :rid, resolution_note = :note,
                        resolved_at = NOW()
                    WHERE id = :aid
                """),
                {"st": req.resolution, "rid": current_user["user_id"],
                 "note": req.note, "aid": appeal_id},
            )
            if req.resolution == "overturned":
                # 推翻 → 提交改判为 published_approved
                await session.execute(
                    text("""
                        UPDATE video_submissions
                        SET status = 'published_approved', final_decision = 'approved',
                            updated_at = NOW()
                        WHERE id = :sid
                    """),
                    {"sid": str(row["submission_id"])},
                )

    logger.info(
        "video.appeal_resolved",
        appeal_id=appeal_id, resolution=req.resolution, reviewer_id=current_user["user_id"],
    )
    return {"appeal_id": appeal_id, "status": req.resolution}


# ── 辅助函数 ───────────────────────────────────────────────────

async def _get_thread_id(submission_id: str) -> str:
    """从 DB 获取 creator_id，拼出 MemorySaver 的 thread_id。"""
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT creator_id FROM video_submissions WHERE id = :sid"),
                {"sid": submission_id},
            )
            row = result.fetchone()
            if row:
                return build_thread_id(str(row[0]), submission_id)
    except Exception as e:
        logger.warning("video.get_thread_id_failed", submission_id=submission_id, error=str(e))
    return f"student_unknown_session_{submission_id}"
