# backend/agents/video_review/graph.py
#
# 视频审核 Agent 状态图（9 节点线性链 + 节点 7 中断恢复）
#
# START
#   → [1] extract_video_assets      ffmpeg 抽帧 + 音频分离 + ASR 转文字
#   → [2] load_video_meta           加载 DB 视频元数据（标题/描述/标签）
#   → [3] run_three_tracks          三轨并行审核（视觉 GPT-4o / 音频 GPT-4o / 元数据 GPT-4o）
#   → [4] aggregate_assessment      汇总三轨，计算六维度风险评分与自动决策
#   → [5] analyze_risk              六维度风险详细分析
#   → [6] notify_reviewer           更新提交状态（auto_approved/auto_rejected/pending_review）
#   → [7] reviewer_review           interrupt 暂停（仅 pending_review），等待审核员决策
#   → [8] apply_reviewer_decision   合并审核员修改，产出 final_results
#   → [9] publish_results           写入 DB 并发布审核结果
# END

from langgraph.graph import StateGraph, START, END

from backend.agents.video_review.state import VideoReviewState
from backend.agents.video_review.nodes import (
    extract_video_assets_node,
    load_video_meta_node,
    run_three_tracks_node,
    aggregate_assessment_node,
    analyze_risk_node,
    notify_reviewer_node,
    reviewer_review_node,
    apply_reviewer_decision_node,
    publish_results_node,
)
from backend.core.memory import get_memory_saver


def build_video_review_graph():
    """
    构建并编译视频审核 Agent 的 LangGraph 状态图。

    核心步骤（按执行顺序）：

    阶段一 · 视频预处理
        1. extract_video_assets
           - ffmpeg 抽帧（每5秒1帧，最多30帧）
           - 音频分离为 wav
           - 云端 ASR 转文字（OpenAI Whisper）
           - 失败降级：各步骤独立降级，不中断整图

        2. load_video_meta
           - 从 DB videos 表加载标题/描述/标签
           - 合并到 video_meta 字典

    阶段二 · AI 审核（三轨并行）
        3. run_three_tracks（asyncio.gather 三轨并行）
           - 视觉轨 → GPT-4o Vision 逐帧审核
           - 音频轨 → GPT-4o 文本审核（转录文本）
           - 元数据轨 → GPT-4o 文本审核（标题/描述/标签）

        4. aggregate_assessment
           - 汇总三轨 → 六维度风险评分
           - 加权总分 → 自动决策（auto_approved/auto_rejected/pending_review）

    阶段三 · 风险分析
        5. analyze_risk
           - 六维度详细分析（LLM 或规则降级）
           - 区域合规标记

    阶段四 · Human-in-the-Loop
        6. notify_reviewer
           - auto_approved → 状态改为 auto_approved
           - auto_rejected → 状态改为 auto_rejected
           - pending_review → 状态改为 pending_review

        7. reviewer_review（interrupt + MemorySaver）
           - auto_approved/auto_rejected → 跳过 interrupt
           - pending_review → interrupt() 暂停，等待审核员 POST /confirm

        8. apply_reviewer_decision
           - approve → 沿用 AI 评分，标记通过
           - modify → 按维度覆盖评分/决策
           - reject → 标记拒绝

    阶段五 · 发布结果
        9. publish_results
           - 事务写入 video_reviews（先删后插，幂等）
           - 更新 video_submissions → published_approved/published_rejected
           - 清理临时帧文件
           - 返回 structured_output 供 API / 前端展示
    """
    builder = StateGraph(VideoReviewState)

    # ── 注册节点 ──────────────────────────────────────────────
    builder.add_node("extract_video_assets",   extract_video_assets_node)    # 节点1：抽帧+ASR
    builder.add_node("load_video_meta",        load_video_meta_node)          # 节点2：加载元数据
    builder.add_node("run_three_tracks",       run_three_tracks_node)         # 节点3：三轨并行审核
    builder.add_node("aggregate_assessment",   aggregate_assessment_node)     # 节点4：汇总风险评分
    builder.add_node("analyze_risk",           analyze_risk_node)             # 节点5：六维度风险分析
    builder.add_node("notify_reviewer",        notify_reviewer_node)          # 节点6：通知审核员
    builder.add_node("reviewer_review",        reviewer_review_node)          # 节点7：审核员确认（中断点）
    builder.add_node("apply_reviewer_decision", apply_reviewer_decision_node) # 节点8：合并审核员修改
    builder.add_node("publish_results",        publish_results_node)          # 节点9：发布最终结果

    # ── 固定边（线性链）──────────────────────────────────────
    builder.add_edge(START,                      "extract_video_assets")
    builder.add_edge("extract_video_assets",     "load_video_meta")
    builder.add_edge("load_video_meta",          "run_three_tracks")
    builder.add_edge("run_three_tracks",         "aggregate_assessment")
    builder.add_edge("aggregate_assessment",     "analyze_risk")
    builder.add_edge("analyze_risk",             "notify_reviewer")
    builder.add_edge("notify_reviewer",          "reviewer_review")
    builder.add_edge("reviewer_review",          "apply_reviewer_decision")
    builder.add_edge("apply_reviewer_decision",  "publish_results")
    builder.add_edge("publish_results",          END)

    # ── 编译，绑定 MemorySaver（支持节点7 interrupt 恢复）────
    checkpointer = get_memory_saver("video_review")
    return builder.compile(checkpointer=checkpointer)
