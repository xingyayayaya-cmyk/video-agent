# =============================================================================
# backend/core/video_processor.py
# -----------------------------------------------------------------------------
# 视频处理核心模块：ffmpeg 抽帧 + 音频分离 + 云端 ASR 转文字
#
# 职责：
#   1. extract_frames()  —— 用 ffmpeg 从视频中按固定间隔抽取关键帧
#   2. extract_audio()   —— 用 ffmpeg 从视频中分离音轨为临时 wav 文件
#   3. transcribe_audio()—— 调用云端 ASR API 将音频转文字
#   4. get_video_info()   —— 用 ffprobe 获取视频时长、分辨率等元信息
#
# 设计要点：
#   - ffmpeg/ffprobe 命令在默认线程池中同步执行，避免阻塞事件循环
#   - 抽帧数量受 config.video_max_frames 控制，控制 Vision API 调用成本
#   - ASR 支持多语言，通过 config.asr_language 配置
#   - 每一步都有降级：ffmpeg 失败返回空列表，ASR 失败返回空字符串
#     保证审核 Agent 仍能运行（只是降级到仅元数据审核）
# =============================================================================

import asyncio
import base64
import json
import os
import shutil
import tempfile
from typing import Optional

import httpx

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


# ──────────────────────────────────────────────────────────────────────────────
# 视频元信息
# ──────────────────────────────────────────────────────────────────────────────

async def get_video_info(video_path: str) -> dict:
    """用 ffprobe 获取视频元信息（时长、分辨率、帧率等）。

    返回：
        {
            "duration": float,       # 秒
            "width": int,
            "height": int,
            "fps": float,
            "codec": str,            # 如 "h264"
            "has_audio": bool,
        }
    失败时返回全零值，不抛异常。
    """
    cmd = [
        settings.ffprobe_path,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format", "-show_streams",
        video_path,
    ]

    try:
        # asyncio.create_subprocess_exec 本身就是协程，直接 await 即可。
        # 早期实现误用 run_in_executor 包裹 lambda，导致协程从未被 await，
        # 子进程根本没有启动（旧日志表现为 'coroutine' object has no attribute 'communicate'）。
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning("video_probe_failed", error=stderr.decode(errors="replace"))
            return {"duration": 0, "width": 0, "height": 0, "fps": 0, "codec": "", "has_audio": False}

        data = json.loads(stdout.decode())

        format_info = data.get("format", {})
        streams = data.get("streams", [])

        video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        duration = float(format_info.get("duration", 0))
        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
        fps_str = video_stream.get("r_frame_rate", "0/1")
        # 解析 "30000/1001" 格式的帧率
        try:
            num, den = fps_str.split("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0

        return {
            "duration": duration,
            "width": width,
            "height": height,
            "fps": round(fps, 2),
            "codec": video_stream.get("codec_name", ""),
            "has_audio": audio_stream is not None,
        }

    except Exception as e:
        logger.error("video_probe_error", error=str(e), path=video_path)
        return {"duration": 0, "width": 0, "height": 0, "fps": 0, "codec": "", "has_audio": False}


# ──────────────────────────────────────────────────────────────────────────────
# 抽帧（固定间隔）
# ──────────────────────────────────────────────────────────────────────────────

async def extract_frames(
    video_path: str,
    interval: Optional[float] = None,
    max_frames: Optional[int] = None,
) -> list[dict]:
    """用 ffmpeg 按固定间隔从视频中抽取关键帧。

    参数：
        video_path: 视频文件路径
        interval:   抽帧间隔（秒），None 则用 config.video_frame_interval
        max_frames: 最大抽帧数，None 则用 config.video_max_frames

    返回：
        [
            {"frame_path": "/tmp/xxx/frame_001.jpg", "timestamp": 0.0},
            {"frame_path": "/tmp/xxx/frame_002.jpg", "timestamp": 5.0},
            ...
        ]
    失败时返回空列表。
    """
    if interval is None:
        interval = settings.video_frame_interval
    if max_frames is None:
        max_frames = settings.video_max_frames

    # 先获取视频时长，计算实际抽帧间隔
    info = await get_video_info(video_path)
    duration = info.get("duration", 0)

    if duration <= 0:
        logger.warning("extract_frames.no_duration", path=video_path)
        return []

    # 计算抽帧数：duration / interval，但不超过 max_frames
    import math
    num_frames = min(math.ceil(duration / interval), max_frames)

    if num_frames <= 0:
        num_frames = 1

    # 如果抽帧数 < 预估（即视频很短），重新计算间隔
    actual_interval = duration / num_frames if num_frames > 0 else interval

    # 创建临时目录存放帧
    frame_dir = tempfile.mkdtemp(prefix="video_frames_")

    cmd = [
        settings.ffmpeg_path,
        "-i", video_path,
        "-vf", f"fps=1/{actual_interval:.2f}",
        "-frames:v", str(num_frames),
        "-q:v", "2",                      # JPEG 质量（2=高质量）
        f"{frame_dir}/frame_%03d.jpg",
        "-y",                             # 覆盖已存在文件
    ]

    try:
        # asyncio.create_subprocess_exec 本身就是协程，直接 await 即可。
        # 早期实现误用 run_in_executor 包裹 lambda，导致协程从未被 await，
        # 子进程根本没有启动（旧日志表现为 'coroutine' object has no attribute 'communicate'）。
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning(
                "extract_frames.ffmpeg_failed",
                error=stderr.decode(errors="replace")[:500],
                path=video_path,
            )
            # 即使 ffmpeg 返回非零，有时仍会输出部分帧
            # 继续检查目录

        # 收集生成的帧文件
        frames = []
        for i in range(1, num_frames + 1):
            frame_path = os.path.join(frame_dir, f"frame_{i:03d}.jpg")
            if os.path.exists(frame_path):
                timestamp = round((i - 1) * actual_interval, 2)
                frames.append({
                    "frame_path": frame_path,
                    "timestamp": timestamp,
                })

        logger.info(
            "extract_frames.done",
            path=video_path,
            duration=duration,
            frames_extracted=len(frames),
            actual_interval=round(actual_interval, 2),
        )
        return frames

    except Exception as e:
        logger.error("extract_frames.error", error=str(e), path=video_path)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# 抽帧（场景切换 + 保底密度，防闪帧规避）
# ──────────────────────────────────────────────────────────────────────────────

async def _detect_scene_changes(video_path: str, threshold: float) -> list[float]:
    """用 ffmpeg scene 滤镜检测场景切换时间点。

    原理：
        select='gt(scene,TH)' 只保留场景相似度低于阈值的帧（即画面突变处），
        showinfo 把每帧 pts_time 打到 stderr —— 违规画面哪怕只出现 0.5s
        （固定间隔抽帧会漏掉的「闪帧规避」手法）也能被捕获。

    返回：
        场景切换时间戳列表（秒）；失败返回空列表（调用方降级为纯间隔抽帧）。
    """
    cmd = [
        settings.ffmpeg_path,
        "-i", video_path,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-f", "null",
        "-",
    ]

    try:
        # asyncio.create_subprocess_exec 本身就是协程，直接 await 即可。
        # 早期实现误用 run_in_executor 包裹 lambda，导致协程从未被 await，
        # 子进程根本没有启动（旧日志表现为 'coroutine' object has no attribute 'communicate'）。
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        timestamps: list[float] = []
        for line in stderr.decode(errors="replace").splitlines():
            # showinfo 行格式：... n:  12 pts: ... pts_time:34.56 ...
            if "pts_time:" not in line:
                continue
            for token in line.split():
                if token.startswith("pts_time:"):
                    try:
                        ts = float(token.split(":", 1)[1])
                        if ts > 0:
                            timestamps.append(ts)
                    except ValueError:
                        continue
        return timestamps

    except Exception as e:
        logger.warning("scene_detect.failed", error=str(e), path=video_path)
        return []


async def _extract_frame_at(
    video_path: str,
    timestamp: float,
    output_path: str,
) -> bool:
    """抽取指定时间点的单帧（-ss 快速定位）。"""
    cmd = [
        settings.ffmpeg_path,
        "-ss", f"{timestamp:.2f}",
        "-i", video_path,
        "-frames:v", "1",
        "-q:v", "2",
        output_path,
        "-y",
    ]
    try:
        # asyncio.create_subprocess_exec 本身就是协程，直接 await 即可。
        # 早期实现误用 run_in_executor 包裹 lambda，导致协程从未被 await，
        # 子进程根本没有启动（旧日志表现为 'coroutine' object has no attribute 'communicate'）。
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0 and os.path.exists(output_path)
    except Exception as e:
        logger.warning("extract_frame_at.failed", error=str(e), ts=timestamp)
        return False


async def extract_keyframes(
    video_path: str,
    interval: Optional[float] = None,
    max_frames: Optional[int] = None,
    scene_threshold: Optional[float] = None,
) -> list[dict]:
    """场景切换 + 保底间隔的混合抽帧（推荐入口，防闪帧规避）。

    策略（对齐需求文档 §4.1）：
        1. 场景切换检测抽帧：每个画面突变处必抽（闪帧规避的主要防线）
        2. 保底密度抽帧：每隔 interval 秒抽 1 帧（长镜头里的渐进违规靠它）
        3. 候选点合并后按最小间隔去重（防止场景密集处挤占配额）
        4. 总量封顶 max_frames（控制 Vision API 成本）

    失败降级：场景检测失败时退化为纯固定间隔抽帧（extract_frames）。
    """
    if interval is None:
        interval = settings.video_frame_interval
    if max_frames is None:
        max_frames = settings.video_max_frames
    if scene_threshold is None:
        scene_threshold = settings.video_scene_threshold

    info = await get_video_info(video_path)
    duration = info.get("duration", 0)
    if duration <= 0:
        logger.warning("extract_keyframes.no_duration", path=video_path)
        return await extract_frames(video_path, interval=interval, max_frames=max_frames)

    # ── 1. 候选时间点：场景切换 + 保底间隔 ──
    candidates: list[float] = [0.0]  # 首帧必抽（封面画面常藏违规）
    candidates.extend(round(t, 2) for t in await _detect_scene_changes(video_path, scene_threshold))
    t = interval
    while t < duration:
        candidates.append(round(t, 2))
        t += interval

    # ── 2. 排序 + 最小间隔去重（min_gap 保证配额被均匀利用）──
    candidates = sorted(set(candidates))
    min_gap = duration / max_frames
    selected: list[float] = []
    for ts in candidates:
        if len(selected) >= max_frames:
            break
        if not selected or (ts - selected[-1]) >= min_gap * 0.5:
            selected.append(ts)

    # ── 3. 若候选不足配额，按纯间隔补齐 ──
    if len(selected) < min(max_frames, int(duration / interval) + 1):
        t = 0.0
        while t < duration and len(selected) < max_frames:
            if all(abs(t - s) >= min_gap * 0.5 for s in selected):
                selected.append(round(t, 2))
            t += interval
    selected.sort()

    # ── 4. 逐时间点抽帧 ──
    frame_dir = tempfile.mkdtemp(prefix="video_frames_")
    frames: list[dict] = []
    for i, ts in enumerate(selected[:max_frames], start=1):
        frame_path = os.path.join(frame_dir, f"frame_{i:03d}.jpg")
        if await _extract_frame_at(video_path, ts, frame_path):
            frames.append({"frame_path": frame_path, "timestamp": ts})
        elif frames:
            # 单点失败用前一帧时间戳重试一次（关键帧定位偶尔偏差）
            alt_path = os.path.join(frame_dir, f"frame_{i:03d}.jpg")
            if await _extract_frame_at(video_path, max(ts - 0.5, 0), alt_path):
                frames.append({"frame_path": alt_path, "timestamp": round(max(ts - 0.5, 0), 2)})

    logger.info(
        "extract_keyframes.done",
        path=video_path,
        duration=round(duration, 2),
        candidates=len(candidates),
        frames_extracted=len(frames),
    )
    return frames


# ──────────────────────────────────────────────────────────────────────────────
# 海报图片处理（poster 内容类型：单帧，无音频）
# ──────────────────────────────────────────────────────────────────────────────

_POSTER_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def is_poster_image(path: str) -> bool:
    """判断文件是否为静态海报图。"""
    return path.lower().endswith(_POSTER_EXTENSIONS)


async def prepare_image_frames(image_path: str) -> list[dict]:
    """把海报图片包装成「单帧」结构，复用视觉轨审核流水线。

    复制到独立临时目录（帧清理逻辑按目录删除，不能直接用上传文件原路径）。
    """
    frame_dir = tempfile.mkdtemp(prefix="video_frames_")
    frame_path = os.path.join(frame_dir, "frame_001.jpg")
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: shutil.copyfile(image_path, frame_path))
        return [{"frame_path": frame_path, "timestamp": 0.0}]
    except Exception as e:
        logger.error("prepare_image_frames.failed", error=str(e), path=image_path)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# 音频分离
# ──────────────────────────────────────────────────────────────────────────────

async def extract_audio(video_path: str) -> Optional[str]:
    """从视频中分离音轨为 wav 临时文件。

    返回：
        成功 → wav 文件临时路径
        失败 → None
    """
    # 先检查视频是否有音轨
    info = await get_video_info(video_path)
    if not info.get("has_audio", False):
        logger.info("extract_audio.no_audio_track", path=video_path)
        return None

    audio_path = tempfile.mktemp(suffix=".wav", prefix="video_audio_")

    cmd = [
        settings.ffmpeg_path,
        "-i", video_path,
        "-vn",                    # 不要视频
        "-acodec", "pcm_s16le",   # PCM 16-bit
        "-ar", "16000",           # 16kHz 采样率（ASR 常用）
        "-ac", "1",               # 单声道
        audio_path,
        "-y",
    ]

    try:
        # asyncio.create_subprocess_exec 本身就是协程，直接 await 即可。
        # 早期实现误用 run_in_executor 包裹 lambda，导致协程从未被 await，
        # 子进程根本没有启动（旧日志表现为 'coroutine' object has no attribute 'communicate'）。
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.warning(
                "extract_audio.ffmpeg_failed",
                error=stderr.decode(errors="replace")[:500],
                path=video_path,
            )
            return None

        if not os.path.exists(audio_path):
            return None

        logger.info("extract_audio.done", path=video_path, audio_path=audio_path)
        return audio_path

    except Exception as e:
        logger.error("extract_audio.error", error=str(e), path=video_path)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 云端 ASR 转文字
# ──────────────────────────────────────────────────────────────────────────────

async def transcribe_audio(audio_path: str) -> str:
    """调用云端 ASR API 将音频文件转文字。

    当前实现：
      - openai   → OpenAI Whisper API（multipart /audio/transcriptions）
      - dashscope→ 阿里百炼 qwen3-omni 全模态模型（chat/completions + input_audio，
                   2026-10-11 实测 paraformer 在该网关 404，omni 可用且支持中文语音）
    可扩展：Azure Speech / Google Cloud Speech-to-Text

    返回：
        成功 → 转录文本
        失败 → 空字符串
    """
    if not os.path.exists(audio_path):
        logger.warning("transcribe.file_not_found", path=audio_path)
        return ""

    provider = settings.asr_provider

    try:
        if provider == "openai":
            return await _transcribe_openai(audio_path)
        elif provider == "dashscope":
            return await _transcribe_dashscope(audio_path)
        elif provider == "azure":
            return await _transcribe_azure(audio_path)
        else:
            logger.warning("transcribe.unknown_provider", provider=provider)
            return ""
    except Exception as e:
        logger.error("transcribe.error", error=str(e), provider=provider)
        return ""


async def _transcribe_dashscope(audio_path: str) -> str:
    """阿里百炼 qwen3-omni 全模态转录。

    百炼 compatible-mode 的 /audio/transcriptions 在该工作空间网关 404，
    所以走 chat/completions + input_audio 消息（OpenAI 音频消息规范）。
    国内直连，trust_env=False 不依赖代理。
    """
    with open(audio_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    url = f"{settings.asr_base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {settings.asr_api_key}"}
    payload = {
        "model": settings.asr_model,
        "messages": [{"role": "user", "content": [
            {"type": "input_audio",
             "input_audio": {"data": f"data:audio/wav;base64,{b64}", "format": "wav"}},
            {"type": "text",
             "text": "请转写出这段音频中的完整语音文字，保持原语言。若音频中没有清晰人声，只回复 NO_SPEECH。"},
        ]}],
    }

    async with httpx.AsyncClient(trust_env=False, timeout=300.0) as client:
        resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code != 200:
        logger.warning(
            "transcribe.dashscope_failed",
            status=resp.status_code, body=resp.text[:200],
        )
        return ""

    text = (resp.json()["choices"][0]["message"].get("content") or "").strip()
    if not text or text.upper().startswith("NO_SPEECH"):
        return ""
    return text


async def _transcribe_openai(audio_path: str) -> str:
    """OpenAI Whisper API 转录。"""
    with open(audio_path, "rb") as audio_file:
        audio_data = audio_file.read()

    url = f"{settings.asr_base_url}/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {settings.asr_api_key}",
    }

    files = {
        "file": ("audio.wav", audio_data, "audio/wav"),
    }
    data = {
        "model": settings.asr_model,
        "language": settings.asr_language,
        "response_format": "text",
    }

    async with httpx.AsyncClient(trust_env=True, timeout=120.0) as client:
        resp = await client.post(url, headers=headers, files=files, data=data)

    if resp.status_code != 200:
        logger.warning(
            "transcribe.openai_failed",
            status=resp.status_code,
            body=resp.text[:500],
        )
        return ""

    return resp.text.strip()


async def _transcribe_azure(audio_path: str) -> str:
    """Azure Speech to Text 转录（预留接口，后续按需实现）。"""
    # TODO: Azure Speech API 实现
    logger.warning("transcribe.azure_not_implemented")
    return ""


# ──────────────────────────────────────────────────────────────────────────────
# 清理临时文件
# ──────────────────────────────────────────────────────────────────────────────

def cleanup_frames(frames: list[dict]) -> None:
    """清理抽帧产生的临时文件和目录。"""
    if not frames:
        return

    frame_dir = os.path.dirname(frames[0].get("frame_path", ""))
    for f in frames:
        path = f.get("frame_path", "")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    # 清理空目录
    if frame_dir and os.path.isdir(frame_dir):
        try:
            os.rmdir(frame_dir)
        except OSError:
            pass


def cleanup_audio(audio_path: Optional[str]) -> None:
    """清理音频临时文件。"""
    if audio_path and os.path.exists(audio_path):
        try:
            os.remove(audio_path)
        except OSError:
            pass


def cleanup_frame_path(frame_path: str) -> None:
    """清理单个帧文件。"""
    if frame_path and os.path.exists(frame_path):
        try:
            os.remove(frame_path)
        except OSError:
            pass
