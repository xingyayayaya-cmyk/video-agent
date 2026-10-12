# =============================================================================
# backend/core/rulepacks.py
# -----------------------------------------------------------------------------
# 出海区域合规规则包 + 内容类型审核策略配置
#
# 设计目标（对齐文档 §3）：
#   审核规则必须「按目标市场 × 内容类型」二维配置化，不能写死在 prompt 里。
#   新增区域或调整规则只需改本文件，无需动流水线代码。
#
# 两个维度：
#   1. REGION_RULEPACKS   —— 区域维度：US / EU / LATAM / MENA / SEA 的合规规则
#      每个区域含三份规则：
#        content_rules      正片通用规则（法律/文化/宗教）
#        advertising_rules  投流切片附加规则（TikTok / Meta / Google Ads 广告政策）
#        poster_rules       封面海报附加规则
#   2. CONTENT_TYPE_PROFILES —— 内容类型维度：episode / clip / poster
#      定义该类型的审核严格度说明与附加 prompt 片段
#
# 用法：
#   from backend.core.rulepacks import build_review_context
#   ctx = build_review_context("US", "clip")   # → 注入三轨 + 聚合 prompt 的文本
# =============================================================================

from __future__ import annotations

# ──────────────────────────────────────────────────────────────────────────────
# 一、区域规则包
# ──────────────────────────────────────────────────────────────────────────────

REGION_RULEPACKS: dict[str, dict[str, str]] = {
    "US": {
        "name": "US / North America",
        "content_rules": """
US Region Guidelines:
- COPPA compliance: content targeting children under 13 requires special handling
- FTC guidelines: no deceptive advertising, clear disclosure of sponsored content
- Copyright: DMCA compliance, fair use limitations
- Content rating: TV-MA equivalent content requires age-gating
- Hate speech: protected classes (race, religion, gender, sexual orientation, disability)
- Violence: no realistic depictions of self-harm or instructions for violence
""",
        "advertising_rules": """
US Advertising Platform Rules (TikTok / Meta / Google Ads):
- No implied nudity or sexual suggestiveness even if the underlying show is TV-MA rated
- No "before/after" claims, no unrealistic outcome promises (e.g. "earn $1000 a day")
- No shock tactics: jump-scares, distressing imagery, "you won't believe" bait
- Clear disclosure of AI-generated actors if depicting realistic persons
- No targeting implications based on health conditions, race, or financial status
""",
        "poster_rules": """
US Poster Rules:
- No implied nudity (strategically covered poses are treated as suggestive)
- No violent or horror imagery as clickbait
- Title text must match the actual show content (no misleading clickbait titles)
""",
    },
    "EU": {
        "name": "European Union",
        "content_rules": """
EU Region Guidelines:
- GDPR compliance: no personal data exposure without consent
- Digital Services Act (DSA): no illegal content, transparent algorithms
- AI Act: AI-generated content must be labeled as such
- Hate speech laws: stricter than US in many EU countries (Germany, France)
- Copyright: EU Copyright Directive, no unlicensed sharing
- Minors: no content harmful to minors per EU Audiovisual Media Services Directive
- Religious sensitivity: blasphemy laws in some EU countries
""",
        "advertising_rules": """
EU Advertising Platform Rules:
- GDPR: no personal data in ad creatives; no implied profiling claims
- DSA: ads must be clearly labeled as advertising
- No gender stereotyping (EU gender-neutral advertising norms)
- AI Act: synthetic/AI-generated people must be disclosed in ads
- No dark patterns: countdown timers, fake urgency claims
""",
        "poster_rules": """
EU Poster Rules:
- AI-generated people must be labeled as AI-generated
- No body-image shaming or unhealthy beauty standard implications
""",
    },
    "LATAM": {
        "name": "Latin America",
        "content_rules": """
LATAM (Latin America) Region Guidelines:
- Cultural sensitivity: respect indigenous cultures, avoid stereotypes
- Religious content: predominantly Catholic region, be sensitive to religious imagery
- Language: Spanish/Portuguese, avoid offensive regional slang
- Violence: strict laws in Brazil and Mexico regarding violent content
- Gambling: regulated differently across LATAM countries
- Advertising: ANATEL (Brazil) and IFETEL (Mexico) regulations for telecom ads
""",
        "advertising_rules": """
LATAM Advertising Platform Rules:
- WhatsApp is the dominant channel: creatives must be legible on small screens
- No content exploiting economic insecurity ("get rich quick" is heavily restricted)
- Telenovela-style dramatic imagery is acceptable; explicit content is not
""",
        "poster_rules": """
LATAM Poster Rules:
- Romantic/relationship drama imagery performs well and is acceptable within limits
- Avoid religious iconography in romance posters (Catholic sensitivity)
""",
    },
    "MENA": {
        "name": "Middle East & North Africa",
        "content_rules": """
MENA (Middle East & North Africa) Region Guidelines:
- Religious sensitivity: highest sensitivity to Islamic content, avoid mocking religion
- Modesty standards: stricter dress code requirements, avoid suggestive content
- Political content: avoid content critical of local governments/leaders
- Alcohol/drugs: depiction of alcohol consumption may violate local laws
- LGBTQ+ content: may be illegal in many MENA countries, high caution required
- Language: Arabic, respect dialectal differences
- Cultural norms: respect family values, avoid content contradicting local customs
""",
        "advertising_rules": """
MENA Advertising Platform Rules:
- Ramadan: heightened sensitivity during Ramadan; avoid food/drink temptation framing
- Modesty: no revealing clothing even in drama clips; no physical intimacy scenes
- Music: some regions restrict music in ads; instrumental-only is safer
- Gender mixing: depictions may need to follow local norms
""",
        "poster_rules": """
MENA Poster Rules:
- Strictest poster standards: no exposed skin beyond face/hands/forearms
- No romantic physical contact depicted on posters
""",
    },
    "SEA": {
        "name": "Southeast Asia",
        "content_rules": """
SEA (Southeast Asia) Region Guidelines:
- Religious diversity: Buddhism, Islam, Christianity, Hinduism all present
- Muslim-majority countries (Indonesia, Malaysia): Islamic content guidelines apply
- Political sensitivity: Thailand (lèse-majesté), Singapore (Pofma), Vietnam
- Language: English, Bahasa Indonesia, Thai, Vietnamese, Tagalog
- Cultural norms: respect for elders, family values
- Gambling: heavily regulated or illegal in most SEA countries
- Content rating: varies by country, Indonesia has strict content filtering
""",
        "advertising_rules": """
SEA Advertising Platform Rules:
- Indonesia & Malaysia: Islamic advertising standards apply to all creatives
- Thailand: absolutely no royal family references
- Singapore: no claims that could fall under POFMA (fake news law)
- Price-sensitive framing is fine; gambling/finance claims are restricted
""",
        "poster_rules": """
SEA Poster Rules:
- Indonesia: posters are subject to content filtering (Kominfo); avoid suggestive imagery
- Horror/ghost themes are culturally popular but must not include religious blasphemy
""",
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# 二、内容类型审核策略
# ──────────────────────────────────────────────────────────────────────────────

CONTENT_TYPE_PROFILES: dict[str, dict[str, str]] = {
    "episode": {
        "label": "正片短剧 (Full Episode)",
        "description": (
            "This is a full episode of an AI-generated vertical short drama. "
            "Review it against all six dimensions. Fictional dramatic context matters: "
            "violence/romance within an obvious fictional storyline is evaluated as fiction, "
            "but real-person harm, instructions for illegal acts, and child safety issues "
            "are violations regardless of fictional framing."
        ),
        "extra_rules": "",
    },
    "clip": {
        "label": "投流切片 (Ad Clip)",
        "description": (
            "This is a paid advertisement clip (a short excerpt cut from the show for "
            "distribution on TikTok / Meta / YouTube Ads). Apply STRICTER standards than "
            "the full episode: ad platforms reject creatives that the underlying show "
            "would tolerate. Any borderline content that would normally be pending_review "
            "should be treated as a violation for ad clips."
        ),
        "extra_rules": (
            "AD-CLIP SPECIFIC RULES (strict mode):\n"
            "- Zero tolerance for suggestive content: ad platforms auto-reject\n"
            "- Zero tolerance for misleading hooks: claims in the clip must be literally true of the show\n"
            "- No engagement baiting: 'follow', 'like', 'comment', 'share' calls-to-action are violations\n"
            "- No fake UI elements: fabricated app store ratings or 'trending' badges\n"
            "- No health/financial claims: weight loss, income promises, medical cures\n"
            "- If the clip is a cut of a borderline scene, judge the CLIP as it stands, not the show"
        ),
    },
    "poster": {
        "label": "封面海报 (Poster)",
        "description": (
            "This is a static cover image / poster for the show. Review the image itself: "
            "implied nudity/suggestiveness, violent or disturbing imagery, misleading "
            "clickbait, text on the poster (read all text carefully), and regional "
            "cultural/religious standards. There is no audio or video to review."
        ),
        "extra_rules": (
            "POSTER SPECIFIC RULES:\n"
            "- Text on the poster is part of the review: read titles/taglines as metadata\n"
            "- Composite layouts (couple + title + badges) are common: check each element\n"
            "- AI-generation artifacts on faces/hands count as platform_guideline low-risk issues"
        ),
    },
}


# ──────────────────────────────────────────────────────────────────────────────
# 三、规则包查询接口
# ──────────────────────────────────────────────────────────────────────────────

def get_rulepack(region: str) -> dict[str, str]:
    """获取区域规则包，未知区域回落到 US。"""
    return REGION_RULEPACKS.get(region.upper(), REGION_RULEPACKS["US"])


def get_content_profile(content_type: str) -> dict[str, str]:
    """获取内容类型审核策略，未知类型按正片处理。"""
    return CONTENT_TYPE_PROFILES.get(content_type.lower(), CONTENT_TYPE_PROFILES["episode"])


def build_review_context(region: str, content_type: str) -> str:
    """构建注入三轨 / 聚合 prompt 的完整规则上下文。

    输出结构：
        [Content Profile]  内容类型说明 + 严格度
        [Regional Rules]   区域正片规则
        [Ad Rules]         投流切片的广告平台规则（仅 clip）
        [Poster Rules]     封面规则（仅 poster）
        [Type Extra Rules] 内容类型附加硬性规则
    """
    pack = get_rulepack(region)
    profile = get_content_profile(content_type)

    parts = [
        f"[Content Profile] {profile['label']}\n{profile['description']}",
        f"[Regional Rules — {region.upper()}]\n{pack['content_rules'].strip()}",
    ]

    ct = content_type.lower()
    if ct == "clip":
        parts.append(f"[Advertising Platform Rules — {region.upper()}]\n{pack['advertising_rules'].strip()}")
    elif ct == "poster":
        parts.append(f"[Poster Rules — {region.upper()}]\n{pack['poster_rules'].strip()}")

    if profile.get("extra_rules"):
        parts.append(profile["extra_rules"])

    return "\n\n".join(parts)


def list_regions() -> list[str]:
    """返回全部已配置区域代码。"""
    return list(REGION_RULEPACKS.keys())


def list_content_types() -> list[dict[str, str]]:
    """返回全部内容类型（供前端下拉框使用）。"""
    return [
        {"value": key, "label": profile["label"]}
        for key, profile in CONTENT_TYPE_PROFILES.items()
    ]
