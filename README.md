# 出海 AI 短视频内容审核平台

> 面向短剧 / 投流素材出海场景的 **AI 多智能体内容审核系统**（ReelShort 类业务）。
> 基于 **LangGraph + FastAPI + Vue3** 构建，用「三轨并行预审 + 六维度风险聚合 + 人工复核闭环」替代人工逐条看片。

---

## 业务背景

短剧 / 广告素材出海，内容合规是第一道生死线：不同国家与地区对暴力、性暗示、涉政、宗教、未成年、文化禁忌的判定标准完全不同，纯人工审核既慢又难保证一致性。

本平台把审核拆成 **AI 预审 → 加权分流 → 人工复核 → 申诉改判** 四段闭环，让机器处理掉大部分确定性内容，人力只聚焦在「AI 拿不准」的灰区。

---

## 核心能力

| 能力 | 说明 |
|---|---|
| 三轨并行预审 | 视觉轨（抽帧逐帧看图）+ 音频轨（语音转写后再审）+ 元数据轨（标题/简介/标签），LangGraph `Send` API 动态分派并行执行 |
| 六维度风险聚合 | 暴力 / 性暗示 / 涉政 / 宗教 / 未成年 / 文化禁忌，聚合为加权安全分与总体风险等级 |
| 三级分流 | `≥0.85` 自动通过、`≤0.30` 自动拒绝、中间区间转人工，阈值可配 |
| HITL 人工复核 | 图在复核节点 `interrupt()` 暂停，审核员基于 AI 三轨证据做 approve / modify / reject 终审，`Command(resume=...)` 恢复 |
| 申诉改判闭环 | 创作者对拒绝结果可发起申诉，审核员裁定可 `overturned` 推翻原结论 |
| 三角色 RBAC | 创作者 / 审核员 / 管理员，JWT + 依赖注入鉴权，前端按角色渲染菜单 |
| 持久化中断状态 | Checkpointer 落盘，服务重启后待审任务不丢、可继续处理 |
| 原片回放 | 上传的原始视频持久化保留，审核详情页可直接播放，支持 Range 拖动 |

---

## 审核流水线

```
                ┌──────────────────────────────┐
   创作者上传 ──▶ │  视频元信息解析 / ffmpeg 抽帧  │
                └───────────────┬──────────────┘
                                │  Send API 并行分派
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
   ┌─────────┐            ┌─────────┐            ┌──────────┐
   │ 视觉轨   │            │ 音频轨   │            │ 元数据轨  │
   │ 抽帧→VLM │            │ ASR→文本 │            │ 文本审核  │
   └────┬────┘            └────┬────┘            └────┬─────┘
        └───────────────────────┼───────────────────────┘
                                ▼
                    ┌────────────────────────┐
                    │ 六维度风险聚合 + 加权分  │
                    └───────────┬────────────┘
                                ▼
              ┌─────────────────┴─────────────────┐
        ≥0.85 │                            ≤0.30 │   0.30 ~ 0.85
              ▼                                   ▼        ▼
        ┌──────────┐                      ┌──────────┐ ┌──────────────┐
        │ 自动通过  │                      │ 自动拒绝  │ │ interrup 人工复核 │
        └──────────┘                      └────┬─────┘ └───────┬──────┘
                                               │               │
                                               │         审核员终审
                                               ▼               ▼
                                         ┌───────────────────────┐
                                         │  创作者申诉 → 改判闭环  │
                                         └───────────────────────┘
```

---

## 技术栈

| 层面 | 选型 |
|---|---|
| 开发语言 | Python 3.12 |
| Web 框架 | FastAPI + uvicorn |
| Agent 框架 | LangGraph（Send API 并行 / interrupt-resume / Checkpointer） |
| 视觉轨 · 元数据轨 | Kimi K2.6（OpenAI 兼容 coding 端点，支持图像输入） |
| 聚合轨 · 区域轨 | DeepSeek Chat |
| 音频转写 | 阿里百炼 qwen3-omni（全模态，中文语音理解） |
| 模型调度 | 自研 LLM Factory：按业务路由模型、统一温度/结构化输出/失败降级 |
| 视频处理 | ffmpeg（抽帧 / 场景切换检测 / 音轨分离） |
| 关系数据库 | PostgreSQL + SQLAlchemy 2.x（全异步）+ asyncpg |
| 状态持久化 | LangGraph Checkpointer（SQLite / PostgreSQL 可切换） |
| 鉴权 | JWT（python-jose）+ FastAPI 依赖注入 RBAC |
| 前端 | Vue3 + TypeScript + Vite + Element Plus + Pinia |
| 部署 | Docker Compose（nginx + uvicorn + postgres 三容器） |

---

## 快速开始

### 方式一：Docker 一键启动（推荐）

```bash
cp .env.example .env          # 填 DB_PASSWORD / JWT_SECRET_KEY / 各家 API Key
docker compose up -d --build
docker compose exec backend python scripts/seed_data.py   # 灌测试账号（仅首次）
```

访问 <http://localhost:3000> 。详细说明见 **[DOCKER.md](DOCKER.md)**。

### 方式二：本地开发

```bash
# 1. 装依赖
pip install -r requirements.txt        # 需自行准备 ffmpeg 并配 FFMPEG_PATH

# 2. 起数据库
docker compose up -d postgres

# 3. 配环境变量
cp .env.example .env.local             # 本地开发读取 .env.local

# 4. 起后端
uvicorn backend.main:app --host 0.0.0.0 --port 8000

# 5. 起前端
cd frontend && npm install && npm run dev
```

---

## 访问地址与测试账号

| 地址 | 说明 |
|---|---|
| <http://localhost:3000> | 前端应用 |
| <http://localhost:8000/docs> | FastAPI Swagger 文档 |
| <http://localhost:8000/health> | 健康检查 |

| 角色 | 账号 | 密码 | 主要职责 |
|---|---|---|---|
| 创作者 | `creator01` | `Creator@123456` | 上传视频、查看提交记录与原片、发起申诉 |
| 审核员 | `reviewer01` | `Reviewer@123456` | 待审队列、证据复核、终审决策、看板、处理申诉 |
| 管理员 | `admin` | `Admin@123456` | 全部权限（超级用户） |

---

## 目录结构

```
EduAgent/
├── backend/
│   ├── main.py                     # FastAPI 入口 & lifespan（迁移 + Checkpointer 初始化）
│   ├── config.py                   # pydantic-settings 配置中心（读 .env.local / 环境变量）
│   ├── dependencies.py             # JWT 鉴权与角色校验（require_roles）
│   ├── api/
│   │   ├── router.py               # 路由总入口（/auth + /video）
│   │   └── v1/
│   │       ├── auth.py             # 登录 / 当前用户
│   │       └── video.py            # 提交、复核、看板、申诉、媒体流
│   ├── agents/video_review/
│   │   ├── graph.py                # 9 节点审核状态机（含 Send 并行与 interrupt）
│   │   ├── nodes.py                # 各节点实现（抽帧/转写/三轨/聚合）
│   │   ├── prompts.py              # 审核提示词（按区域 × 内容类型）
│   │   └── state.py                # 图状态定义
│   ├── core/
│   │   ├── llm_factory.py          # 多模型统一路由层
│   │   ├── video_processor.py      # ffmpeg 抽帧 / ASR 转写
│   │   ├── rulepacks.py            # 出海区域 × 内容类型规则包
│   │   ├── memory.py               # 持久化 Checkpointer
│   │   └── logger.py               # 结构化日志
│   └── db/migrations.py            # 启动时自动执行的幂等迁移
├── frontend/src/
│   ├── views/video/                # 上传 / 提交详情 / 复核队列 / 复核详情 / 看板
│   ├── api/                        # axios 封装（client / auth / video）
│   ├── router/                     # 路由与角色守卫
│   └── stores/auth.ts              # Pinia 登录态
├── scripts/
│   ├── init_db.sql                 # 建表脚本（容器初始化自动执行）
│   └── seed_data.py                # 测试账号种子
├── docker/
│   ├── Dockerfile.backend          # python:3.12-slim + ffmpeg
│   ├── Dockerfile.frontend         # node 构建 → nginx 托管
│   └── nginx.conf                  # SPA 回退 + /api 反向代理
├── docker-compose.yml              # postgres + backend + frontend 全栈编排
├── DOCKER.md                       # Docker 部署指南
├── requirements.txt
└── .env.example
```

---

## 容错设计

| 层级 | 触发条件 | 处理方式 |
|---|---|---|
| 模型级降级 | 单条轨道 LLM 调用失败 / 超时 | 该轨按规则兜底给分，不阻断整体流水线 |
| 解析级加固 | LLM 返回 JSON 夹带解释文字 | 截取首个完整 JSON 块再解析 |
| 长流程恢复 | 服务重启 / 进程被杀 | Checkpointer 落盘，人工复核状态可续；DB 中 `pending_review` 不会变成孤儿任务 |
| 上传持久化 | 审核完成后 | 原视频**不删除**，保留在 `uploads/` 供详情页回放 |

---

## License

本项目仅供学习与技术演示使用。
