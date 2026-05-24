"""
Pranox Deep Research — Production Engine
======================================
Agentic research pipeline: Plan → Search → Read → Gap-check → Synthesize
Designed to work like Perplexity / Gemini Deep Research.

Env vars (all optional with sensible defaults):
  GROQ_API_KEY                  — required
  SERPER_API_KEY                — required
  DEEPSEARCH_PRIMARY_MODEL      — default: llama-3.3-70b-versatile
  DEEPSEARCH_FALLBACK_MODEL     — default: llama-3.1-8b-instant
  DEEPSEARCH_MAX_ROUNDS         — default: 2
  DEEPSEARCH_RESULTS_PER_QUERY  — default: 6
  DEEPSEARCH_READ_PER_QUERY     — default: 3
  DEEPSEARCH_MAX_TOTAL_SOURCES  — default: 20
  SERPER_GL                     — default: in
  SERPER_HL                     — default: en
"""

import os
import re
import json
import time
import hashlib
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════
#  CONFIGURATION
# ═══════════════════════════════════════════════════════

PRIMARY_MODEL  = os.getenv("DEEPSEARCH_PRIMARY_MODEL",  "llama-3.3-70b-versatile")
FALLBACK_MODEL = os.getenv("DEEPSEARCH_FALLBACK_MODEL", "llama-3.1-8b-instant")
MODELS = [PRIMARY_MODEL, FALLBACK_MODEL]

SERPER_API_KEY      = os.getenv("SERPER_API_KEY", "")
SERPER_GL           = os.getenv("SERPER_GL", "in")
SERPER_HL           = os.getenv("SERPER_HL", "en")

MAX_ROUNDS          = int(os.getenv("DEEPSEARCH_MAX_ROUNDS",         "2"))
RESULTS_PER_QUERY   = int(os.getenv("DEEPSEARCH_RESULTS_PER_QUERY",  "6"))
READ_PER_QUERY      = int(os.getenv("DEEPSEARCH_READ_PER_QUERY",     "3"))
MAX_TOTAL_SOURCES   = int(os.getenv("DEEPSEARCH_MAX_TOTAL_SOURCES",  "20"))
MAX_SYNTH_SOURCES   = 8    # max sources sent to synthesizer to avoid context overflow
SCRAPE_TIMEOUT      = 14   # seconds per page fetch
SERPER_TIMEOUT      = 12   # seconds per search request
INTER_QUERY_SLEEP   = 0.4  # seconds between queries to respect rate limits

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Domain trust tiers — used to rank sources before synthesis
TIER1_DOMAINS = {
    "wikipedia.org", "nature.com", "science.org", "pubmed.ncbi.nlm.nih.gov",
    "scholar.google.com", "arxiv.org", "bbc.com", "reuters.com", "apnews.com",
    "theguardian.com", "nytimes.com", "wsj.com", "ft.com", "economist.com",
    "who.int", "cdc.gov", "nih.gov", "nasa.gov", "mit.edu", "stanford.edu",
    "harvard.edu", "techcrunch.com", "wired.com", "arstechnica.com",
    "thehindu.com", "hindustantimes.com", "ndtv.com", "livemint.com",
    "economictimes.indiatimes.com", "timesofindia.indiatimes.com",
}
TIER2_DOMAINS = {
    "medium.com", "forbes.com", "bloomberg.com", "businessinsider.com",
    "cnbc.com", "cnn.com", "theverge.com", "engadget.com", "zdnet.com",
    "investopedia.com", "britannica.com", "statista.com", "mckinsey.com",
}


# ═══════════════════════════════════════════════════════
#  LAZY GROQ CLIENT
# ═══════════════════════════════════════════════════════

_groq_client = None

def get_groq_client():
    global _groq_client
    if _groq_client is None:
        from groq import Groq
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY is not set.")
        _groq_client = Groq(api_key=api_key)
    return _groq_client


# ═══════════════════════════════════════════════════════
#  CORE HELPERS
# ═══════════════════════════════════════════════════════

def safe_trim(text: str, limit: int = 4000) -> str:
    text = text or ""
    return text[:limit] if len(text) > limit else text


def clean_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def parse_numbered_list(text: str, limit: int = 6) -> list[str]:
    """Robustly parse numbered/bulleted/plain LLM list output."""
    items = []
    seen = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        line = re.sub(r"^[-*•]\s*", "", line)
        line = re.sub(r"^\d+[\.\)\-]\s*", "", line)
        line = line.strip(" \"'`")
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
        host = urlparse(url).netloc.lower()
        return host.replace("www.", "")
    except Exception:
        return ""


def normalize_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    except Exception:
        return url.strip()


def domain_trust_score(domain: str) -> int:
    """Return trust tier: 2 = highest, 1 = medium, 0 = unknown."""
    if domain in TIER1_DOMAINS:
        return 2
    if domain in TIER2_DOMAINS:
        return 1
    return 0


def sse_event(type_: str, **kwargs) -> str:
    payload = {"type": type_, **kwargs}
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def run_llm(
    messages: list[dict],
    max_tokens: int = 1500,
    temperature: float = 0.25,
    model_index: int = 0,
) -> str:
    """
    Call Groq with automatic fallback to secondary model.
    Returns empty string on total failure — never raises.
    """
    client = get_groq_client()
    for i in range(model_index, len(MODELS)):
        try:
            completion = client.chat.completions.create(
                model=MODELS[i],
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return (completion.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"[DEEPSEARCH LLM] model={MODELS[i]} failed: {e}")
            time.sleep(0.5)
    return ""


# ═══════════════════════════════════════════════════════
#  STEP 1 — RESEARCH PLANNER
# ═══════════════════════════════════════════════════════

def planner(question: str) -> list[str]:
    """
    Breaks the question into 5–6 targeted search queries covering
    different angles: background, current state, data/stats,
    expert opinions, risks, comparisons, future outlook.
    """
    prompt = f"""You are an expert research planner for Pranox DeepSearch.

User research question:
"{question}"

Break this into 5 strong, specific Google search queries that together will build a complete, thorough answer.

Each query must:
- Be directly searchable on Google (like a real search query, not a sentence)
- Cover a DIFFERENT angle: background/history, current state, key statistics/data, expert analysis, risks/challenges, comparisons, recent developments, future outlook
- Include relevant keywords that would surface authoritative sources

Return ONLY a numbered list of search queries. No explanations. No extra text.

Example format:
1. [specific search query]
2. [specific search query]
3. [specific search query]
4. [specific search query]
5. [specific search query]"""

    reply = run_llm(
        [{"role": "user", "content": prompt}],
        max_tokens=400,
        temperature=0.15,
    )
    queries = parse_numbered_list(reply, limit=6)

    if not queries:
        # Deterministic fallback — always works
        base = question.strip(" ?")
        queries = [
            question,
            f"{base} latest developments 2024 2025",
            f"{base} benefits risks challenges",
            f"{base} statistics data facts",
            f"{base} expert analysis future outlook",
        ]

    return queries[:6]


# ═══════════════════════════════════════════════════════
#  STEP 2 — WEB SEARCH
# ═══════════════════════════════════════════════════════

def serper_search(query: str, num: int = RESULTS_PER_QUERY) -> list[dict]:
    """Search Google via Serper API. Returns structured result list."""
    if not SERPER_API_KEY:
        print("[DEEPSEARCH] SERPER_API_KEY missing")
        return []

    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": SERPER_API_KEY,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": num, "gl": SERPER_GL, "hl": SERPER_HL},
            timeout=SERPER_TIMEOUT,
        )

        if response.status_code != 200:
            print(f"[DEEPSEARCH SEARCH] HTTP {response.status_code}: {response.text[:200]}")
            return []

        data = response.json()
        results = []

        # Google Answer Box — highest quality direct answer
        if data.get("answerBox"):
            ab = data["answerBox"]
            answer = ab.get("answer") or ab.get("snippet") or ""
            if answer:
                results.append({
                    "title": ab.get("title", "Direct Answer"),
                    "url": ab.get("link", ""),
                    "snippet": clean_text(answer),
                    "kind": "answer_box",
                })

        # Knowledge Graph — structured entity info
        if data.get("knowledgeGraph"):
            kg = data["knowledgeGraph"]
            desc = kg.get("description", "")
            attrs = kg.get("attributes", {})
            attr_text = " ".join(f"{k}: {v}." for k, v in list(attrs.items())[:8])
            combined = clean_text(f"{desc} {attr_text}").strip()
            if combined:
                results.append({
                    "title": kg.get("title", "Knowledge Graph"),
                    "url": kg.get("descriptionLink", ""),
                    "snippet": combined,
                    "kind": "knowledge_graph",
                })

        # Organic results
        for item in data.get("organic", [])[:num]:
            title   = clean_text(item.get("title", ""))
            url     = item.get("link", "")
            snippet = clean_text(item.get("snippet", ""))
            if not title or not snippet:
                continue
            results.append({
                "title":   title,
                "url":     url,
                "snippet": snippet,
                "kind":    "organic",
            })

        return results

    except requests.Timeout:
        print("[DEEPSEARCH SEARCH] Serper timeout")
        return []
    except Exception as e:
        print(f"[DEEPSEARCH SEARCH] Error: {e}")
        return []


# ═══════════════════════════════════════════════════════
#  STEP 3 — PAGE READER
# ═══════════════════════════════════════════════════════

def scrape_url(url: str) -> str:
    """
    Extract main text from a URL.
    Strategy: trafilatura first (best quality), requests fallback.
    Returns empty string on any failure — never raises.
    """
    if not url:
        return ""

    # Skip binary / office file formats
    skip_exts = (".pdf", ".zip", ".doc", ".docx", ".ppt", ".pptx",
                 ".xls", ".xlsx", ".mp4", ".mp3", ".avi", ".png",
                 ".jpg", ".jpeg", ".gif", ".svg")
    if any(url.lower().endswith(ext) for ext in skip_exts):
        return ""

    try:
        import trafilatura

        # trafilatura.fetch_url has its own timeout handling
        downloaded = None
        try:
            downloaded = trafilatura.fetch_url(url)
        except Exception:
            pass

        # Requests fallback if trafilatura fetch failed
        if not downloaded:
            try:
                r = requests.get(url, headers=REQUEST_HEADERS, timeout=SCRAPE_TIMEOUT)
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
        )

        return safe_trim(clean_text(text or ""), 5000)

    except Exception as e:
        print(f"[DEEPSEARCH SCRAPE] {url}: {e}")
        return ""


# ═══════════════════════════════════════════════════════
#  STEP 4 — SOURCE RANKER
# ═══════════════════════════════════════════════════════

def rank_sources(knowledge_base: list[dict]) -> list[dict]:
    """
    Score and sort sources by quality before feeding to synthesizer.
    Scoring: domain trust tier (0-2) + has full content (+2) + snippet length bonus.
    This ensures the synthesizer sees the best sources first.
    """
    def score(item: dict) -> int:
        s = domain_trust_score(item.get("domain", ""))
        if item.get("content"):
            s += 2
        if item.get("kind") in ("answer_box", "knowledge_graph"):
            s += 1
        snippet_len = len(item.get("snippet", ""))
        if snippet_len > 200:
            s += 1
        return s

    return sorted(knowledge_base, key=score, reverse=True)


# ═══════════════════════════════════════════════════════
#  STEP 5 — GAP CHECKER
# ═══════════════════════════════════════════════════════

def gap_checker(
    original_question: str,
    knowledge_base: list[dict],
) -> tuple[bool, list[str]]:
    """
    Decide whether existing evidence is sufficient.
    Returns: (is_sufficient, follow_up_queries)
    Note: if sufficient=True, follow_up_queries is always [].
    """
    if len(knowledge_base) >= MAX_TOTAL_SOURCES:
        return True, []

    compact = "\n".join(
        f"- [{item.get('query', '')}] {item.get('title', '')} — "
        f"{safe_trim(item.get('snippet', ''), 180)}"
        for item in knowledge_base[:14]
    )

    prompt = f"""You are a research quality checker.

Original question: "{original_question}"

Evidence collected so far ({len(knowledge_base)} sources):
{compact}

Is this evidence sufficient to write a comprehensive, well-cited research report?

Consider:
- Is the background/history covered?
- Is the current state covered?
- Are key facts, statistics, or data points present?
- Are risks, limitations, or opposing views represented?

If sufficient, reply:
STATUS: SUFFICIENT

If more is needed, reply:
STATUS: MORE_NEEDED
QUERIES:
1. [specific missing search query]
2. [specific missing search query]
3. [specific missing search query]

Reply in exactly this format. No other text."""

    reply = run_llm(
        [{"role": "user", "content": prompt}],
        max_tokens=280,
        temperature=0.1,
    )

    # Parse strictly — sufficient and queries are mutually exclusive
    is_sufficient = bool(re.search(r"STATUS:\s*SUFFICIENT", reply, re.I))

    if is_sufficient:
        return True, []

    # Only parse queries if MORE_NEEDED
    queries_section = ""
    if "QUERIES:" in reply.upper():
        queries_section = reply.upper().split("QUERIES:", 1)[1]
        # Get original-case text after QUERIES:
        idx = reply.upper().find("QUERIES:")
        queries_section = reply[idx + len("QUERIES:"):]

    followup = parse_numbered_list(queries_section, limit=3)
    return False, followup


# ═══════════════════════════════════════════════════════
#  STEP 6 — SYNTHESIS
# ═══════════════════════════════════════════════════════

def build_context(sources: list[dict]) -> tuple[str, str]:
    """Build the evidence context and source list for the synthesizer prompt."""
    context_parts = []
    source_lines  = []

    for i, item in enumerate(sources, 1):
        # Prefer full scraped content over snippet
        content = item.get("content") or item.get("snippet") or ""
        # Cap each source at 800 chars to leave room for all sources
        content = safe_trim(content, 800)

        context_parts.append(
            f"[Source {i}]\n"
            f"Title: {item.get('title', 'Untitled')}\n"
            f"Domain: {item.get('domain', 'unknown')}\n"
            f"URL: {item.get('url', '')}\n"
            f"Angle: {item.get('query', '')}\n"
            f"Content:\n{content}"
        )

        if item.get("url"):
            source_lines.append(
                f"[{i}] {item.get('title', 'Untitled')} — {item.get('url', '')}"
            )

    return "\n\n---\n\n".join(context_parts), "\n".join(source_lines)


def synthesizer(original_question: str, knowledge_base: list[dict]) -> str:
    """
    Generate the final research report.
    Uses top MAX_SYNTH_SOURCES ranked sources to avoid context overflow.
    """
    # Rank sources by quality, take best MAX_SYNTH_SOURCES
    ranked = rank_sources(knowledge_base)[:MAX_SYNTH_SOURCES]
    context, sources_list = build_context(ranked)

    prompt = f"""You are Pranox DeepSearch — a world-class AI research assistant like Perplexity or Gemini Deep Research.

User's research question:
"{original_question}"

You have gathered evidence from {len(ranked)} web sources. Write a premium, comprehensive research report.

EVIDENCE:
{safe_trim(context, 6000)}

SOURCE LIST:
{sources_list}

REPORT REQUIREMENTS:
- Start by directly answering the question in 2-3 sentences
- Use ## markdown headings for each section
- Use inline citations [1], [2], [3] after every factual claim — ONLY cite sources listed above
- Compare and cross-reference evidence across multiple sources
- Include specific numbers, statistics, dates, and named examples where available
- Acknowledge where sources conflict or where information is uncertain
- Do NOT write "I searched the web" or "based on my research" — just present findings authoritatively
- Write at least 500 words — this is a deep research report, not a summary
- Be specific, analytical, and genuinely useful

STRUCTURE (use these exact headings):
## Overview
## Key Findings
## Detailed Analysis
## Current State & Recent Developments
## Challenges & Limitations
## What This Means
## Sources

After ## Sources list every source exactly as numbered above.

Then add one final section:
## Related Questions
List 3 specific follow-up research questions the user might want to explore next."""

    return run_llm(
        [{"role": "user", "content": prompt}],
        max_tokens=2048,
        temperature=0.2,
    )


# ═══════════════════════════════════════════════════════
#  STEP 7 — FOLLOW-UP RECOMMENDATIONS
# ═══════════════════════════════════════════════════════

def generate_recommendations(original_question: str, report: str) -> list[str]:
    """Generate 4 specific follow-up research questions based on the report."""
    prompt = f"""Based on this research question and report, generate 4 specific, interesting follow-up research questions.

Original question: "{original_question}"

Report excerpt:
{safe_trim(report, 1500)}

Rules:
- Questions must be specific and self-contained (someone should be able to research them independently)
- Avoid vague phrases like "tell me more about" or "what else"
- Each question should explore a different dimension: deeper detail, comparison, future, impact, or alternative
- Return ONLY a numbered list, no extra text"""

    reply = run_llm(
        [{"role": "user", "content": prompt}],
        max_tokens=280,
        temperature=0.5,
    )
    questions = parse_numbered_list(reply, limit=4)

    if not questions:
        base = re.sub(
            r"^(what is|how does|explain|tell me about|who is|what are)\s+",
            "", original_question, flags=re.I,
        ).strip(" ?") or "this topic"
        questions = [
            f"What are the biggest challenges currently facing {base}?",
            f"How will {base} evolve over the next five years?",
            f"Who are the leading organizations or experts working on {base}?",
            f"How does {base} compare to the leading alternatives?",
        ]

    return questions[:4]


# ═══════════════════════════════════════════════════════
#  MAIN AGENTIC LOOP
# ═══════════════════════════════════════════════════════

def run_deepsearch(question: str):
    """
    Flask streaming generator.
    Usage in app.py:
        from deepsearch import run_deepsearch
        return Response(stream_with_context(run_deepsearch(q)), mimetype="text/event-stream")
    
    Yields SSE-formatted strings for live frontend progress updates.
    """
    question = clean_text(question)

    # ── Pre-flight checks ──────────────────────────────
    if not question:
        yield sse_event("error", message="Please enter a research question.")
        return

    if not SERPER_API_KEY:
        yield sse_event("error", message="Search API key is missing. Please configure SERPER_API_KEY.")
        return

    if not os.getenv("GROQ_API_KEY"):
        yield sse_event("error", message="AI API key is missing. Please configure GROQ_API_KEY.")
        return

    try:
        # ── PHASE 1: PLANNING ──────────────────────────
        yield sse_event("progress", message="Analyzing your research question...")
        sub_questions = planner(question)

        yield sse_event("plan", questions=sub_questions)
        yield sse_event(
            "progress",
            message=f"Research plan ready — {len(sub_questions)} search angles identified",
        )

        knowledge_base: list[dict] = []
        seen_urls: set[str]        = set()
        queries_to_search          = sub_questions[:]

        # ── PHASE 2: MULTI-ROUND SEARCH & SCRAPE ───────
        for round_num in range(1, MAX_ROUNDS + 1):
            if not queries_to_search:
                break

            yield sse_event(
                "progress",
                message=f"Round {round_num} — searching {len(queries_to_search)} angles...",
            )

            for query in queries_to_search:
                if len(knowledge_base) >= MAX_TOTAL_SOURCES:
                    break

                yield sse_event("progress", message=f"Searching: {query[:90]}")
                search_results = serper_search(query, num=RESULTS_PER_QUERY)

                if not search_results:
                    yield sse_event("progress", message=f"No results for: {query[:60]}")
                    time.sleep(INTER_QUERY_SLEEP)
                    continue

                read_count = 0

                for result in search_results:
                    if len(knowledge_base) >= MAX_TOTAL_SOURCES:
                        break

                    raw_url = result.get("url", "")
                    url     = normalize_url(raw_url)
                    title   = clean_text(result.get("title", "Untitled"))
                    snippet = clean_text(result.get("snippet", ""))

                    # Deduplicate by URL, or title+snippet hash if no URL
                    dedupe_key = url or hashlib.md5(
                        (title + snippet).encode("utf-8")
                    ).hexdigest()

                    if dedupe_key in seen_urls:
                        continue
                    seen_urls.add(dedupe_key)

                    domain = domain_from_url(url)
                    item: dict = {
                        "query":   query,
                        "title":   title,
                        "url":     url,
                        "domain":  domain,
                        "snippet": snippet,
                        "content": "",
                        "kind":    result.get("kind", "organic"),
                    }

                    # Notify frontend of new source
                    yield sse_event("source", title=title, url=url, domain=domain)

                    # Scrape full page for top sources per query
                    if url and read_count < READ_PER_QUERY:
                        yield sse_event("progress", message=f"Reading: {title[:75]}...")
                        content = scrape_url(url)
                        if content:
                            item["content"] = content
                            read_count += 1

                    knowledge_base.append(item)

                time.sleep(INTER_QUERY_SLEEP)

            yield sse_event(
                "progress",
                message=f"Round {round_num} complete — {len(knowledge_base)} sources collected",
            )

            # ── PHASE 3: GAP CHECK (between rounds only) ──
            if round_num < MAX_ROUNDS:
                yield sse_event("progress", message="Checking for missing research angles...")
                sufficient, followup_queries = gap_checker(question, knowledge_base)

                if sufficient or not followup_queries:
                    yield sse_event("progress", message="Evidence is strong — moving to report...")
                    break

                queries_to_search = followup_queries
                yield sse_event(
                    "progress",
                    message=f"Found {len(queries_to_search)} missing angles — searching more...",
                )

        # ── PHASE 4: SANITY CHECK ──────────────────────
        if not knowledge_base:
            yield sse_event(
                "error",
                message="Could not collect enough web evidence. Try rephrasing your question.",
            )
            return

        yield sse_event(
            "progress",
            message=f"Synthesizing report from {len(knowledge_base)} sources...",
        )

        # ── PHASE 5: SYNTHESIS ─────────────────────────
        report = synthesizer(question, knowledge_base)

        if not report:
            yield sse_event(
                "error",
                message="Evidence collected but report generation failed. Please try again.",
            )
            return

        # ── PHASE 6: RECOMMENDATIONS ───────────────────
        # Generated BEFORE result event so the frontend gets everything at once
        recommendations = generate_recommendations(question, report)

        # ── PHASE 7: EMIT RESULT ───────────────────────
        sources_for_frontend = [
            {
                "title":  item.get("title", "Source"),
                "url":    item.get("url", ""),
                "domain": item.get("domain", ""),
            }
            for item in rank_sources(knowledge_base)
            if item.get("url")
        ]

        yield sse_event("progress", message="DeepSearch report ready")

        yield sse_event(
            "result",
            content=report,
            source_count=len(sources_for_frontend),
            sources=sources_for_frontend[:12],
            recommendations=recommendations,
        )

        yield sse_event("suggestions", questions=recommendations)

    except RuntimeError as e:
        # Config errors (missing keys etc.)
        print(f"[DEEPSEARCH CONFIG ERROR] {e}")
        yield sse_event("error", message=str(e))

    except Exception as e:
        print(f"[DEEPSEARCH FATAL] {e}")
        yield sse_event(
            "error",
            message="Something went wrong during research. Please try again.",
        )