# Docker 部署指南

出海 AI 短视频审核平台的容器化方案：一条命令拉起 **PostgreSQL + FastAPI 后端 + Vue 前端** 全栈。

---

## 一、前置条件

| 依赖 | 版本要求 | 说明 |
|---|---|---|
| Docker Engine | 20.10+ | Linux / macOS / Windows(WSL2) 均可 |
| Docker Compose | v2（`docker compose` 子命令） | 不是老的 `docker-compose` |
| 磁盘 | ≥ 3 GB | 镜像 + 数据库卷 |

> 后端镜像内置 **ffmpeg**，宿主机无需额外安装。

---

## 二、快速开始

```bash
# 1. 准备环境变量（密钥不入库，必须自己建一份）
cp .env.example .env
#    编辑 .env，至少填：DB_PASSWORD / JWT_SECRET_KEY /
#    DEEPSEEK_API_KEY / KIMI_API_KEY / ASR_API_KEY

# 2. 构建并启动全栈
docker compose up -d --build

# 3. 灌入测试账号（只需执行一次）
docker compose exec backend python scripts/seed_data.py
```

启动完成后：

| 入口 | 地址 |
|---|---|
| 前端页面 | http://localhost:3000 |
| 后端 API 文档 | http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health |
| PostgreSQL | `localhost:5433`（容器内 5432） |

### 测试账号

| 角色 | 账号 | 密码 |
|---|---|---|
| 管理员 | `admin` | `Admin@123456` |
| 创作者 | `creator01` | `Creator@123456` |
| 审核员 | `reviewer01` | `Reviewer@123456` |

---

## 三、架构

```
                    ┌──────────────────────────────┐
   浏览器  ───────▶ │  frontend (nginx:80 → 3000)  │
                    │  静态资源 + /api 反向代理     │
                    └───────────────┬──────────────┘
                                    │ http://backend:8000
                                    ▼
                    ┌──────────────────────────────┐
                    │  backend (uvicorn:8000)      │
                    │  FastAPI + LangGraph 审核流水线│
                    │  内置 ffmpeg（抽帧/转码）      │
                    └───────────────┬──────────────┘
                                    │ postgres:5432
                                    ▼
                    ┌──────────────────────────────┐
                    │  postgres (15-alpine)        │
                    │  volume: postgres_data       │
                    └──────────────────────────────┘
```

| 服务 | 容器名 | 端口映射 | 数据卷 |
|---|---|---|---|
| postgres | `edu_agent_postgres` | 5433 → 5432 | `postgres_data` |
| backend | `edu_agent_backend` | 8000 → 8000 | `checkpoints`（审核中断状态）、`./uploads`（原视频） |
| frontend | `edu_agent_frontend` | 3000 → 80 | — |

> 前端 axios 的 baseURL 是相对路径 `/api/v1`，同源部署下**不需要任何 CORS 配置**，请求由 Nginx 直接转发给后端。

---

## 四、常用运维命令

```bash
docker compose ps                      # 查看服务状态
docker compose logs -f backend         # 跟踪后端日志
docker compose restart backend         # 重启后端（改完 .env 后执行）
docker compose up -d --build backend   # 改完后端代码后重建
docker compose down                    # 停止并删除容器（数据卷保留）
docker compose down -v                 # 连数据卷一起删（清库重来）
```

进入容器排查：

```bash
docker compose exec backend bash
docker compose exec postgres psql -U eduagent_user -d eduagent
```

---

## 五、常见问题

**Q：`docker compose up` 报 `请设置 DB_PASSWORD`？**
A：`.env` 没建或没填。执行 `cp .env.example .env` 并填写。

**Q：后端日志出现 `migrations_failed`？**
A：多半是数据库还没就绪。compose 已配 `depends_on: service_healthy`，正常不会发生；若手动单独启动后端，请等 PG 健康后再起。

**Q：上传视频后 AI 审核报 `Connection error`？**
A：模型接口不通。三条轨分别依赖：
- 视觉轨 / 元数据轨 → `api.kimi.com`（Kimi 套餐 key）
- 聚合轨 / 区域轨 → `api.deepseek.com`
- 语音转写 → `dashscope.aliyuncs.com`

若部署在需要代理的网络里，给 backend 服务追加代理变量：

```yaml
# docker-compose.yml → backend → environment
HTTP_PROXY: http://<代理地址>:<端口>
HTTPS_PROXY: http://<代理地址>:<端口>
NO_PROXY: localhost,127.0.0.1,postgres
```

**Q：上传的原始视频存在哪？**
A：宿主机 `./uploads`（挂载到容器 `/app/uploads`）。可在详情页回放，接口支持 Range 拖动。

**Q：人工复核状态会丢吗？**
A：不会。LangGraph Checkpointer 落到 `checkpoints` 数据卷，容器重启后待审任务仍可继续处理。

**Q：想拆成「只跑后端 + 外接数据库」？**
A：删掉 compose 里的 `postgres` 服务，把 `backend.environment` 里的 `DB_HOST/DB_PORT` 改成外部库地址即可。

---

## 六、镜像内容说明

| 镜像 | 基础镜像 | 关键内容 |
|---|---|---|
| `eduagent-backend:latest` | `python:3.12-slim` | ffmpeg / curl / libpq5 + `requirements.txt` 全部依赖 + `backend/`、`scripts/` |
| `eduagent-frontend:latest` | `nginx:1.27-alpine` | Vue 打包产物 + 站点配置（SPA 回退 + `/api` 反代） |

构建上下文为**项目根目录**，`.dockerignore` 已排除 `.tools/`、`uploads/`、`node_modules/`、`dist/`、`.env*`，保证镜像干净且不泄露密钥。
