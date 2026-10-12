# =============================================================================
# backend/dependencies.py
# -----------------------------------------------------------------------------
# FastAPI「依赖注入」模块：把「数据库会话」和「当前登录用户」封装成可复用的依赖，
# 供各业务接口通过 Depends(...) 自动注入，避免每个路由里重复写连接 / 鉴权代码。
#
# 本文件职责一览：
#   1. 创建异步 SQLAlchemy 引擎与会话工厂（连接池）
#   2. get_db()          —— 每个请求拿到独立的 AsyncSession，结束时自动提交/回滚
#   3. get_current_user() —— 解析 Authorization: Bearer <JWT>，校验后返回用户信息
#
# 典型用法（在路由里）：
#   async def api(db: AsyncSession = Depends(get_db),
#                 user: dict = Depends(get_current_user)):
#       ...
# =============================================================================

from typing import AsyncGenerator
from fastapi import Depends, HTTPException, status
# HTTPBearer：声明「本接口需要 Bearer Token」的安全方案；
# HTTPAuthorizationCredentials：解析后的凭证对象，.credentials 即 token 字符串
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
# AsyncSession / create_async_engine / async_sessionmaker：
# SQLAlchemy 2.x 异步 API，配合 asyncpg 驱动访问 PostgreSQL
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
# python-jose：JWT 编解码；JWTError 覆盖签名错误、过期、格式错误等
from jose import JWTError, jwt
import os, sys

# ── 把项目根目录加入 sys.path，保证「直接运行本文件」时也能 import backend.xxx ──
# __file__ → .../EduAgent/backend/dependencies.py
# dirname 两次 → .../EduAgent（项目根）
eduagents_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(eduagents_path)

from backend.config import get_settings

# 全局唯一配置对象（内部有 @lru_cache，多次调用不会重复读 .env.local）
settings = get_settings()


# =============================================================================
# 一、异步数据库引擎（连接池）
# =============================================================================
# create_async_engine 只应在进程内创建一次：引擎内部维护连接池，
# 请求来了从池里借连接，用完归还，避免「每个请求都新建 TCP 连接」的开销。
engine = create_async_engine(
    settings.database_url,  # 形如 postgresql+asyncpg://user:pwd@host:port/db
                            # 由 config.Settings.database_url 拼出，最终来自 .env.local
    pool_size=10,           # 池内常驻连接数：平时最多同时复用 10 条连接
    max_overflow=20,        # 高峰时可临时再开 20 条；总上限 ≈ 10 + 20 = 30
    echo=False,             # True 时把每条 SQL 打到日志，排错时临时打开即可
)


# =============================================================================
# 二、会话工厂
# =============================================================================
# async_sessionmaker：根据引擎「批量生产」AsyncSession 的工厂。
# expire_on_commit=False：
#   默认 commit 后 ORM 对象属性会过期，再次访问要重新查库；
#   设为 False 后，commit 后仍可直接读对象字段（接口返回前很方便），
#   代价是可能读到 commit 前的内存快照，一般 API 场景可接受。
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：为单个请求提供异步数据库会话。

    生命周期（配合 Depends 使用时由 FastAPI 驱动）：
      1. 进入 async with，从连接池取出一条连接，创建 Session
      2. yield session —— 控制权交给路由函数，路由里用这个 session 读写
      3. 路由正常返回 → 执行 await session.commit() 提交事务
      4. 路由抛异常   → 执行 await session.rollback() 回滚，再把异常抛出去
      5. async with 结束 → Session / 连接自动关闭归还连接池

    注意：这是「生成器依赖」，yield 前后的代码分别对应「请求开始」和「请求结束」。
    """
    async with AsyncSessionLocal() as session:
        try:
            ##耗时操作

            yield session             # 把会话交给接口使用
            await session.commit()    # 接口正常结束 → 自动提交
        except Exception:
            await session.rollback()  # 出错 → 自动回滚，保证数据一致性
            raise                     # 必须再抛出，否则 FastAPI 收不到错误状态


# =============================================================================
# 三、JWT 鉴权
# =============================================================================
# HTTPBearer()：
#   - 告诉 OpenAPI / Swagger：此接口需要 Authorization: Bearer <token>
#   - 请求头缺失或格式不是 "Bearer xxx" 时，FastAPI 会直接返回 401，进不了下面的函数
bearer_scheme = HTTPBearer()


async def get_current_user(
    # Depends(bearer_scheme)：先跑 HTTPBearer，拿到凭证再注入本函数
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """FastAPI 依赖：验证 JWT，返回当前用户上下文。

    返回值结构（供业务代码使用）：
        {
          "user_id":   str,  # JWT 标准声明 sub，登录时写入
          "role":      str,  # 业务自定义：student / teacher 等，缺省 student
          "tenant_id": str,  # 多租户 ID，缺省用 settings.default_tenant_id
        }

    Token 无效、过期、缺 sub 时统一抛 HTTP 401，并带 WWW-Authenticate: Bearer，
    方便前端识别「需要重新登录」。
    """
    # 预先构造统一的 401 异常，下面多处 raise，避免重复写参数
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效的认证凭证",
        # 按 RFC 6750，告诉客户端应使用 Bearer 方案重新认证
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # jwt.decode 会校验：
        #   - 签名是否用 jwt_secret_key + jwt_algorithm 匹配
        #   - 是否已过期（若签发时带了 exp）
        # 任一失败都会抛 JWTError
        payload = jwt.decode(
            credentials.credentials,              # 去掉 "Bearer " 后的纯 token 字符串
            settings.jwt_secret_key,              # 必须与签发端（登录接口）使用同一密钥
            algorithms=[settings.jwt_algorithm],  # 一般是 HS256；写成列表防止算法混淆攻击
        )
        # 从 payload 取业务字段；.get 第二参数是缺省值
        user_id: str = payload.get("sub")  # RFC 7519：sub（subject）表示「令牌主体」= 用户 ID
        role: str = payload.get("role", "student")
        tenant_id: str = payload.get("tenant_id", settings.default_tenant_id)

        # sub 是必填身份标识；没有则视为伪造/残缺 Token
        if not user_id:
            raise credentials_exception

    except JWTError:
        # 签名错误、过期、非法 Base64、算法不匹配等，一律按「凭证无效」处理
        # （不要把具体 JWT 错误细节回给客户端，避免泄露验签信息）
        raise credentials_exception

    return {"user_id": user_id, "role": role, "tenant_id": tenant_id}


# =============================================================================
# 四、角色鉴权（视频审核平台：creator / reviewer / admin）
# =============================================================================

def require_roles(*allowed_roles: str):
    """FastAPI 依赖工厂：要求当前用户属于指定角色之一（admin 恒有权限）。

    用法：
        @router.post("/submit")
        async def submit(..., user: dict = Depends(require_roles("creator", "admin"))):
            ...

    未登录 → 401（由 get_current_user 抛出）；
    角色不允许 → 403。
    """
    async def _checker(current_user: dict = Depends(get_current_user)) -> dict:
        role = current_user.get("role", "")
        if role != "admin" and role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"该操作需要 { '/'.join(allowed_roles) } 角色权限",
            )
        return current_user

    return _checker


# =============================================================================
# 四、本地快速自检（python -m backend.dependencies 或直接运行本文件）
# =============================================================================
if __name__ == "__main__":
    # 不经过 FastAPI，手动迭代 get_db()，验证：
    #   - .env.local 里的库配置能否连上
    #   - 异步引擎 / 会话是否工作正常
    import asyncio
    from sqlalchemy import text

    async def main():
        # async for 取到 yield 出来的 session；离开循环体后会走 commit/关闭逻辑
        async for db in get_db():
            r = await db.execute(
                text(
                    "SELECT current_database(), "
                    "count(*) FROM information_schema.tables WHERE table_schema = :s"
                ),
                {"s": "public"},
            )
            row = r.fetchone()
            print("get_db 连到库:", row[0])
            print("public 下表数量:", row[1])

    asyncio.run(main())
    print("get_db 测试通过 ✅")
