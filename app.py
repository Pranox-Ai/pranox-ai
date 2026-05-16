from flask import Flask, render_template, request, redirect, session, jsonify, send_file, send_from_directory
import os
import base64
import re
import json
import tempfile
import sqlite3
import requests
from io import BytesIO
from dotenv import load_dotenv
from authlib.integrations.flask_client import OAuth
from groq import Groq
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import pdfplumber
from PIL import Image

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_ENV") == "production",
    SESSION_COOKIE_HTTPONLY=True,
)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile", "prompt": "select_account"},
)

client = Groq(api_key=os.getenv("GROQ_API_KEY"))
CLOUDFLARE_API_TOKEN  = os.getenv("CLOUDFLARE_API_TOKEN", "")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")

def get_db():
    conn = sqlite3.connect("pranox.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS chats(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_email TEXT, role TEXT,
        message TEXT, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE IF NOT EXISTS user_memory(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_email TEXT, key TEXT, value TEXT)""")
    db.commit(); db.close()

init_db()

def save_memory(user_email, key, value):
    db = get_db()
    db.execute("DELETE FROM user_memory WHERE user_email=? AND key=?", (user_email, key))
    db.execute("INSERT INTO user_memory(user_email,key,value) VALUES (?,?,?)", (user_email, key, value))
    db.commit(); db.close()

def get_memory(user_email):
    db = get_db()
    rows = db.execute("SELECT key,value FROM user_memory WHERE user_email=?", (user_email,)).fetchall()
    db.close()
    return "\n".join([f"{r['key']}: {r['value']}" for r in rows])

# ═══════════════════════════════════════════
#  QUERY REWRITER
#  Cleans raw user messages into proper Google search queries
#  e.g. "tell me the latest cm of bangal" → "Chief Minister West Bengal 2025"
# ═══════════════════════════════════════════

PLACE_CORRECTIONS = {
    "bangal": "West Bengal", "bengal": "West Bengal",
    "tamilnadu": "Tamil Nadu", "tamilnad": "Tamil Nadu", "tn": "Tamil Nadu",
    "andhra": "Andhra Pradesh", "ap": "Andhra Pradesh",
    "telangana": "Telangana", "ts": "Telangana",
    "odisha": "Odisha", "orissa": "Odisha",
    "himachal": "Himachal Pradesh", "hp": "Himachal Pradesh",
    "arunachal": "Arunachal Pradesh",
    "up": "Uttar Pradesh", "uttar pradesh": "Uttar Pradesh",
    "mp": "Madhya Pradesh", "madhya pradesh": "Madhya Pradesh",
}

FILLER_RE = re.compile(
    r"^(tell me( the)?|can you tell me|do you know|i want to know|"
    r"please tell me|please |kindly |could you |give me |"
    r"i need to know |find out |look up |help me with |"
    r"what('s| is) the |who('s| is) the |let me know |"
    r"tell me about |give me information (on|about) )\s*",
    re.IGNORECASE
)

ROLE_MAP = {
    r'\bcm\b': 'Chief Minister',
    r'\bpm\b': 'Prime Minister',
    r'\bgovt\b': 'government',
    r'\bgov\b': 'governor',
    r'\bmla\b': 'MLA',
    r'\bceo\b': 'CEO',
    r'\bcto\b': 'CTO',
    r'\bcfo\b': 'CFO',
    r'\bcoo\b': 'COO',
}

POLITICAL_ROLES = [
    "chief minister", "prime minister", "president", "governor",
    "minister", "ceo", "chairman", "mla", "member of parliament",
    "owner", "head of"
]

def rewrite_search_query(user_message: str) -> str:
    """Convert messy user message into a clean Google search query."""
    msg = user_message.strip()

    # Step 1: Strip filler phrases from the start
    msg = FILLER_RE.sub("", msg).strip()

    # Step 2: Fix place name typos (work on lowercase copy)
    msg_lower = msg.lower()
    for typo, correct in PLACE_CORRECTIONS.items():
        msg_lower = re.sub(r'\b' + re.escape(typo) + r'\b', correct, msg_lower)
    msg = msg_lower

    # Step 3: Expand role abbreviations
    for pattern, replacement in ROLE_MAP.items():
        msg = re.sub(pattern, replacement, msg, flags=re.IGNORECASE)

    # Step 4: Remove vague time words (we'll add "2025" instead)
    msg = re.sub(r'\b(latest|current|present|now|today|recent|new|updated)\b', '', msg, flags=re.IGNORECASE)
    msg = re.sub(r'\s{2,}', ' ', msg).strip()

    # Step 5: Append year for political/current-affairs queries
    if any(role in msg.lower() for role in POLITICAL_ROLES):
        msg = msg + " 2025"

    # Step 6: Clean trailing punctuation
    msg = re.sub(r'[?!.,;:]+$', '', msg).strip()

    print(f"[QUERY REWRITE] '{user_message}' → '{msg}'")
    return msg if msg else user_message


# ═══════════════════════════════════════════
#  SERPER SEARCH  (unchanged structure, just called with rewritten query)
# ═══════════════════════════════════════════

def search_internet(query):
    """Search using Serper API and return clean, structured results."""
    try:
        api_key = os.getenv("SERPER_API_KEY")
        if not api_key:
            print("[SEARCH] SERPER_API_KEY not set")
            return ""

        res = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 8, "gl": "in", "hl": "en"},
            timeout=8
        )

        if res.status_code != 200:
            print(f"[SEARCH] Serper returned status {res.status_code}")
            return ""

        data = res.json()
        results = []

        # 1. Answer box — highest priority
        if "answerBox" in data:
            ab = data["answerBox"]
            answer = ab.get("answer") or ab.get("snippet") or ab.get("title", "")
            source = ab.get("link", "")
            if answer:
                results.append(f"[TOP ANSWER]: {answer}" + (f" (Source: {source})" if source else ""))

        # 2. Knowledge graph
        if "knowledgeGraph" in data:
            kg = data["knowledgeGraph"]
            title = kg.get("title", "")
            desc  = kg.get("description", "")
            attrs = kg.get("attributes", {})
            if title or desc:
                kg_text = f"[KNOWLEDGE GRAPH]: {title}"
                if desc: kg_text += f" — {desc}"
                for k, v in list(attrs.items())[:5]:
                    kg_text += f"\n  {k}: {v}"
                results.append(kg_text)

        # 3. Organic results
        if "organic" in data:
            for r in data["organic"][:6]:
                snippet = r.get("snippet", "")
                title   = r.get("title", "")
                link    = r.get("link", "")
                if snippet:
                    results.append(f"[SOURCE: {title}] ({link})\n  {snippet}")

        # 4. Related searches for context
        if "relatedSearches" in data and len(results) < 3:
            related = [r.get("query","") for r in data["relatedSearches"][:3]]
            if related:
                results.append(f"[Related]: {', '.join(related)}")

        final = "\n\n".join(results)
        print(f"[SEARCH SUCCESS] {len(results)} results, {len(final)} chars")
        return final

    except requests.Timeout:
        print("[SEARCH] Timeout after 8s")
        return ""
    except Exception as e:
        print(f"[SEARCH ERROR]: {e}")
        return ""

def safe_trim(text, limit=6000):
    return text[:limit] if len(text) > limit else text

def clean_text(text):
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

def run_ai(messages, model_index=0, max_tokens=1200):
    for i in range(model_index, len(MODELS)):
        try:
            completion = client.chat.completions.create(
                model=MODELS[i], messages=messages, temperature=0.3, max_tokens=max_tokens)
            return completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"AI ERROR (model {MODELS[i]}):", e)
    return None

def is_bad_response(reply):
    if not reply: return True
    bad_patterns = ["here's the corrected code", "flask application", "example of how you could", "missing code"]
    return any(p in reply.lower() for p in bad_patterns)

# ═══════════════════════════════════════════
#  SMART SEARCH TRIGGER  (unchanged)
# ═══════════════════════════════════════════

SEARCH_KEYWORDS = [
    "latest", "recent", "current", "today", "now", "news", "update",
    "2024", "2025", "2026",
    "price", "cost", "rate", "stock", "crypto", "bitcoin", "market",
    "weather", "temperature", "forecast",
    "who won", "who is winning", "score", "result", "match",
    "election", "vote", "winner", "elected",
    "war", "conflict", "attack", "crisis", "protest",
    "launched", "released", "announced", "introduced",
    "died", "arrested", "resigned", "appointed", "fired",
    "ipl", "cricket", "football", "nba", "nfl", "fifa",
    "trending", "viral", "breaking",
    "chief minister", "cm of", "cm is", "who is cm",
    "prime minister", "pm of", "pm is", "who is pm",
    "president of", "who is president",
    "minister of", "governor of", "mayor of",
    "mla", "mp ", "mp of", "mpp",
    "ruling party", "government of",
    "ceo of", "founder of", "owner of", "head of",
]

SEARCH_PATTERNS = [
    r"\bwho is (the )?(current |new |latest |present )?(cm|chief minister|pm|prime minister|president|ceo|founder|owner|governor|minister|mayor|chancellor|king|queen|coo|cto|cfo)\b",
    r"\bwhat is (the )?(current |latest |new )?(price|rate|status|situation|update)\b",
    r"\b(cm|chief minister|pm|prime minister|president|ceo) of \w+",
    r"\bcurrent (government|ruling party|leader|head)\b",
    r"\blatest (news|update|development|result)\b",
    r"\brecently (happened|announced|launched|released|arrested|elected)\b",
    r"\bwho (leads?|runs?|heads?|controls?|owns?) \w+",
    r"\bis \w+ (still|currently|now)\b",
    r"\bwhat happened (to|with|in|at)\b",
    r"\bhow much (does|is|are|did)\b",
    r"\bpresent (cm|pm|president|minister|ceo|chief)\b",
    r"\b(cm|pm|president|minister|ceo|governor) of (india|bengal|tamilnadu|tamil nadu|karnataka|kerala|andhra|telangana|maharashtra|gujarat|rajasthan|punjab|delhi|bihar|up|odisha|assam|jharkhand|chhattisgarh|uttarakhand|himachal|goa|manipur|meghalaya|mizoram|nagaland|sikkim|tripura|arunachal)\b",
    r"\bwho (is|are|was|were) (the )?(new|current|present|latest|sitting|elected|appointed|acting)\b",
    # catch "tell me the latest/current X" patterns
    r"\btell me (the )?(latest|current|present|new|recent)\b",
    # catch Indian state name mentions that usually need live data
    r"\b(bangal|bengal|tamilnadu|karnataka|kerala|andhra|telangana|gujarat|rajasthan|punjab|bihar|odisha)\b",
]

def needs_live_search(message: str) -> bool:
    """Return True if the query requires live internet search."""
    msg_lower = message.lower().strip()

    short_political = re.search(
        r"(who is|what is|tell me|who's|whats).{0,80}(cm|pm|president|minister|ceo|price|rate|score|result|winner|latest|current|present|chief|prime)",
        msg_lower
    )
    if short_political:
        return True

    if any(kw in msg_lower for kw in SEARCH_KEYWORDS):
        return True

    for pattern in SEARCH_PATTERNS:
        if re.search(pattern, msg_lower):
            return True

    return False


# ═══════════════════════════════════════════
#  SYSTEM PROMPTS  (unchanged)
# ═══════════════════════════════════════════

BASE_SYSTEM_PROMPT = """You are Pranox AI — a highly intelligent, friendly, and precise AI assistant.

IDENTITY:
- Founder: Chetansaipranav R
- Created: January 2026
- If asked who created you or about Pranox, always say: "Chetansaipranav R is the founder of Pranox AI. He created it in January 2026."

BEHAVIOR:
- Understand questions deeply, give clean final answers only
- Do NOT show internal reasoning steps
- Be friendly, smart, structured
- Use bullet points only when genuinely helpful
- For greetings (hi, hello, hey, good morning): respond warmly and briefly
- If user's name is in memory, use it naturally in greetings

CODE RULES:
- Always use proper code blocks with correct indentation
- Separate explanation from code clearly

FOLLOW-UP: Suggest 1–2 follow-up questions only when relevant.

PRANOX LINKS (share ONLY when user asks about Pranox social media):
- Instagram: https://www.instagram.com/pranoxgroups
- X: https://x.com/Pranoxgroups
- LinkedIn: https://www.linkedin.com/in/chetansaipranav-r-a6b18333b
- Product Hunt: https://www.producthunt.com/@pranoxai
- Email (only when asked for contact): pranoxoffical@gmail.com
"""

SEARCH_OVERRIDE_PROMPT = """
══════════════════════════════════════════════
🔴 CRITICAL INSTRUCTION — LIVE SEARCH MODE 🔴
══════════════════════════════════════════════

Real-time web search results have been fetched for this query.
These results are from TODAY and are MORE ACCURATE than your training data.

YOU MUST FOLLOW THESE RULES WITHOUT EXCEPTION:

1. USE THE SEARCH RESULTS to answer the question.
   - The answer is IN the search results — read them carefully.
   - Extract the direct answer from [TOP ANSWER] or [KNOWLEDGE GRAPH] first.
   - If not there, read the [SOURCE] snippets to find the answer.

2. DO NOT use your training data for this answer.
   - Your training may be from 2023/2024 — it is OUTDATED for current info.
   - NEVER say "as of my last update" or "my knowledge cutoff" when search results exist.
   - NEVER say "I don't have real-time data" when search results are provided.

3. STATE THE ANSWER DIRECTLY and CONFIDENTLY.
   - If search results say the CM is X, say "The Chief Minister is X."
   - If results show a price, state that price.
   - If results confirm an election result, state it.

4. CITE YOUR SOURCE naturally.
   - Add phrases like "According to recent sources..." or "As of today..."
   - Mention the search result source if it adds credibility.

5. If search results are unclear or conflicting:
   - State what the most reliable source says
   - Mention there may be conflicting info and suggest verifying

BOTTOM LINE: The search results below contain the real answer. Use them.
══════════════════════════════════════════════
"""

def build_system_prompt(memory: str) -> str:
    base = BASE_SYSTEM_PROMPT
    if memory:
        base += f"\nUSER MEMORY:\n{memory}\n"
    else:
        base += "\nUSER MEMORY: No memory stored yet.\n"
    return base


def build_messages_with_search(system_prompt: str, search_results: str, history: list, user_message: str) -> list:
    msgs = [{"role": "system", "content": system_prompt}]

    for h in reversed(history[1:]):
        role = h["role"] if h["role"] in ("user", "assistant") else "user"
        msgs.append({"role": role, "content": h["message"]})

    enriched_user_message = (
        f"{SEARCH_OVERRIDE_PROMPT}\n\n"
        f"LIVE SEARCH RESULTS FOR YOUR QUERY:\n"
        f"{'='*50}\n"
        f"{search_results}\n"
        f"{'='*50}\n\n"
        f"USER QUESTION: {user_message}\n\n"
        f"Now answer the USER QUESTION using the search results above. "
        f"Extract the direct answer from the results and state it confidently."
    )
    msgs.append({"role": "user", "content": enriched_user_message})
    return msgs


def build_messages_no_search(system_prompt: str, history: list, user_message: str) -> list:
    msgs = [{"role": "system", "content": system_prompt}]
    for h in reversed(history[1:]):
        role = h["role"] if h["role"] in ("user", "assistant") else "user"
        msgs.append({"role": role, "content": h["message"]})
    msgs.append({"role": "user", "content": user_message})
    return msgs


def get_redirect_uri():
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        return f"{render_url.rstrip('/')}/authorize"
    return "http://127.0.0.1:8000/authorize"

@app.route("/")
def landing():
    return render_template("landing.html")

@app.route("/login")
def login():
    redirect_uri = get_redirect_uri()
    print("REDIRECT URI:", redirect_uri)
    return oauth.google.authorize_redirect(redirect_uri)

@app.route("/authorize")
def authorize():
    try:
        token = oauth.google.authorize_access_token()
        user  = oauth.google.userinfo()
        session["user"] = {"email": user.get("email"), "name": user.get("name"), "picture": user.get("picture")}
        return redirect("/dashboard")
    except Exception as e:
        print("GOOGLE LOGIN ERROR:", e)
        return redirect("/?error=login_failed")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    if "user" not in session: return redirect("/login")
    return render_template("dashboard.html", user=session.get("user"))

@app.route("/chat")
def chat():
    return render_template("chat.html", user=session.get("user"))

@app.route("/email", methods=["GET", "POST"])
def email():
    if "user" not in session: return render_template("login_required.html")
    output = ""
    if request.method == "POST":
        topic  = request.form.get("topic", "")
        tone   = request.form.get("tone", "professional")
        output = run_ai([
            {"role": "system", "content": "Write a professional email with clear paragraphs. No markdown."},
            {"role": "user",   "content": f"Write a {tone} email about: {topic}"},
        ]) or "Couldn't generate email. Please try again."
        output = clean_text(re.sub(r"[*#_`]", "", output))
    return render_template("email.html", email=output)

@app.route("/resume", methods=["GET", "POST"])
def resume():
    if "user" not in session: return render_template("login_required.html")
    output = ""
    if request.method == "POST":
        name       = request.form.get("name", "").strip()
        role       = request.form.get("role", "").strip()
        skills     = request.form.get("skills", "").strip()
        experience = request.form.get("experience", "").strip()
        education  = request.form.get("education", "").strip()
        if not all([name, role, skills, experience, education]):
            return render_template("resume.html", resume="Please fill all fields!")
        prompt = f"Create a professional resume for:\nName: {name}\nTarget Role: {role}\nSkills: {skills}\nWork Experience: {experience}\nEducation: {education}\n\nFormat cleanly with sections: Summary, Skills, Experience, Education."
        output = run_ai([
            {"role": "system", "content": "You are an expert resume writer. Format cleanly. No markdown symbols."},
            {"role": "user",   "content": prompt},
        ]) or "Couldn't generate resume. Please try again."
        output = clean_text(re.sub(r"[*#_`]", "", output))
    return render_template("resume.html", resume=output)

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data         = request.get_json(force=True)
    user_message = safe_trim(data.get("message", "").strip())
    if not user_message:
        return jsonify({"reply": "Please send a message."}), 400

    user_email = session["user"]["email"] if "user" in session else "guest"
    db = get_db()
    try:
        db.execute("INSERT INTO chats(user_email,role,message) VALUES (?,?,?)", (user_email, "user", user_message))
        db.commit()

        memory = get_memory(user_email)

        name_match = re.search(r"my name is ([a-zA-Z ]+)", user_message, re.IGNORECASE)
        if name_match:
            save_memory(user_email, "name", name_match.group(1).strip().title())