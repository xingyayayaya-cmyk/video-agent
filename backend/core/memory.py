# backend/core/memory.py
# =============================================================================
# LangGraph Checkpointer：状态持久化 + thread_id + config 构造
#
# 设计动机：
#   视频审核是「上传 → AI 三轨审核 → 人工复核 → 发布」的长流程。
#   图会在「人工复核」节点 interrupt 暂停，等审核员确认后再 resume。
#   若用进程内 MemorySaver：
#     1. 服务一重启，checkpoint 全丢 → DB 里 pending_review 的待审任务
#        变成无法处理的孤儿（详情接口 404、审批直接报错）；
#     2. 多实例部署时请求打到不同实例，状态不共享。
#
#   因此本模块提供持久化 Checkpointer，业务侧仍通过
#   get_memory_saver / build_config 接入，无需感知后端差异。
#
# 后端选型：
#   - sqlite（默认）：跨平台、零外部依赖。
#     ⚠️ PostgreSQL 方案（AsyncPostgresSaver + psycopg3）在 Windows 上不可用 ——
#        psycopg3 明确拒绝 ProactorEventLoop，而 uvicorn 在 Windows 默认用它
#        （因为 SelectorEventLoop 不支持 subprocess，会破坏能力）。
#   - postgres（可选）：Linux 生产环境可与主库共用。
#
# 调用关系：
#   - agents/video_review/graph.py → get_memory_saver("video_review")
#   - api/v1/video.py              → build_thread_id / build_config
#   - main.py (lifespan)           → init_persistent_checkpointer
# =============================================================================

from langgraph.checkpoint.memory import MemorySaver

from backend.core.logger import get_logger

logger = get_logger(__name__)

# ── Checkpointer 实例池 ─────────────────────────────────────────────────────
# key = agent_type（如 "video_review"），value = 独立 saver。
#
# 为何必须按 Agent 隔离：
#   LangGraph 用 msgpack 序列化 State；不同 Agent 的 TypedDict 字段集合不同。
#   若共用同一 saver，同一进程内后编译的图可能读到另一 Agent 写入的检查点，
#   触发 schema 字段冲突或反序列化失败。
_memory_savers: dict[str, MemorySaver] = {}

# ── 持久化 Checkpointer 状态 ────────────────────────────────────────────────
# 由 main.py 的 lifespan 调用 init_persistent_checkpointer 注入。
_persistent_saver = None

# 持有连接引用：aiosqlite 连接若被 GC 回收，checkpointer 会失效
_persistent_conn = None

# 需要跨重启保留 checkpoint 的 Agent（HITL 长流程）
_PERSISTENT_AGENTS: set[str] = {"video_review"}


def _to_psycopg_dsn(sqlalchemy_url: str) -> str:
    """把 SQLAlchemy 连接串转成 psycopg 可用的 DSN。

    SQLAlchemy 异步驱动串：postgresql+asyncpg://...
    psycopg 只认：        postgresql://...
    """
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def init_persistent_checkpointer(
    database_url: str,
    sqlite_path: str,
    backend: str = "sqlite",
) -> bool:
    """初始化持久化 Checkpointer 并建表（幂等）。供 FastAPI lifespan 调用。

    必须在任何图被编译之前完成 —— 图编译时会把 checkpointer 绑定进去，
    因此 video 路由采用惰性编译（get_graph()）而非模块级编译。

    Args:
        database_url: SQLAlchemy 连接串（postgres 后端时使用）
        sqlite_path:  SQLite 文件路径（sqlite 后端时使用）
        backend:      "sqlite"（默认，跨平台）/ "postgres"（Linux 生产可选）

    Returns:
        True  → 持久化 checkpointer 就绪，_PERSISTENT_AGENTS 将使用它
        False → 初始化失败，已降级为内存存储（仅告警，不影响启动）
    """
    global _persistent_saver, _persistent_conn

    if _persistent_saver is not None:
        return True

    # ── 后端 1：SQLite（默认）──
    if backend == "sqlite":
        try:
            import os

            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            os.makedirs(os.path.dirname(sqlite_path), exist_ok=True)

            # 注意：不用 AsyncSqliteSaver.from_conn_string()——它是 async with
            # 上下文管理器，退出即关连接；这里需长期持有，故手动建连接。
            _persistent_conn = await aiosqlite.connect(sqlite_path)
            saver = AsyncSqliteSaver(_persistent_conn)
            await saver.setup()          # 建 checkpoint 表（幂等）

            _persistent_saver = saver
            logger.info(
                "memory.persistent_checkpointer_ready",
                backend="sqlite",
                path=sqlite_path,
                agents=sorted(_PERSISTENT_AGENTS),
            )
            return True

        except Exception as e:  # noqa: BLE001
            logger.warning("memory.persistent_checkpointer_failed", backend="sqlite", error=str(e))
            _persistent_saver = None
            _persistent_conn = None
            return False

    # ── 后端 2：PostgreSQL（Linux 生产环境可选）──
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        dsn = _to_psycopg_dsn(database_url)
        pool = AsyncConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=10,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await pool.open(wait=True, timeout=15)
        saver = AsyncPostgresSaver(pool)
        await saver.setup()

        _persistent_saver = saver
        logger.info("memory.persistent_checkpointer_ready", backend="postgres")
        return True

    except Exception as e:  # noqa: BLE001
        logger.warning("memory.persistent_checkpointer_failed", backend="postgres", error=str(e))
        _persistent_saver = None
        return False


def has_persistent_checkpointer() -> bool:
    """持久化 checkpointer 是否已就绪（供健康检查 / 诊断用）。"""
    return _persistent_saver is not None


def get_memory_saver(agent_type: str = "default"):
    """
    获取指定 Agent 类型的 Checkpointer 单例。

    优先级：
      1. 若 agent_type 属于 _PERSISTENT_AGENTS 且持久化 checkpointer 已就绪
         → 返回持久化 saver（跨重启保留，HITL 场景必需）
      2. 否则 → 返回进程内 MemorySaver（懒创建并缓存）

    Args:
        agent_type: Agent 标识符，如 "video_review"。
                    须与 graph 编译处传入的字符串一致，否则等于另开一套记忆。

    Returns:
        Checkpointer 实例，传给 StateGraph.compile(checkpointer=...)
    """
    # 长流程优先用持久化后端
    if agent_type in _PERSISTENT_AGENTS and _persistent_saver is not None:
        return _persistent_saver

    if agent_type not in _memory_savers:
        _memory_savers[agent_type] = MemorySaver()
        logger.info("memory.saver_initialized", agent=agent_type, backend="memory")
    return _memory_savers[agent_type]


def build_thread_id(creator_id: str, session_id: str) -> str:
    """
    构建 LangGraph Checkpointer 使用的 thread_id。

    thread_id 是检查点的主键：同一 thread_id 连续 ainvoke / astream 会恢复
    上一次的 State；不同 thread_id 互不共享历史。

    格式：student_{creator_id}_session_{session_id}
    - 用户维度：便于按创作者排查与清理；
    - 会话维度：同一创作者的多份提交互不干扰。

    视频审核场景下 session_id 取 submission_id，
    使「一次上传」对应一条检查点链。

    Example:
        build_thread_id("abc123", "xyz789")
        → "student_abc123_session_xyz789"
    """
    return f"student_{creator_id}_session_{session_id}"


def build_config(creator_id: str, session_id: str) -> dict:
    """
    构建 LangGraph 调用所需的 config 字典。

    LangGraph 约定：检查点相关参数放在 config["configurable"] 下；
    最关键的是 thread_id。本函数封装该样板，避免各 API 手写拼错 key。

    用法：
        config = build_config(creator_id, submission_id)
        result = await graph.ainvoke(state, config=config)

    Returns:
        {"configurable": {"thread_id": "student_xxx_session_yyy"}}
    """
    return {
        "configurable": {
            "thread_id": build_thread_id(creator_id, session_id),
        }
    }
