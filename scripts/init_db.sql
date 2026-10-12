-- ============================================================
-- 出海 AI 短视频审核平台 · PostgreSQL 数据库初始化脚本
-- Docker 启动时自动执行（挂载到 /docker-entrypoint-initdb.d/）
-- ============================================================

-- UUID 生成说明：
--   本脚本统一使用 gen_random_uuid()，它是 PostgreSQL 13+ 的「内置函数」，
--   无需安装 uuid-ossp / pgcrypto 扩展，也不用 superuser 权限。
--   （旧版本曾用 uuid_generate_v4() + uuid-ossp 扩展，PG13 起已被内置函数取代）

-- ============================================================
-- 用户与权限表
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    username        VARCHAR(64) NOT NULL,
    email           VARCHAR(128) NOT NULL,
    password_hash   VARCHAR(256) NOT NULL,
    -- 平台角色：
    --   creator  创作者 —— 上传视频/海报、发起申诉
    --   reviewer 审核员 —— 人工复核、审核看板、处理申诉
    --   admin    管理员 —— 全部权限
    role            VARCHAR(16) NOT NULL
                    CHECK (role IN ('admin', 'creator', 'reviewer')),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tenant_id, email)
);
CREATE INDEX idx_users_tenant_id ON users (tenant_id);
CREATE INDEX idx_users_role ON users (role);


-- ============================================================
-- 出海 AI 短视频审核平台（ReelShort 类）表结构
-- ============================================================

-- 剧集表：一部短剧（如 "The Double Life of My Billionaire Husband"）
CREATE TABLE IF NOT EXISTS video_series (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    creator_id      UUID NOT NULL REFERENCES users(id),
    title           VARCHAR(256) NOT NULL,
    description     TEXT,
    genre           VARCHAR(64),                     -- romance / werewolf / billionaire ...
    target_region   VARCHAR(16) NOT NULL DEFAULT 'US',
    status          VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_video_series_creator ON video_series (creator_id, created_at DESC);

-- 视频表：一集正片 / 一条投流切片 / 一张封面海报
CREATE TABLE IF NOT EXISTS videos (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    creator_id      UUID NOT NULL REFERENCES users(id),
    series_id       UUID REFERENCES video_series(id) ON DELETE SET NULL,
    episode_no      INT,                           -- 正片的集号；切片/封面为 NULL
    content_type    VARCHAR(16) NOT NULL DEFAULT 'episode'
                    CHECK (content_type IN ('episode', 'clip', 'poster')),
    title           VARCHAR(256) NOT NULL,
    description     TEXT,
    tags            JSONB,
    file_path       TEXT,
    file_size       BIGINT DEFAULT 0,
    duration        FLOAT DEFAULT 0,
    status          VARCHAR(32) NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_videos_creator ON videos (creator_id, created_at DESC);
CREATE INDEX idx_videos_series ON videos (series_id, episode_no);

-- 提交审核表：一次上传 = 一条提交（走 LangGraph 审核流水线）
CREATE TABLE IF NOT EXISTS video_submissions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    video_id        UUID NOT NULL REFERENCES videos(id),
    creator_id      UUID NOT NULL REFERENCES users(id),
    series_id       UUID REFERENCES video_series(id) ON DELETE SET NULL,
    episode_no      INT,
    content_type    VARCHAR(16) NOT NULL DEFAULT 'episode',
    target_region   VARCHAR(16) NOT NULL DEFAULT 'US',
    status          VARCHAR(32) NOT NULL DEFAULT 'ai_processing',
    weighted_score  FLOAT,
    overall_risk    VARCHAR(16),
    final_decision  VARCHAR(32),
    risk_dimensions JSONB,
    risk_summary    TEXT,
    submitted_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at    TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_video_submissions_status ON video_submissions (tenant_id, status, submitted_at DESC);
CREATE INDEX idx_video_submissions_creator ON video_submissions (creator_id, submitted_at DESC);

-- 逐维度审核结果表（AI 预审核 + 审核员修改后的最终结论）
-- 注意：video_id 可空 —— 发布节点只写 submission_id（可反查 video），
--       保留该列作为弱引用以便直连查询。
CREATE TABLE IF NOT EXISTS video_reviews (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    submission_id   UUID NOT NULL REFERENCES video_submissions(id),
    video_id        UUID REFERENCES videos(id),
    dimension       VARCHAR(64) NOT NULL,
    risk_score      FLOAT NOT NULL,
    severity        VARCHAR(16) NOT NULL,
    confidence      FLOAT NOT NULL,
    violations_found INT NOT NULL DEFAULT 0,
    details         TEXT,
    suggestions     TEXT,
    reviewer_id     UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at     TIMESTAMPTZ
);
CREATE INDEX idx_video_reviews_submission ON video_reviews (submission_id);

-- 创作者申诉表：对 auto_rejected / published_rejected 结果发起申诉
-- 注意：本表在下方 updated_at 触发器列表中，因此必须有 updated_at 列，
--       否则 UPDATE 时触发器会报「record "new" has no field "updated_at"」。
CREATE TABLE IF NOT EXISTS video_appeals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       VARCHAR(64) NOT NULL DEFAULT 'tenant_default',
    submission_id   UUID NOT NULL REFERENCES video_submissions(id),
    creator_id      UUID NOT NULL REFERENCES users(id),
    reason          TEXT NOT NULL,
    status          VARCHAR(32) NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'under_review', 'upheld', 'overturned')),
    reviewer_id     UUID REFERENCES users(id),
    resolution_note TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);
CREATE INDEX idx_video_appeals_status ON video_appeals (tenant_id, status, created_at DESC);
CREATE INDEX idx_video_appeals_creator ON video_appeals (creator_id, created_at DESC);

-- ============================================================
-- 自动更新 updated_at 触发器
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'users',
        'video_series', 'videos', 'video_submissions', 'video_appeals'
    ]
    LOOP
        EXECUTE format('
            CREATE TRIGGER trg_%s_updated_at
            BEFORE UPDATE ON %s
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
        ', t, t);
    END LOOP;
END;
$$;
