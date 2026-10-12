# backend/main.py
"""
出海 AI 短视频审核平台 · 后端入口：创建 FastAPI 应用、挂载业务路由。
本文件不写具体业务，只负责把应用组装起来并管好启动/关闭。

组装顺序（本文件主线）：
  1. 定义 lifespan 启动钩子
  2. 创建 FastAPI 应用
  3. 配 CORS
  4. 挂路由

1. 定义 lifespan 启动钩子
   yield 之前 = 启动（阻塞到就绪才接请求）；yield 之后 = 关闭（SIGTERM / Ctrl+C）。
   启动顺序：初始化结构化日志 → 幂等 DB Schema 迁移 → 初始化持久化 Checkpointer
   （SQLite，供人工复核中断状态跨重启恢复）。
   关闭时清空 LLMFactory 缓存，避免连接泄漏。
   降级：迁移 / Checkpointer 初始化失败只告警，不拦启动。

2. 创建 FastAPI 应用
   创建 API 应用，挂上 /docs、/redoc，并把 lifespan 绑到应用实例。
   前置兜底：Windows + conda 补 DLL 搜索路径。

3. 配 CORS
   放行本地常见前端端口（3000 / 5173 / 8080 / 5500 / 8765），
   以及 file:// 打开页面时的 Origin "null"，方便开发调试。

4. 挂路由
   - REST：/api/v1（认证、视频审核，由 api_router 聚合）
   - GET /health：探活，不依赖数据库或模型
"""

import os
import sys

# ---------------------------------------------------------------------------
# Windows + conda 环境补丁
# ---------------------------------------------------------------------------
# conda 的 _lzma.pyd 依赖 base 环境 Library/bin 下的 liblzma.dll。
# 若当前解释器是 env 里的 python，默认搜不到该 DLL，import 阶段会报错。
# 把 base/Library/bin 加入 DLL 搜索路径即可；非 Windows 直接跳过。
if sys.platform == "win32":
    _conda_base_lib_bin = os.path.normpath(
        os.path.join(os.path.dirname(sys.executable), "..", "..", "Library", "bin")
    )
    if os.path.isdir(_conda_base_lib_bin):
        os.add_dll_directory(_conda_base_lib_bin)

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.config import get_settings                          # 配置：环境变量 / .env
from backend.core.logger import configure_logging, get_logger    # 结构化日志
from backend.api.router import api_router                        # 聚合的总路由

settings = get_settings()


# TODO 1.定义 lifespan 启动钩子
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI 生命周期钩子：启动时初始化资源，关闭时释放缓存。

    yield 之前 = 启动阶段（阻塞直到就绪，之后才开始接请求）；
    yield 之后 = 关闭阶段（uvicorn 收到 SIGTERM / Ctrl+C 时执行）。
    """
    configure_logging()                                  # 初始化结构化日志（3.3），此后才能用 get_logger
    logger = get_logger(__name__)
    logger.info("app.starting | env=%s port=%s", settings.app_env, settings.app_port)

    # ① DB Schema 自动迁移（幂等，每次启动执行）—— 第 3 章的建表/迁移
    # 失败只告警不抛出：开发机没起数据库时，仍希望 /docs、健康检查能打开。
    try:
        from backend.db.migrations import run_migrations
        await run_migrations()
    except Exception as e:
        logger.warning("app.migrations_failed | error=%s", e)

    # ①-b 持久化 Checkpointer（默认 SQLite）
    # 视频审核是「上传 → AI 审核 → 人工复核」的长流程，人工复核阶段图会
    # interrupt 暂停。若用进程内 MemorySaver，服务一重启 checkpoint 就没了，
    # DB 里 pending_review 的任务会变成无法处理的孤儿。
    # 此处注入持久化 saver，使中断状态可跨重启恢复。
    # 必须在任何图被编译之前完成（video 路由改为惰性编译正是为此）。
    try:
        import os as _os

        from backend.core.memory import init_persistent_checkpointer
        # normpath 而非 lstrip("./")：后者按「字符集」剥离，
        # 会把 ".tools" 开头那个点也吃掉，导致路径变成 "tools/..."。
        _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        _sqlite_path = _os.path.join(
            _root, _os.path.normpath(settings.checkpoint_sqlite_path)
        )
        ok = await init_persistent_checkpointer(
            database_url=settings.database_url,
            sqlite_path=_sqlite_path,
            backend=settings.checkpoint_backend,
        )
        logger.info("app.checkpointer | backend=%s persistent=%s", settings.checkpoint_backend, ok)
    except Exception as e:
        logger.warning("app.checkpointer_init_failed | error=%s", e)

    # ② 关闭阶段逻辑（下面 yield 之后调用）
    async def _shutdown() -> None:
        """关闭阶段：清 LLM 客户端缓存，避免进程退出时连接泄漏。"""
        logger.info("app.shutting_down")
        from backend.core.llm_factory import LLMFactory
        LLMFactory.clear_cache()
        logger.info("app.shutdown_complete")

    logger.info("app.started")
    yield
    await _shutdown()


# TODO 2.创建 FastAPI 应用
app = FastAPI(
    title="出海短视频审核平台 API",
    description="AI 短视频审核系统 API（三轨并行审核 + 六维度风险 + 人工复核）",
    version="1.0.0",
    docs_url="/docs",                                    # Swagger UI：交互式调试
    redoc_url="/redoc",                                  # ReDoc：只读文档
    lifespan=lifespan,                                   # 绑定上面的启动/关闭钩子
)

# TODO 3.配 CORS
# 浏览器从其他源访问本 API 时，必须显式放行 Origin。
# 开发阶段覆盖常见前端端口；allow_origin_regex 兜底任意 localhost 端口。
# "null"：直接双击打开 file:// HTML 时，浏览器 Origin 为字符串 "null"。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000", "http://localhost:5173", "http://localhost:8080",
        "http://127.0.0.1:3000", "http://127.0.0.1:5173", "http://127.0.0.1:8080",
        "http://localhost:5500", "http://127.0.0.1:5500",   # VS Code / Cursor Live Server
        "http://localhost:8765", "http://127.0.0.1:8765",   # part_7 静态测试页
        "null",                                             # 直接双击打开 file:// 的 HTML
    ],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,                              # 允许携带 Cookie / Authorization
    allow_methods=["*"],                                 # 开发期不限制方法
    allow_headers=["*"],                                 # 开发期不限制自定义头
)

# TODO 4.挂路由
# 业务接口统一挂在 /api/v1 下（认证、视频审核，见 api_router）
app.include_router(api_router, prefix="/api/v1")


@app.get("/health", tags=["系统"])
async def health_check():
    """
    健康检查：运维探活、负载均衡探活使用。
    不依赖数据库或模型，只要进程活着就返回 ok。
    """
    return {"status": "ok", "env": settings.app_env}
