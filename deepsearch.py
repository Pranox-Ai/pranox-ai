"""
Pranox Deep Research — v2.0 Production Engine
==============================================
Agentic pipeline that mirrors how Perplexity & Gemini Deep Research work:

  Plan → Parallel Search (organic + news) → Parallel Scrape →
  Relevance Filter → Gap Check → Streaming Synthesis → Recommendations

What's new in v2 vs v1:
  ✦ Parallel search  — all queries fire simultaneously (ThreadPoolExecutor)
  ✦ Parallel scrape  — 8 concurrent page reads (3-5× speed improvement)
  ✦ Dual search      — Serper organic + Serper news in the same round
  ✦ Live synthesis   — Groq streams tokens to the frontend in real time
  ✦ Smart passages   — extracts most relevant paragraphs, not blind first-N chars
  ✦ Token budgeting  — high-trust sources get more context allocation
  ✦ Redis caching    — search + scrape results cached (graceful fallback)
  ✦ Content dedup    — URL + content fingerprint deduplication
  ✦ Relevance filter — low-signal sources excluded before synthesis
  ✦ Longer reports   — 3 000-token synthesis budget, 700-word minimum

Env vars:
  GROQ_API_KEY                   required
  SERPER_API_KEY                 required
  REDIS_URL                      optional  (default: redis://localhost:6379/0)
  DEEPSEARCH_PRIMARY_MODEL       optional  (default: openai/gpt-oss-120b)
  DEEPSEARCH_FALLBACK_MODEL      optional  (default: openai/gpt-oss-20b)
  DEEPSEARCH_MAX_ROUNDS          optional  (default: 3)
  DEEPSEARCH_RESULTS_PER_QUERY   optional  (default: 8)
  DEEPSEARCH_READ_PER_QUERY      optional  (default: 4)
  DEEPSEARCH_MAX_TOTAL_SOURCES   optional  (default: 25)
  DEEPSEARCH_MAX_SYNTH_SOURCES   optional  (default: 14)
  SERPER_GL                      optional  (default: in)
  SERPER_HL                      optional  (default: en)

SSE event types yielded by run_deepsearch():
  progress    {message}                   — status updates
  plan        {questions}                 — research angles list
  source      {title, url, domain}        — each discovered source
  stream      {chunk}                     — live synthesis tokens  ← NEW
  result      {content, source_count, sources, recommendations}
  suggestions {questions}                 — follow-up question list
  error       {message}
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════

PRIMARY_MODEL  = os.getenv("DEEPSEARCH_PRIMARY_MODEL",  "openai/gpt-oss-120b")
FALLBACK_MODEL = os.getenv("DEEPSEARCH_FALLBACK_MODEL", "openai/gpt-oss-20b")
MODELS         = [PRIMARY_MODEL, FALLBACK_MODEL]

SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
SERPER_GL      = os.getenv("SERPER_GL", "in")
SERPER_HL      = os.getenv("SERPER_HL", "en")
REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379/0")

MAX_ROUNDS          = int(os.getenv("DEEPSEARCH_MAX_ROUNDS",          "3"))
RESULTS_PER_QUERY   = int(os.getenv("DEEPSEARCH_RESULTS_PER_QUERY",   "8"))
READ_PER_QUERY      = int(os.getenv("DEEPSEARCH_READ_PER_QUERY",      "4"))
MAX_TOTAL_SOURCES   = int(os.getenv("DEEPSEARCH_MAX_TOTAL_SOURCES",   "25"))
MAX_SYNTH_SOURCES   = int(os.getenv("DEEPSEARCH_MAX_SYNTH_SOURCES",   "14"))

SCRAPE_WORKERS      = 8      # concurrent page fetches
SEARCH_WORKERS      = 5      # concurrent Serper requests
SCRAPE_TIMEOUT      = 12     # seconds per page
SERPER_TIMEOUT      = 10     # seconds per search call
CACHE_SEARCH_TTL    = 3_600  # 1 hour
CACHE_SCRAPE_TTL    = 86_400 # 24 hours
RELEVANCE_THRESHOLD = 0.07   # min keyword overlap to include a source
SYNTH_CONTEXT_CHARS = 13_000 # total char budget fed to synthesizer
SYNTH_MAX_TOKENS    = 3_000  # Groq max_tokens for synthesis

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

SKIP_EXTENSIONS = frozenset({
    ".pdf", ".zip", ".doc", ".docx", ".ppt", ".pptx",
    ".xls", ".xlsx", ".mp4", ".mp3", ".avi", ".png",
    ".jpg", ".jpeg", ".gif", ".svg", ".exe", ".dmg",
})

# Domain trust tiers ─ used for source ranking + context budget allocation
TIER1_DOMAINS: frozenset[str] = frozenset({
    "wikipedia.org", "nature.com", "science.org", "pubmed.ncbi.nlm.nih.gov",
    "scholar.google.com", "arxiv.org", "bbc.com", "reuters.com", "apnews.com",
    "theguardian.com", "nytimes.com", "wsj.com", "ft.com", "economist.com",
    "who.int", "cdc.gov", "nih.gov", "nasa.gov", "mit.edu", "stanford.edu",
    "harvard.edu", "techcrunch.com", "wired.com", "arstechnica.com",
    "thehindu.com", "hindustantimes.com", "ndtv.com", "livemint.com",
    "economictimes.indiatimes.com", "timesofindia.indiatimes.com",
    "moneycontrol.com", "financialexpress.com", "business-standard.com",
    "pib.gov.in", "mospi.gov.in", "rbi.org.in", "sebi.gov.in",
})
TIER2_DOMAINS: frozenset[str] = frozenset({
    "medium.com", "forbes.com", "bloomberg.com", "businessinsider.com",
    "cnbc.com", "cnn.com", "theverge.com", "engadget.com", "zdnet.com",
    "investopedia.com", "britannica.com", "statista.com", "mckinsey.com",
    "hbr.org", "venturebeat.com", "towardsdatascience.com", "substack.com",
    "analyticsvidhya.com", "kaggle.com",
})


# ═══════════════════════════════════════════════════════════════════════════
#  REDIS CACHE  (optional — all ops are no-ops if Redis is unavailable)
# ═══════════════════════════════════════════════════════════════════════════

_redis_client = None
_redis_unavailable = False  # stop retrying after first failure


def _get_redis():
    global _redis_client, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis_client is not None:
        return _redis_client
    try:
        import redis  # type: ignore
        r = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        r.ping()
        _redis_client = r
        return r
    except Exception:
        _redis_unavailable = True
        return None


def cache_get(key: str) -> str | None:
    try:
        r = _get_redis()
        if r:
            val = r.get(key)
            return val.decode("utf-8") if val else None
    except Exception:
        pass
    return None


def cache_set(key: str, value: str, ttl: int = 3_600) -> None:
    try:
        r = _get_redis()
        if r:
            r.setex(key, ttl, value.encode("utf-8"))
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════
#  GROQ CLIENT  (lazy singleton)
# ═══════════════════════════════════════════════════════════════════════════

_groq_client = None


def _get_groq():
    global _groq_client
    if _groq_client is None:
        from groq import Groq  # type: ignore
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not set.")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def safe_trim(text: str, limit: int = 4_000) -> str:
    text = text or ""
    return text[:limit] if len(text) > limit else text


def clean_text(text: str) -> str:
    text = text or ""
    return re.sub(r"\s+", " ", text).strip()


def parse_numbered_list(text: str, limit: int = 6) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = re.sub(r"^[-*•]\s*", "", raw.strip())
        line = re.sub(r"^\d+[\.\)\-]\s*", "", line).strip(" \"'`")
        if len(line) < 8:
            continue
        key = line.lower()
        if key not in seen:
            seen.add(key)
            items.append(line)
        if len(items) >= limit:
            break
    return items


def domain_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
    except Exception:
        return url.strip()


def domain_trust_score(domain: str) -> int:
    """0 = unknown, 1 = tier-2, 2 = tier-1."""
    if domain in TIER1_DOMAINS:
        return 2
    if domain in TIER2_DOMAINS:
        return 1
    return 0


def sse_event(type_: str, **kwargs) -> str:
    return f"data: {json.dumps({'type': type_, **kwargs}, ensure_ascii=False)}\n\n"


def content_fingerprint(text: str) -> str:
    """Cheap similarity key — first 400 chars MD5."""
    return hashlib.md5((text or "")[:400].encode()).hexdigest()


def relevance_score(content: str, question: str) -> float:
    """Fast keyword-overlap relevance — no LLM needed. Returns 0–1."""
    if not content or not question:
        return 0.0
    q_words = set(re.findall(r"\b\w{4,}\b", question.lower()))
    c_words = set(re.findall(r"\b\w{4,}\b", content[:3_000].lower()))
    if not q_words:
        return 0.5
    return len(q_words & c_words) / len(q_words)


def extract_relevant_passages(content: str, query: str, budget: int = 1_200) -> str:
    """
    Select the most topically relevant paragraphs from a scraped page
    instead of blindly taking the first N characters (which is often
    navigation, ads, or boilerplate).
    """
    if not content:
        return ""
    if len(content) <= budget:
        return content

    # Split on blank lines or hard line-breaks before sentences
    paragraphs = [p.strip() for p in re.split(r"\n{2,}|\n(?=[A-Z])", content) if len(p.strip()) > 60]
    if not paragraphs:
        return safe_trim(content, budget)

    q_words = set(re.findall(r"\b\w{4,}\b", query.lower()))

    def para_score(p: str) -> float:
        p_words = set(re.findall(r"\b\w{4,}\b", p.lower()))
        overlap  = len(q_words & p_words) / max(len(q_words), 1)
        length_b = min(len(p) / 400.0, 1.5)
        penalty  = 0.5 if len(p) < 100 else 1.0
        return (overlap * 2.5 + length_b) * penalty

    ranked = sorted(paragraphs, key=para_score, reverse=True)

    selected: list[str] = []
    total = 0
    for p in ranked:
        if total + len(p) + 2 > budget:
            if not selected:
                selected.append(p[:budget])
            break
        selected.append(p)
        total += len(p) + 2

    return "\n\n".join(selected)


def run_llm(
    messages: list[dict],
    max_tokens: int = 1_500,
    temperature: float = 0.2,
) -> str:
    """Non-streaming LLM call with automatic model fallback. Never raises."""
    client = _get_groq()
    for model in MODELS:
        try:
            c = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (c.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"[LLM] {model} error: {e}")
            time.sleep(0.4)
    return ""


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 1 — RESEARCH PLANNER
# ═══════════════════════════════════════════════════════════════════════════

def planner(question: str) -> list[str]:
    """
    Break the research question into 6 diverse, targeted search queries —
    each covering a different angle so we get maximum coverage.
    """
    prompt = f"""You are an expert research planner for Pranox DeepSearch (like Perplexity AI).

User's research question: "{question}"

Generate 6 highly specific, diverse Google search queries for complete topic coverage.

Each query MUST cover a different angle:
1. Core definition / background / history
2. Latest news / current developments (2024-2025)
3. Key statistics / data / research findings
4. Expert analysis / academic / industry perspective
5. Challenges / criticism / risks / limitations
6. Future outlook / impact / comparison with alternatives

Rules:
- Write every query exactly as you'd type it into Google (real search syntax)
- Include specific keywords, years, or qualifiers that surface authoritative sources
- Make each query meaningfully distinct

Output ONLY a numbered list. No explanations. No other text.

1. [query]
2. [query]
3. [query]
4. [query]
5. [query]
6. [query]"""

    reply = run_llm([{"role": "user", "content": prompt}], max_tokens=400, temperature=0.15)
    queries = parse_numbered_list(reply, limit=6)

    if not queries:
        base = question.strip(" ?")
        queries = [
            question,
            f"{base} 2024 2025 latest developments",
            f"{base} statistics data research findings",
            f"{base} benefits challenges expert analysis",
            f"{base} future outlook trends impact",
            f"{base} comparison alternatives",
        ]

    return queries[:6]


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 2 — PARALLEL SEARCH  (organic + news, all queries at once)
# ═══════════════════════════════════════════════════════════════════════════

def _serper_call(endpoint: str, query: str, num: int) -> list[dict]:
    """
    Single Serper API request against /search or /news.
    Results are Redis-cached for CACHE_SEARCH_TTL seconds.
    """
    cache_key = f"serper:{endpoint}:{hashlib.md5(query.encode()).hexdigest()}:{num}"
    cached = cache_get(cache_key)
    if cached:
        try:
            return json.loads(cached)
        except Exception:
            pass

    try:
        resp = requests.post(
            f"https://google.serper.dev/{endpoint}",
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": num, "gl": SERPER_GL, "hl": SERPER_HL},
            timeout=SERPER_TIMEOUT,
        )
        if resp.status_code != 200:
            print(f"[SEARCH] HTTP {resp.status_code} on '{query[:50]}'")
            return []

        data = resp.json()
        results: list[dict] = []

        if endpoint == "search":
            # 1. Answer Box — highest-quality direct answer
            if data.get("answerBox"):
                ab = data["answerBox"]
                answer = ab.get("answer") or ab.get("snippet") or ""
                if answer:
                    results.append({
                        "title":   ab.get("title", "Direct Answer"),
                        "url":     ab.get("link", ""),
                        "snippet": clean_text(answer),
                        "kind":    "answer_box",
                    })

            # 2. Knowledge Graph — structured entity facts
            if data.get("knowledgeGraph"):
                kg   = data["knowledgeGraph"]
                desc = kg.get("description", "")
                attr = " ".join(
                    f"{k}: {v}."
                    for k, v in list(kg.get("attributes", {}).items())[:6]
                )
                combined = clean_text(f"{desc} {attr}").strip()
                if combined:
                    results.append({
                        "title":   kg.get("title", "Knowledge Graph"),
                        "url":     kg.get("descriptionLink", ""),
                        "snippet": combined,
                        "kind":    "knowledge_graph",
                    })

            # 3. Organic results
            for item in data.get("organic", [])[:num]:
                t = clean_text(item.get("title", ""))
                s = clean_text(item.get("snippet", ""))
                if t and s:
                    results.append({
                        "title":   t,
                        "url":     item.get("link", ""),
                        "snippet": s,
                        "kind":    "organic",
                    })

        elif endpoint == "news":
            for item in data.get("news", [])[:num]:
                t = clean_text(item.get("title", ""))
                s = clean_text(item.get("snippet", ""))
                if t:
                    results.append({
                        "title":   t,
                        "url":     item.get("link", ""),
                        "snippet": s,
                        "kind":    "news",
                        "date":    item.get("date", ""),
                    })

        if results:
            cache_set(cache_key, json.dumps(results), CACHE_SEARCH_TTL)

        return results

    except requests.Timeout:
        print(f"[SEARCH] Timeout: {query[:60]}")
        return []
    except Exception as e:
        print(f"[SEARCH] Error ({query[:60]}): {e}")
        return []


def search_parallel(queries: list[str]) -> dict[str, list[dict]]:
    """
    Fire all queries against Serper organic AND news endpoints simultaneously.
    Returns {query: [merged_results]} dict.
    """
    all_results: dict[str, list[dict]] = {q: [] for q in queries}
    tasks: dict = {}

    with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as executor:
        # Organic for all queries
        for q in queries:
            f = executor.submit(_serper_call, "search", q, RESULTS_PER_QUERY)
            tasks[f] = q

        # News for first 3 queries (most relevant angles)
        for q in queries[:3]:
            f = executor.submit(_serper_call, "news", q, 5)
            tasks[f] = q

        for future in as_completed(tasks, timeout=30):
            q = tasks[future]
            try:
                all_results[q].extend(future.result())
            except Exception as e:
                print(f"[SEARCH PARALLEL] '{q[:40]}': {e}")

    return all_results


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 3 — PARALLEL SCRAPER
# ═══════════════════════════════════════════════════════════════════════════

def scrape_url(url: str) -> str:
    """
    Fetch and extract main content from a URL.
    Uses trafilatura for precision extraction, falls back to raw requests.
    Caches result in Redis for 24 hours.
    """
    if not url:
        return ""
    url_lower = url.lower()
    if any(url_lower.endswith(ext) for ext in SKIP_EXTENSIONS):
        return ""

    cache_key = f"scrape:{hashlib.md5(url.encode()).hexdigest()}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    downloaded: str | None = None

    try:
        import trafilatura  # type: ignore

        try:
            downloaded = trafilatura.fetch_url(url)
        except Exception:
            pass

        # Fallback: plain requests
        if not downloaded:
            try:
                r = requests.get(
                    url,
                    headers=REQUEST_HEADERS,
                    timeout=SCRAPE_TIMEOUT,
                    allow_redirects=True,
                )
                if r.status_code == 200:
                    downloaded = r.text
            except Exception:
                pass

        if not downloaded:
            return ""

        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
            no_fallback=False,
        ) or ""

        result = clean_text(text)[:6_000]  # raw cap before passage extraction
        if result:
            cache_set(cache_key, result, CACHE_SCRAPE_TTL)
        return result

    except Exception as e:
        print(f"[SCRAPE] {url}: {e}")
        return ""


def scrape_batch(url_pairs: list[tuple[str, str]]) -> dict[str, str]:
    """
    Scrape multiple URLs concurrently.
    url_pairs: [(url, query), ...]
    Returns {url: content}.
    """
    results: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=SCRAPE_WORKERS) as executor:
        future_to_url = {
            executor.submit(scrape_url, url): url
            for url, _ in url_pairs
            if url
        }
        for future in as_completed(future_to_url, timeout=35):
            url = future_to_url[future]
            try:
                results[url] = future.result() or ""
            except Exception:
                results[url] = ""

    return results


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 4 — SOURCE SCORING & RANKING
# ═══════════════════════════════════════════════════════════════════════════

def _source_score(item: dict) -> float:
    """Composite quality score for a source item."""
    s = domain_trust_score(item.get("domain", "")) * 2.0

    content = item.get("content", "")
    if content and len(content) > 200:
        s += 3.0

    kind = item.get("kind", "")
    if kind == "answer_box":
        s += 2.5
    elif kind == "knowledge_graph":
        s += 2.0
    elif kind == "news":
        s += 0.8

    if len(item.get("snippet", "")) > 200:
        s += 0.5

    rel = item.get("relevance", 0.0)
    if rel > 0.3:
        s += rel * 1.5
    elif rel > 0.15:
        s += rel

    return s


def rank_sources(knowledge_base: list[dict]) -> list[dict]:
    return sorted(knowledge_base, key=_source_score, reverse=True)


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 5 — GAP CHECKER
# ═══════════════════════════════════════════════════════════════════════════

def gap_checker(
    question: str,
    knowledge_base: list[dict],
) -> tuple[bool, list[str]]:
    """
    Ask the LLM whether collected evidence is sufficient for a comprehensive
    report, or whether specific angles are still missing.
    Returns (is_sufficient, follow_up_queries).
    """
    if len(knowledge_base) >= MAX_TOTAL_SOURCES:
        return True, []

    compact = "\n".join(
        f"- [{item.get('kind', 'web')}] {item.get('title', '')} — "
        f"{safe_trim(item.get('snippet', ''), 160)}"
        for item in knowledge_base[:16]
    )

    prompt = f"""Research question: "{question}"

Evidence collected so far ({len(knowledge_base)} sources):
{compact}

Is this enough for a comprehensive, well-cited research report?

Check: background ✓? current data ✓? statistics/numbers ✓? expert views ✓? risks/challenges ✓?

If sufficient, reply exactly:
STATUS: SUFFICIENT

If gaps remain, reply exactly:
STATUS: MORE_NEEDED
QUERIES:
1. [specific missing angle query]
2. [specific missing angle query]
3. [specific missing angle query]

No other text."""

    reply = run_llm(
        [{"role": "user", "content": prompt}],
        max_tokens=260,
        temperature=0.1,
    )

    if re.search(r"STATUS:\s*SUFFICIENT", reply, re.I):
        return True, []

    queries_text = ""
    idx = reply.upper().find("QUERIES:")
    if idx != -1:
        queries_text = reply[idx + 8:]

    return False, parse_numbered_list(queries_text, limit=3)


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 6 — SMART CONTEXT BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_context(
    sources: list[dict],
    total_budget: int = SYNTH_CONTEXT_CHARS,
) -> tuple[str, str]:
    """
    Allocate context budget proportionally by source quality score.
    High-trust sources with full content get more characters.
    For each source, extract the most relevant paragraphs (not blind first-N).
    """
    if not sources:
        return "", ""

    scores = [max(_source_score(item), 0.1) for item in sources]
    total_score = sum(scores)
    min_chars = 280

    context_parts: list[str] = []
    source_lines:  list[str] = []

    for i, (item, score) in enumerate(zip(sources, scores), 1):
        alloc = max(int((score / total_score) * total_budget), min_chars)

        raw = item.get("content") or item.get("snippet") or ""
        if item.get("content") and len(item["content"]) > 300:
            text = extract_relevant_passages(item["content"], item.get("query", ""), alloc)
        else:
            text = safe_trim(raw, alloc)

        date_tag = f" | {item['date']}" if item.get("date") else ""
        context_parts.append(
            f"[Source {i}] {item.get('title', 'Untitled')} "
            f"({item.get('domain', 'unknown')}{date_tag})\n"
            f"URL: {item.get('url', '')}\n"
            f"{text}"
        )

        if item.get("url"):
            source_lines.append(
                f"[{i}] {item.get('title', 'Untitled')} — {item.get('url', '')}"
            )

    return "\n\n---\n\n".join(context_parts), "\n".join(source_lines)


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 7 — SYNTHESIS PROMPT
# ═══════════════════════════════════════════════════════════════════════════

def _build_synthesis_prompt(
    question: str,
    context: str,
    sources_list: str,
    source_count: int,
) -> str:
    return f"""You are Pranox DeepSearch — a world-class AI research engine rivalling Perplexity and Gemini Deep Research.

Research question: "{question}"

You have gathered and read evidence from {source_count} real web sources. Write a premium, insightful research report.

═══ EVIDENCE FROM THE WEB ═══
{context}

═══ SOURCE REFERENCE LIST ═══
{sources_list}

═══ REPORT REQUIREMENTS ═══
• Minimum 700 words — this is a DEEP research report, not a quick summary
• Open with 2–3 sentences that directly answer the question
• Use ## markdown headings for every section
• After every factual claim add an inline citation [1], [2] etc. — cite ONLY numbered sources above
• Cross-reference multiple sources; explicitly note agreements AND conflicts
• Include specific numbers, dates, names, statistics wherever the evidence provides them
• Be analytical and insightful — draw connections, not just descriptions
• Never write "based on my research", "I searched", or "according to the sources" — be authoritative
• If evidence is thin on a sub-topic, say so honestly

═══ REQUIRED STRUCTURE ═══

## Overview
[2–3 sentence direct answer + essential context]

## Key Findings
[5–7 bullet points of the most important facts, each with at least one citation]

## Detailed Analysis
[3–4 paragraphs of in-depth analysis comparing and contrasting sources, all claims cited]

## Current State & Recent Developments
[Latest news, trends, data points — what's true as of 2024-2025]

## Challenges & Limitations
[Counterpoints, risks, unsolved problems, conflicting evidence, criticism]

## Outlook & Implications
[Where this is headed, what it means, why it matters — forward-looking with citations]

## Sources
[List every source exactly as numbered in the SOURCE REFERENCE LIST above]

Write the full report now. Be thorough, specific, and analytical."""


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 8 — FOLLOW-UP RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════════════

def generate_recommendations(question: str, report: str) -> list[str]:
    """Generate 4 specific follow-up research questions based on the report."""
    prompt = f"""Based on this research, generate 4 specific follow-up questions a curious researcher would ask next.

Original question: "{question}"
Report excerpt: {safe_trim(report, 1_200)}

Rules:
- Each question must be independently researchable
- Cover different angles: deeper detail, comparison, future impact, related topic
- Avoid vague "tell me more" phrasing
- Return ONLY a numbered list, no other text"""

    reply = run_llm(
        [{"role": "user", "content": prompt}],
        max_tokens=260,
        temperature=0.5,
    )
    questions = parse_numbered_list(reply, limit=4)

    if not questions:
        base = re.sub(
            r"^(what is|how does|explain|tell me about|who is|what are)\s+",
            "", question, flags=re.I,
        ).strip(" ?") or "this topic"
        questions = [
            f"What are the biggest challenges currently facing {base}?",
            f"How will {base} evolve over the next five years?",
            f"Who are the leading experts or organizations working on {base}?",
            f"How does {base} compare to the most popular alternatives?",
        ]

    return questions[:4]


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN AGENTIC LOOP
# ═══════════════════════════════════════════════════════════════════════════

def run_deepsearch(question: str):
    """
    Flask SSE generator — yields SSE-formatted strings for live frontend updates.

    Usage in app.py:
        from deepsearch import run_deepsearch
        return Response(
            stream_with_context(run_deepsearch(q)),
            mimetype="text/event-stream"
        )
    """
    question = clean_text(question)

    # ── Pre-flight checks ──────────────────────────────────────────────────
    if not question:
        yield sse_event("error", message="Please enter a research question.")
        return
    if not SERPER_API_KEY:
        yield sse_event("error", message="SERPER_API_KEY is not configured.")
        return
    if not os.getenv("GROQ_API_KEY"):
        yield sse_event("error", message="GROQ_API_KEY is not configured.")
        return

    try:
        # ══════════════════════════════════════════════════════════════════
        #  PHASE 1 — PLAN
        # ══════════════════════════════════════════════════════════════════
        yield sse_event("progress", message="Analyzing your research question...")
        sub_questions = planner(question)

        yield sse_event("plan", questions=sub_questions)
        yield sse_event(
            "progress",
            message=f"Research plan ready — {len(sub_questions)} search angles identified",
        )

        knowledge_base:      list[dict] = []
        seen_urls:           set[str]   = set()
        seen_fingerprints:   set[str]   = set()
        queries_to_search              = sub_questions[:]

        # ══════════════════════════════════════════════════════════════════
        #  PHASE 2 — MULTI-ROUND PARALLEL SEARCH + SCRAPE
        # ══════════════════════════════════════════════════════════════════
        for round_num in range(1, MAX_ROUNDS + 1):
            if not queries_to_search or len(knowledge_base) >= MAX_TOTAL_SOURCES:
                break

            yield sse_event(
                "progress",
                message=f"Round {round_num} — searching {len(queries_to_search)} angles in parallel...",
            )

            # ── Parallel search (organic + news) ──────────────────────────
            all_search_results = search_parallel(queries_to_search)

            # ── Collect items + identify URLs to scrape ───────────────────
            candidates: list[tuple[str, str, dict]] = []  # (url, query, item)

            for query, results in all_search_results.items():
                scrape_count = 0
                for result in results:
                    if len(knowledge_base) + len(candidates) >= MAX_TOTAL_SOURCES:
                        break

                    raw_url  = result.get("url", "")
                    url      = normalize_url(raw_url)
                    title    = clean_text(result.get("title", "Untitled"))
                    snippet  = clean_text(result.get("snippet", ""))
                    domain   = domain_from_url(url)

                    # URL-level deduplication
                    dedupe_key = url or hashlib.md5((title + snippet).encode()).hexdigest()
                    if dedupe_key in seen_urls:
                        continue

                    # Content-level deduplication (catches mirrors / reposts)
                    fp = content_fingerprint(snippet)
                    if fp in seen_fingerprints and snippet:
                        continue

                    seen_urls.add(dedupe_key)
                    seen_fingerprints.add(fp)

                    rel = relevance_score(snippet, question)

                    item: dict = {
                        "query":     query,
                        "title":     title,
                        "url":       url,
                        "domain":    domain,
                        "snippet":   snippet,
                        "content":   "",
                        "kind":      result.get("kind", "organic"),
                        "date":      result.get("date", ""),
                        "relevance": rel,
                    }

                    # Emit source to frontend immediately
                    yield sse_event("source", title=title, url=url, domain=domain)

                    # Queue for parallel scraping if relevant enough
                    if url and scrape_count < READ_PER_QUERY and rel >= RELEVANCE_THRESHOLD:
                        candidates.append((url, query, item))
                        scrape_count += 1
                    else:
                        knowledge_base.append(item)

            # ── Parallel scrape ────────────────────────────────────────────
            if candidates:
                yield sse_event(
                    "progress",
                    message=f"Reading {len(candidates)} pages simultaneously...",
                )
                scraped = scrape_batch([(url, q) for url, q, _ in candidates])

                for url, query, item in candidates:
                    content = scraped.get(url, "")
                    item["content"] = content

                    # Refine relevance score with full content
                    if content:
                        item["relevance"] = max(
                            item["relevance"],
                            relevance_score(content[:2_000], question),
                        )

                    knowledge_base.append(item)

            yield sse_event(
                "progress",
                message=f"Round {round_num} complete — {len(knowledge_base)} sources collected",
            )

            # ── Gap check (between rounds only) ───────────────────────────
            if round_num < MAX_ROUNDS and len(knowledge_base) < MAX_TOTAL_SOURCES:
                yield sse_event("progress", message="Checking for missing research angles...")
                sufficient, followup = gap_checker(question, knowledge_base)

                if sufficient or not followup:
                    yield sse_event("progress", message="Coverage is strong — moving to synthesis...")
                    break

                queries_to_search = followup
                yield sse_event(
                    "progress",
                    message=f"Found {len(queries_to_search)} gaps — filling them...",
                )

        # ── Sanity check ──────────────────────────────────────────────────
        if not knowledge_base:
            yield sse_event(
                "error",
                message="Could not collect enough web evidence. Try rephrasing your question.",
            )
            return

        yield sse_event(
            "progress",
            message=f"Building report from {len(knowledge_base)} sources...",
        )

        # ══════════════════════════════════════════════════════════════════
        #  PHASE 3 — STREAMING SYNTHESIS
        #  (tokens stream live to the frontend as Groq generates them)
        # ══════════════════════════════════════════════════════════════════
        ranked           = rank_sources(knowledge_base)[:MAX_SYNTH_SOURCES]
        context, src_lst = build_context(ranked)
        synth_prompt     = _build_synthesis_prompt(question, context, src_lst, len(ranked))

        client       = _get_groq()
        report_parts: list[str] = []
        stream_started = False

        for model in MODELS:
            try:
                completion = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": synth_prompt}],
                    temperature=0.2,
                    max_tokens=SYNTH_MAX_TOKENS,
                    stream=True,
                )

                for chunk in completion:
                    if not chunk.choices:
                        continue  # some providers send a final usage-only chunk with no choices
                    delta = chunk.choices[0].delta.content or ""
                    if delta:
                        if not stream_started:
                            yield sse_event("progress", message="Writing report...")
                            stream_started = True
                        report_parts.append(delta)
                        yield sse_event("stream", chunk=delta)   # live token stream

                break  # success — skip fallback

            except Exception as e:
                print(f"[SYNTH STREAM] {model} failed: {e}")
                report_parts = []
                stream_started = False
                time.sleep(0.5)

        report = "".join(report_parts)

        if not report:
            yield sse_event("error", message="Report generation failed. Please try again.")
            return

        # ══════════════════════════════════════════════════════════════════
        #  PHASE 4 — RECOMMENDATIONS
        # ══════════════════════════════════════════════════════════════════
        recommendations = generate_recommendations(question, report)

        # ══════════════════════════════════════════════════════════════════
        #  PHASE 5 — EMIT FINAL RESULT
        # ══════════════════════════════════════════════════════════════════
        sources_for_frontend = [
            {
                "title":  item.get("title", "Source"),
                "url":    item.get("url", ""),
                "domain": item.get("domain", ""),
            }
            for item in ranked
            if item.get("url")
        ]

        yield sse_event("progress", message="Deep research complete!")

        yield sse_event(
            "result",
            content=report,
            source_count=len(sources_for_frontend),
            sources=sources_for_frontend[:15],
            recommendations=recommendations,
        )

        yield sse_event("suggestions", questions=recommendations)

    except RuntimeError as e:
        # Config errors (missing API keys etc.)
        print(f"[DEEPSEARCH CONFIG] {e}")
        yield sse_event("error", message=str(e))

    except Exception as e:
        print(f"[DEEPSEARCH FATAL] {e}")
        yield sse_event("error", message="An unexpected error occurred. Please try again.")