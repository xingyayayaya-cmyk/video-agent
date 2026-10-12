# scripts/seed_data.py
# =============================================================================
# 本地开发数据种子脚本（Seed Script）
# -----------------------------------------------------------------------------
# 执行方式：在项目根目录运行
#   python scripts/seed_data.py
#
# 用途：
#   向 PostgreSQL 的 users 表灌入若干固定测试账号，方便本地联调登录、
#   权限（admin / teacher / student）等功能，无需手写 INSERT。
#
# 前置条件：
#   1. 数据库已按 backend 配置可连通（.env / 环境变量中的 DB_*）
#   2. users 表已存在（通常由迁移或建表脚本创建）
#   3. 表上有 (tenant_id, email) 唯一约束，用于幂等插入（重复执行安全）
# =============================================================================

import asyncio
import uuid
import os
import asyncpg                       # PostgreSQL 异步驱动；本脚本直接用它，不经过 SQLAlchemy
from passlib.context import CryptContext
from backend.config import get_settings

# -----------------------------------------------------------------------------
# bcrypt / passlib 兼容性补丁
# -----------------------------------------------------------------------------
# 新版 bcrypt 可能不再暴露 __about__.__version__，而 passlib 初始化时会去读它，
# 读不到会报错。这里与 auth.py 相同做法：若缺失则伪造一个轻量命名空间，
# 让 passlib 能拿到版本字符串并正常工作。
# -----------------------------------------------------------------------------
import bcrypt as _b, types as _t
if not hasattr(_b, "__about__"):
    _b.__about__ = _t.SimpleNamespace(__version__=getattr(_b, "__version__", "4.x"))

# 从项目统一配置读取数据库连接信息（用户名、密码、主机、端口、库名等）
s = get_settings()

# -----------------------------------------------------------------------------
# 组装 asyncpg 连接串（DSN）
# -----------------------------------------------------------------------------
# 注意协议前缀：
#   - SQLAlchemy 异步常用：postgresql+asyncpg://...
#   - asyncpg 原生连接只用：postgresql://...（不要带 +asyncpg）
# -----------------------------------------------------------------------------
DB_DSN = (
    f"postgresql://{s.db_user}:{s.db_password}"
    f"@{s.db_host}:{s.db_port}"
    f"/{s.db_name}"
)

# 调试用：打印完整 DSN（含密码）。仅适合本地；生产或共享环境请勿打印密码
print(f'DB_DSN: {DB_DSN}')

# passlib 密码上下文：使用 bcrypt 算法对明文密码做单向哈希
# deprecated="auto" 表示若检测到旧方案会自动迁移，当前仅配置 bcrypt
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# 默认租户 ID：与业务里「单租户 / 默认租户」约定一致，插入用户时写入同一 tenant
TENANT_ID = "tenant_default"


async def seed_users():
    """
    向 users 表灌入 4 个固定测试账号。

    行为说明：
    - 使用 INSERT ... ON CONFLICT (tenant_id, email) DO NOTHING：
      同一租户下邮箱已存在时跳过，不覆盖密码/角色，因此脚本可重复执行。
    - 密码只存 bcrypt 哈希，不存明文。
    - 每个用户生成新的 UUID 作为主键 id；冲突跳过时不会改已有行的 id。
    """
    # 建立一条异步数据库连接（脚本级一次性使用，结束后在 finally 中关闭）
    conn = await asyncpg.connect(DB_DSN)
    print("✅ 数据库连接成功，开始灌入测试账号...")
    try:
        # 测试账号清单：覆盖平台三种角色，便于联调 RBAC / 业务接口
        # 字段含义：
        #   username — 登录名展示用
        #   email    — 登录标识之一，且与 tenant_id 组成唯一键
        #   pwd      — 明文仅用于本脚本哈希后写入，切勿在业务日志中打印
        #   role     — 平台角色：admin（管理员）/ creator（创作者）/ reviewer（审核员）
        users = [
            {"username": "admin",      "email": "admin@eduagent.local",      "pwd": "Admin@123456",    "role": "admin"},
            {"username": "creator01",  "email": "creator01@eduagent.local",  "pwd": "Creator@123456",  "role": "creator"},
            {"username": "reviewer01", "email": "reviewer01@eduagent.local", "pwd": "Reviewer@123456", "role": "reviewer"},
        ]
        for u in users:
            # $1..$6 为 asyncpg 位置参数，按顺序对应下方传入的实参，可防 SQL 注入
            await conn.execute(
                """
                INSERT INTO users (id, tenant_id, username, email, password_hash, role)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (tenant_id, email) DO NOTHING
                """,
                str(uuid.uuid4()),                   # $1 id：新 UUID 字符串
                TENANT_ID,                           # $2 tenant_id：默认租户
                u["username"],                       # $3 username
                u["email"],                          # $4 email（冲突检测字段之一）
                pwd_context.hash(u["pwd"]),          # $5 password_hash：bcrypt，绝不存明文
                u["role"],                           # $6 role
            )
        # 控制台打印明文密码仅供本地开发登录；不要把本输出提交到公开日志/CI
        print(f"✅ 测试账号灌入完成（{len(users)} 个，已存在则跳过）：")
        print("   admin@eduagent.local      / Admin@123456      （管理员：全部权限）")
        print("   creator01@eduagent.local  / Creator@123456    （创作者：上传 + 申诉）")
        print("   reviewer01@eduagent.local / Reviewer@123456   （审核员：复核 + 看板）")
    finally:
        # 无论插入成功还是中途抛错，都关闭连接，避免连接泄漏
        await conn.close()


# 仅作为脚本直接运行时入口；被 import 时不会自动灌库
if __name__ == "__main__":
    # asyncio.run 创建事件循环并执行协程，结束后自动清理循环
    asyncio.run(seed_users())
