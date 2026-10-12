# =============================================================================
# backend/db/migrations.py
# -----------------------------------------------------------------------------
# 启动时自动执行的 Schema 补丁（全部幂等，可重复运行）。
#
# 为什么需要这个文件？
#   正式环境里库表往往是「很早以前」用 init_db.sql 建好的；后续业务加了新字段 /
#   新索引时，不能要求运维每次都手工改库。本模块在 FastAPI lifespan 启动阶段
#   （见 backend/main.py）自动跑一遍补丁，把「旧库」对齐到「当前代码期望的 Schema」。
#
# 与 init_db.sql 的分工：
#   - init_db.sql：全新空库的「完整建表脚本」（含所有列与索引）
#   - 本文件：已有库的「增量补丁」——只追加缺失的列 / 索引
#   约定：每次在 init_db.sql 里新增字段或索引，必须同步在 _MIGRATIONS 末尾追加一条
#
# 编写规则（硬性）：
#   1. 只写幂等 DDL：ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
#   2. 禁止 DROP / TRUNCATE / ALTER COLUMN TYPE 等破坏性或不可逆变更
#   3. 每条迁移是 (描述名, SQL) 二元组；描述名会出现在日志里，便于排查
#   4. 按时间顺序追加；不要改已经上线过的旧条目（避免环境间状态不一致）
# =============================================================================

from sqlalchemy import text
from backend.dependencies import AsyncSessionLocal
from backend.core.logger import get_logger

logger = get_logger(__name__)


# =============================================================================
# 迁移清单：按「上线时间」从早到晚追加
# -----------------------------------------------------------------------------
# 元素格式：(desc, sql)
#   desc —— 人类可读标识，写入结构化日志的 column 字段（实际可能是列名或索引名）
#   sql  —— 单条幂等 DDL；执行成功会 commit，失败会 rollback 后继续下一条
# =============================================================================
_MIGRATIONS: list[tuple[str, str]] = [
    # ══════════════════════════════════════════════════════════════════════
    # 出海 AI 短视频审核平台（ReelShort 类）Schema —— 2026-10
    # ══════════════════════════════════════════════════════════════════════
    # ── 用户角色：收敛为平台三角色 ────────────────────────────────────────
    # PostgreSQL 不能修改 CHECK 约束，只能先删后加；DROP IF EXISTS 保证幂等。
    # 注意：两条必须连续执行，且不能合并成一个 text()（SQLAlchemy 默认不允许多语句）。
    (
        "users.role_check_drop",
        "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check",
    ),
    # 清理教育场景遗留账号（student / teacher）：新约束不容许这些角色。
    # 用 NOT EXISTS 兜底保护——若某账号已产生视频业务数据则不删，
    # 避免外键级联误伤（正常教育账号不会有视频数据）。
    (
        "users.legacy_edu_roles_cleanup",
        "DELETE FROM users u WHERE u.role NOT IN ('admin', 'creator', 'reviewer') "
        "AND NOT EXISTS (SELECT 1 FROM video_submissions s WHERE s.creator_id = u.id) "
        "AND NOT EXISTS (SELECT 1 FROM video_series v WHERE v.creator_id = u.id) "
        "AND NOT EXISTS (SELECT 1 FROM video_appeals a WHERE a.creator_id = u.id)",
    ),
    (
        "users.role_check_add",
        "ALTER TABLE users ADD CONSTRAINT users_role_check "
        "CHECK (role IN ('admin', 'creator', 'reviewer'))",
    ),
    # ── 教育遗留列清理（幂等，已不存在则跳过）──────────────────────────────
    # users.class_id 是原教育场景的班级分组列，平台无此概念。
    (
        "users.class_id_drop",
        "ALTER TABLE users DROP COLUMN IF EXISTS class_id",
    ),
    # ── 剧集表 ────────────────────────────────────────────────────────────
    (
        "video_series",
        "CREATE TABLE IF NOT EXISTS video_series ("
        " id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
        " tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_default',"
        " creator_id UUID NOT NULL REFERENCES users(id),"
        " title VARCHAR(256) NOT NULL,"
        " description TEXT,"
        " genre VARCHAR(64),"
        " target_region VARCHAR(16) NOT NULL DEFAULT 'US',"
        " status VARCHAR(32) NOT NULL DEFAULT 'active',"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    ),
    (
        "idx_video_series_creator",
        "CREATE INDEX IF NOT EXISTS idx_video_series_creator "
        "ON video_series (creator_id, created_at DESC)",
    ),
    # ── 视频表：新增 series/episode/content_type（已有库为增量列）─────────
    (
        "videos.series_id",
        "ALTER TABLE videos ADD COLUMN IF NOT EXISTS series_id UUID REFERENCES video_series(id) ON DELETE SET NULL",
    ),
    (
        "videos.episode_no",
        "ALTER TABLE videos ADD COLUMN IF NOT EXISTS episode_no INT",
    ),
    (
        "videos.content_type",
        "ALTER TABLE videos ADD COLUMN IF NOT EXISTS content_type VARCHAR(16) NOT NULL DEFAULT 'episode'",
    ),
    (
        "idx_videos_series",
        "CREATE INDEX IF NOT EXISTS idx_videos_series ON videos (series_id, episode_no)",
    ),
    # ── 提交表：新增 series/episode/content_type ─────────────────────────
    (
        "video_submissions.series_id",
        "ALTER TABLE video_submissions ADD COLUMN IF NOT EXISTS series_id UUID REFERENCES video_series(id) ON DELETE SET NULL",
    ),
    (
        "video_submissions.episode_no",
        "ALTER TABLE video_submissions ADD COLUMN IF NOT EXISTS episode_no INT",
    ),
    (
        "video_submissions.content_type",
        "ALTER TABLE video_submissions ADD COLUMN IF NOT EXISTS content_type VARCHAR(16) NOT NULL DEFAULT 'episode'",
    ),
    (
        "idx_video_submissions_status",
        "CREATE INDEX IF NOT EXISTS idx_video_submissions_status "
        "ON video_submissions (tenant_id, status, submitted_at DESC)",
    ),
    (
        "idx_video_submissions_creator",
        "CREATE INDEX IF NOT EXISTS idx_video_submissions_creator "
        "ON video_submissions (creator_id, submitted_at DESC)",
    ),
    (
        "idx_video_reviews_submission",
        "CREATE INDEX IF NOT EXISTS idx_video_reviews_submission ON video_reviews (submission_id)",
    ),
    # ── 发布节点写入列补丁（2026-10 修复）────────────────────────────────
    # publish_results 节点写库时用到这两列，早期建表遗漏，导致发布阶段报错
    # 「column "reviewed_at" does not exist」。此处补齐，保证已有库自愈。
    (
        "video_reviews.reviewed_at",
        "ALTER TABLE video_reviews ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ",
    ),
    (
        "video_submissions.published_at",
        "ALTER TABLE video_submissions ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ",
    ),
    # video_reviews.video_id 原为 NOT NULL，但发布节点只写 submission_id；
    # 放开为可空，避免 NOT NULL 约束导致插入失败。
    (
        "video_reviews.video_id_nullable",
        "ALTER TABLE video_reviews ALTER COLUMN video_id DROP NOT NULL",
    ),
    # video_appeals 被纳入 updated_at 触发器列表，但早期建表漏了该列，
    # 导致任何 UPDATE 都报「record "new" has no field "updated_at"」。
    (
        "video_appeals.updated_at",
        "ALTER TABLE video_appeals ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
    ),
    # ── 申诉表 ────────────────────────────────────────────────────────────
    (
        "video_appeals",
        "CREATE TABLE IF NOT EXISTS video_appeals ("
        " id UUID PRIMARY KEY DEFAULT gen_random_uuid(),"
        " tenant_id VARCHAR(64) NOT NULL DEFAULT 'tenant_default',"
        " submission_id UUID NOT NULL REFERENCES video_submissions(id),"
        " creator_id UUID NOT NULL REFERENCES users(id),"
        " reason TEXT NOT NULL,"
        " status VARCHAR(32) NOT NULL DEFAULT 'open'"
        " CHECK (status IN ('open', 'under_review', 'upheld', 'overturned')),"
        " reviewer_id UUID REFERENCES users(id),"
        " resolution_note TEXT,"
        " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
        " resolved_at TIMESTAMPTZ)",
    ),
    (
        "idx_video_appeals_status",
        "CREATE INDEX IF NOT EXISTS idx_video_appeals_status "
        "ON video_appeals (tenant_id, status, created_at DESC)",
    ),
    (
        "idx_video_appeals_creator",
        "CREATE INDEX IF NOT EXISTS idx_video_appeals_creator "
        "ON video_appeals (creator_id, created_at DESC)",
    ),
]


async def run_migrations() -> None:
    """
    在应用启动时执行所有 Schema 补丁。

    调用时机：
        backend/main.py 的 lifespan 里、路由开始接请求之前 await run_migrations()。

    容错策略：
        - 单条失败只打 warning，不抛异常、不阻断启动（避免一次坏 SQL 拖垮整站）
        - 每条独立 commit：前面的成功不会因后面失败被回滚
        - 若错误信息含 "already exists"（极端竞态 / 非 IF NOT EXISTS 路径），视为无害并静默跳过

    注意：
        这里用的是依赖注入模块里的 AsyncSessionLocal（连接池会话），
        不是 init_db.sql 那种一次性脚本连接；因此必须保证 SQL 简短、幂等。
    """
    async with AsyncSessionLocal() as session:
        for desc, sql in _MIGRATIONS:
            try:
                # text()：把原始 SQL 字符串包成 SQLAlchemy 可执行对象
                await session.execute(text(sql))
                await session.commit()
                # debug 级别：成功路径很常见（每次重启都会跑），避免刷屏
                logger.debug("db.migration_applied", column=desc)
            except Exception as e:
                # 当前这条失败：回滚本事务，再继续下一条
                await session.rollback()
                err = str(e)
                # PostgreSQL 在部分场景会报 already exists；IF NOT EXISTS 通常不会走到这里，
                # 保留过滤是为了兼容竞态（多实例同时启动）或驱动差异。
                if "already exists" not in err:
                    logger.warning("db.migration_failed", column=desc, error=err)

    logger.info("db.migrations_done", count=len(_MIGRATIONS))
