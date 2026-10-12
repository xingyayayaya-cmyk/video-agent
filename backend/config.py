# backend/config.py
# 全项目唯一的「配置中心」：从 .env.local 读取所有配置项，供任何模块取用。

from pydantic_settings import BaseSettings   # Pydantic 的「配置基类」，能自动从环境变量/.env 读取并做类型校验
from functools import lru_cache              # 标准库装饰器：缓存函数结果，让函数实际只执行一次
import os, sys
live_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

env_local_path = os.path.join(live_path, ".env.local")
# print(f"env_local_path: {env_local_path}")

class Settings(BaseSettings):
    """配置模型：每个类属性对应 .env.local 里的一项配置。
    继承 BaseSettings 后，Pydantic 会自动把同名（大小写不敏感）的配置读进来并转成对应类型。"""

    # ── 数据库（PostgreSQL）──
    db_host: str = "localhost"   # 主机；写了默认值 = 可选项
    db_port: int = 5433          # 端口；本机 5432 已占用，隔离到 5433
    db_name: str = "eduagent"    # 库名
    db_user: str                 # 用户名；没有默认值 = 必填，.env.local 缺了会启动报错
    db_password: str             # 密码；同样必填

    @property
    def database_url(self) -> str:
        """把上面几个散件拼成 SQLAlchemy 需要的连接串。
        用 @property 装饰后，可像访问属性一样 settings.database_url 取值，不用加括号调用。"""
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ── 大模型（DeepSeek）──
    deepseek_api_key: str                                   # 必填：DeepSeek 的 API Key
    deepseek_base_url: str = "https://api.deepseek.com/v1"  # DeepSeek 接口地址
    deepseek_model_chat: str = "deepseek-chat"              # 对话模型名
    deepseek_model_coder: str = "deepseek-coder"            # 代码模型名

    # ── OpenAI（GPT-4o Vision，用于视频帧视觉审核）──
    openai_api_key: str = ""                                     # 必填：OpenAI API Key
    openai_base_url: str = "https://api.openai.com/v1"           # OpenAI 接口地址

    # ── 大模型（Kimi，视觉轨/元数据轨）──
    # Kimi Code 套餐 key（sk-kimi- 前缀）走 coding 端点；该端点要求 temperature 固定为 1，
    # 且 kimi.com 系域名需经本机代理（Clash 7897）访问——客户端 trust_env=True 读环境变量。
    kimi_api_key: str = ""                                       # Kimi Code 套餐 API Key
    kimi_base_url: str = "https://api.kimi.com/coding/v1"        # Kimi coding 端点（OpenAI 兼容）
    openai_model_vision: str = "gpt-4o"                          # Vision 模型名
    openai_model_text: str = "gpt-4o"                            # 文本审核模型名

    # ── ASR 云端 API（音频转文字，用于语音轨审核）──
    asr_provider: str = "openai"                                 # ASR 供应商：openai / azure / google
    asr_api_key: str = ""                                        # ASR API Key
    asr_base_url: str = "https://api.openai.com/v1"              # ASR 接口地址（Azure 时填 endpoint）
    asr_model: str = "whisper-1"                                 # ASR 模型名
    asr_language: str = "en"                                     # ASR 语言提示（en/zh/es/ar...）

    # ── 视频处理（ffmpeg）──
    ffmpeg_path: str = "ffmpeg"                                  # ffmpeg 可执行文件路径
    ffprobe_path: str = "ffprobe"                                # ffprobe 可执行文件路径
    video_frame_interval: float = 5.0                           # 正片抽帧间隔（秒），每 N 秒抽 1 帧
    video_clip_interval: float = 2.0                            # 投流切片抽帧间隔（秒）——广告素材标准更严，抽帧更密
    video_max_frames: int = 30                                   # 单视频最大抽帧数（控制 Vision API 成本）
    video_max_duration: int = 600                                # 最大允许视频时长（秒），超限拒绝
    video_scene_threshold: float = 0.35                          # 场景切换检测灵敏度（0-1，越大越不敏感）；>0 时启用防闪帧规避抽帧

    # ── LangGraph Checkpointer（HITL 长流程的状态持久化）──
    # video_review 这类「上传→AI审核→人工复核」流程会在图中间 interrupt 暂停，
    # 若用进程内 MemorySaver，服务重启后待审任务会变成无法处理的孤儿。
    #   checkpoint_backend: sqlite（默认，Windows 兼容）/ postgres（Linux 生产可选）
    #   checkpoint_sqlite_path: SQLite 文件路径（相对项目根）
    checkpoint_backend: str = "sqlite"
    checkpoint_sqlite_path: str = "./.tools/checkpoints.db"

    # ── 出海区域审核配置（参考 ReelShort 面向市场）──
    # 支持区域：US(美国), EU(欧洲), LATAM(拉美), MENA(中东北非), SEA(东南亚)
    review_regions: str = "US,EU,LATAM,MENA,SEA"                 # 逗号分隔，可覆盖的区域列表
    review_default_region: str = "US"                            # 默认审核区域
    review_auto_approve_threshold: float = 0.85                  # 风险分 >= 此值 → 自动通过
    review_auto_reject_threshold: float = 0.30                  # 风险分 < 此值 → 自动拒绝
    review_confidence_threshold: float = 0.7                    # 置信度 < 此值 → 进人工复核

    # ── JWT 认证 ──
    jwt_secret_key: str                           # 必填：签发登录令牌用的密钥
    jwt_algorithm: str = "HS256"                  # 签名算法
    jwt_access_token_expire_minutes: int = 10080  # 令牌有效期（分钟）

    # ── 应用基础配置 ──
    app_env: str = "local"                     # 运行环境标识
    app_debug: bool = False                    # 是否调试模式
    app_host: str = "0.0.0.0"                  # 监听地址
    app_port: int = 8000                       # 监听端口
    log_level: str = "INFO"                    # 日志级别
    default_tenant_id: str = "tenant_default"  # 多租户默认值

    class Config:
        """Pydantic 的元配置：告诉 BaseSettings 该怎么读取配置。"""
        env_file = env_local_path          # 从这个文件读取配置
        env_file_encoding = "utf-8"      # 文件编码
        case_sensitive = False           # 大小写不敏感：环境变量 DB_HOST 能对应字段 db_host
        extra = "ignore"                 # .env.local 里多出来的、模型没定义的字段一律忽略（不报错）


@lru_cache()                             # 缓存：保证 get_settings() 只创建一次 Settings、只读一次文件
def get_settings() -> Settings:
    """获取全局唯一的配置对象。任何模块要用配置，都调用这个函数。"""
    return Settings()                    # 首次调用时创建实例；之后每次都返回同一个缓存对象


if __name__ == '__main__':
    settings = get_settings()
    print(settings.database_url)
    print(settings.db_user)
    ...
