# backend/api/router.py
# API 路由总入口
#
# 当前仅保留出海 AI 短视频审核平台所需的两组路由：
#   /auth  —— 登录鉴权（creator / reviewer / admin 角色）
#   /video —— 视频审核（上传、复核、看板、申诉）
# 原教育场景路由（qa / exam / resume / interview / chat）已随模块一并移除。

import importlib
import sys

from fastapi import APIRouter

api_router = APIRouter()                              # 总路由

# 按模块单独挂载，便于定位导入失败的具体模块。
_SUB_ROUTERS = (
    ("auth",  "/auth",  ["认证"],     True),
    ("video", "/video", ["视频审核"], True),
)

for _name, _prefix, _tags, _required in _SUB_ROUTERS:
    try:
        _mod = importlib.import_module(f"backend.api.v1.{_name}")
        api_router.include_router(_mod.router, prefix=_prefix, tags=_tags)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[router] 跳过 {_prefix}（backend.api.v1.{_name} 导入失败: {exc}）",
            file=sys.stderr,
        )
        if _required:
            raise
