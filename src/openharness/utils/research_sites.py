"""Shared, configurable source catalogue for investment research web tools.

Catalogue membership describes source identity, never factual verification.
"""

from __future__ import annotations

from typing import Literal
from typing_extensions import TypedDict
from urllib.parse import urlsplit

from openharness.config.settings import ResearchSiteConfig, WebSettings, load_settings


Category = Literal["policy", "macro", "disclosure", "industry", "news"]
Tier = Literal["official", "professional", "media"]


class SourceClassification(TypedDict):
    url: str
    domain: str
    listed: bool
    name: str | None
    catalog_domain: str | None
    categories: list[Category] | None
    region: str | None
    tier: Tier | None


SOURCE_TIER_ORDER = {"official": 0, "professional": 1, "media": 2}
CATEGORY_LABELS = {
    "all": "综合",
    "policy": "政策",
    "macro": "宏观统计",
    "disclosure": "公司披露",
    "industry": "行业",
    "news": "财经资讯",
}
TIER_LABELS = {
    "official": "官方政策/统计/披露",
    "professional": "行业专业来源",
    "media": "财经媒体",
}

# Specific hostnames keep category selection from becoming a broad .gov search.
_BUILTIN_SITES: tuple[tuple[str, str, list[Category], str, Tier], ...] = (
    ("www.gov.cn", "中国政府网", ["policy"], "CN", "official"),
    ("ndrc.gov.cn", "国家发展改革委", ["policy", "industry"], "CN", "official"),
    ("mof.gov.cn", "财政部", ["policy", "macro"], "CN", "official"),
    ("pboc.gov.cn", "中国人民银行", ["policy", "macro"], "CN", "official"),
    ("stats.gov.cn", "国家统计局", ["macro"], "CN", "official"),
    ("safe.gov.cn", "国家外汇管理局", ["policy", "macro"], "CN", "official"),
    ("customs.gov.cn", "海关总署", ["macro"], "CN", "official"),
    ("csrc.gov.cn", "中国证监会", ["policy", "disclosure"], "CN", "official"),
    ("nfra.gov.cn", "国家金融监督管理总局", ["policy"], "CN", "official"),
    ("miit.gov.cn", "工业和信息化部", ["policy", "industry"], "CN", "official"),
    ("nea.gov.cn", "国家能源局", ["policy", "macro", "industry"], "CN", "official"),
    ("sse.com.cn", "上海证券交易所", ["policy", "disclosure"], "CN", "official"),
    ("szse.cn", "深圳证券交易所", ["policy", "disclosure"], "CN", "official"),
    ("bse.cn", "北京证券交易所", ["disclosure"], "CN", "official"),
    ("cninfo.com.cn", "巨潮资讯", ["disclosure"], "CN", "official"),
    ("hkexnews.hk", "披露易", ["disclosure"], "HK", "official"),
    ("sec.gov", "美国证券交易委员会", ["policy", "disclosure"], "US", "official"),
    ("federalreserve.gov", "美联储", ["policy", "macro"], "US", "official"),
    ("bls.gov", "美国劳工统计局", ["macro"], "US", "official"),
    ("bea.gov", "美国经济分析局", ["macro"], "US", "official"),
    ("ecb.europa.eu", "欧洲中央银行", ["policy", "macro"], "EU", "official"),
    ("imf.org", "国际货币基金组织", ["macro"], "Global", "official"),
    ("worldbank.org", "世界银行", ["macro"], "Global", "official"),
    ("bis.org", "国际清算银行", ["policy", "macro"], "Global", "official"),
    ("chinapv.org.cn", "中国光伏行业协会", ["industry"], "CN", "professional"),
    ("chinaisa.org.cn", "中国钢铁工业协会", ["industry"], "CN", "professional"),
    ("caam.org.cn", "中国汽车工业协会", ["industry"], "CN", "professional"),
    ("infolink-group.com", "InfoLink", ["industry"], "Global", "professional"),
    ("trendforce.com", "TrendForce", ["industry"], "Global", "professional"),
    ("reuters.com", "Reuters", ["news"], "Global", "media"),
    ("stcn.com", "证券时报", ["news"], "CN", "media"),
    ("cs.com.cn", "中国证券报", ["news"], "CN", "media"),
    ("cnstock.com", "上海证券报", ["news"], "CN", "media"),
    ("yicai.com", "第一财经", ["news"], "CN", "media"),
    ("caixin.com", "财新", ["news"], "CN", "media"),
)


def get_research_sites(settings: WebSettings | None = None) -> list[ResearchSiteConfig]:
    """Merge additions, overrides and disabled entries without changing defaults."""
    settings = settings if settings is not None else load_settings().web
    sites = {
        domain: ResearchSiteConfig(
            domain=domain, name=name, categories=categories, region=region, tier=tier
        )
        for domain, name, categories, region, tier in _BUILTIN_SITES
    }
    for override in settings.research_sites:
        original = sites.get(override.domain)
        data = (
            original.model_dump()
            if original
            else {
                "name": override.domain,
                "categories": ["industry"],
                "region": "CN",
                "tier": "professional",
            }
        )
        data.update(override.model_dump(exclude_none=True))
        sites[override.domain] = ResearchSiteConfig.model_validate(data)
    return [site for site in sites.values() if site.enabled]


def classify_source(url: str, sites: list[ResearchSiteConfig]) -> SourceClassification:
    """Match real hostname boundaries; prefer the most specific configured host."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
        valid = parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password
    except ValueError:
        host, valid = "", False
    matches = [
        site
        for site in sites
        if valid and (host == site.domain or host.endswith("." + site.domain))
    ]
    if not matches:
        return {
            "url": url,
            "domain": host,
            "listed": False,
            "name": None,
            "catalog_domain": None,
            "categories": [],
            "region": None,
            "tier": None,
        }
    site = max(matches, key=lambda item: len(item.domain))
    return {
        "url": url,
        "domain": host,
        "listed": True,
        "name": site.name,
        "catalog_domain": site.domain,
        "categories": site.categories,
        "region": site.region,
        "tier": site.tier,
    }


def source_identity(url: str, sites: list[ResearchSiteConfig] | None = None) -> str:
    """Return a conservative publisher identity for source-independence checks."""
    source = classify_source(url, sites if sites is not None else get_research_sites())
    if source["catalog_domain"]:
        return source["catalog_domain"]
    host = source["domain"]
    if not host:
        return url
    labels = host.split(".")
    compound_suffixes = {
        "com.cn",
        "org.cn",
        "net.cn",
        "gov.cn",
        "com.hk",
        "co.uk",
        "com.au",
        "co.jp",
    }
    suffix = ".".join(labels[-2:])
    width = 3 if suffix in compound_suffixes else 2
    return ".".join(labels[-width:]) if len(labels) >= width else host


def describe_source(source: SourceClassification) -> str:
    if not source["listed"]:
        return "目录外来源（身份及事实需另行核验）"
    categories = "/".join(CATEGORY_LABELS[item] for item in source["categories"] or [])
    return (
        f"目录内来源: {source['name']} | {categories} | {source['region']} | "
        f"{TIER_LABELS[source['tier'] or 'professional']}（目录收录不代表事实已核验）"
    )
