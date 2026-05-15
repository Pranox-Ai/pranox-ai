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
            memory = get_memory(user_email)

        age_match = re.search(r"i(?:'m| am) (\d+) years? old", user_message, re.IGNORECASE)
        if age_match:
            save_memory(user_email, "age", age_match.group(1))
            memory = get_memory(user_email)

        history = db.execute(
            "SELECT role,message FROM chats WHERE user_email=? ORDER BY id DESC LIMIT 20",
            (user_email,)
        ).fetchall()
        history = [h for h in history if "couldn't fully process" not in h["message"].lower()]

        # ── SMART SEARCH WITH QUERY REWRITING ────────────────
        do_search      = needs_live_search(user_message)
        search_results = ""
        if do_search:
            # CRITICAL FIX: rewrite query before sending to Serper
            clean_query = rewrite_search_query(user_message)
            print(f"[SEARCH TRIGGERED] Raw: '{user_message}' | Clean: '{clean_query}'")
            search_results = search_internet(clean_query)

            # Fallback: if rewritten query returned nothing, try a simpler strip
            if not search_results:
                fallback = re.sub(
                    r'\b(tell me|please|kindly|can you|i want to know|what is the|who is the|the latest|the current)\b',
                    '', user_message, flags=re.IGNORECASE
                ).strip()
                if fallback and fallback.lower() != user_message.lower():
                    print(f"[SEARCH FALLBACK] Trying: '{fallback}'")
                    search_results = search_internet(fallback)
        # ─────────────────────────────────────────────────────

        system_prompt = build_system_prompt(memory)

        if search_results.strip():
            msgs = build_messages_with_search(system_prompt, search_results, history, user_message)
        elif do_search and not search_results:
            no_result_note = (
                f"NOTE: A live web search was attempted for this query but returned no results. "
                f"Your training data may be outdated for this question. "
                f"Please give your best answer but clearly tell the user to verify the information independently.\n\n"
                f"USER QUESTION: {user_message}"
            )
            msgs = [{"role": "system", "content": system_prompt}]
            for h in reversed(history[1:]):
                role = h["role"] if h["role"] in ("user", "assistant") else "user"
                msgs.append({"role": role, "content": h["message"]})
            msgs.append({"role": "user", "content": no_result_note})
        else:
            msgs = build_messages_no_search(system_prompt, history, user_message)

        reply = run_ai(msgs)
        if not reply:
            reply = "I ran into an issue generating a response. Please try again."

        if is_bad_response(reply):
            links = search_internet(rewrite_search_query(user_message))
            reply = f"Here are some helpful resources:\n\n{links}" if links else "I couldn't process that. Could you rephrase your question?"

        reply = re.sub(r"\n{3,}", "\n\n", reply).strip()

        if "user" not in session and "login_nudge" not in session:
            reply += "\n\n💡 *Login to save your chat history and get personalized responses*"
            session["login_nudge"] = True

        if "couldn't fully process" not in reply.lower():
            db.execute("INSERT INTO chats(user_email,role,message) VALUES (?,?,?)", (user_email, "assistant", reply))
            db.commit()

        return jsonify({"reply": reply})

    except Exception as e:
        print("CHAT ERROR:", e)
        return jsonify({"reply": "An unexpected error occurred. Please try again."}), 500
    finally:
        db.close()

# ═══════════════════════════════════════════
#  IMAGE VISION — Groq multimodal
# ═══════════════════════════════════════════
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
IMAGE_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
    ".bmp": "image/png", ".tiff": "image/png", ".tif": "image/png",
}
VISION_MODELS = [
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
]

def analyse_image(image_bytes, ext, user_question=""):
    mime = IMAGE_MIME.get(ext, "image/jpeg")
    if ext in (".bmp", ".tiff", ".tif"):
        try:
            img = Image.open(BytesIO(image_bytes)).convert("RGB")
            buf = BytesIO(); img.save(buf, format="PNG")
            image_bytes = buf.getvalue(); mime = "image/png"
        except Exception as e:
            print("Image conversion error:", e)
    b64      = base64.standard_b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime};base64,{b64}"
    prompt   = (
        user_question.strip() if user_question.strip()
        else "Describe this image in full detail. List every object, person, text, chart or scene visible. If there is readable text, transcribe it exactly."
    )
    for model in VISION_MODELS:
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": prompt},
                ]}],
                max_tokens=1024, temperature=0.5,
            )
            return completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"Vision error ({model}):", e)
    return ""

# ═══════════════════════════════════════════
#  TEXT EXTRACTION from non-image files
# ═══════════════════════════════════════════
def extract_text(file_bytes, ext):
    text = ""

    if ext == ".pdf":
        try:
            with pdfplumber.open(BytesIO(file_bytes)) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t: text += t + "\n"
        except Exception as e: print("PDF error:", e)

    elif ext in (".docx", ".doc"):
        try:
            from docx import Document
            doc   = Document(BytesIO(file_bytes))
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    rt = "\t".join(c.text for c in row.cells if c.text.strip())
                    if rt.strip(): parts.append(rt)
            text = "\n".join(parts)
        except Exception as e:
            print("python-docx error:", e)
            try:
                import docx2txt
                with tempfile.NamedTemporaryFile(delete=False, suffix=".docx") as tmp:
                    tmp.write(file_bytes); tmp_path = tmp.name
                text = docx2txt.process(tmp_path); os.unlink(tmp_path)
            except Exception as e2: print("docx2txt error:", e2)

    elif ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
            wb    = openpyxl.load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
            parts = []
            for sn in wb.sheetnames:
                ws = wb[sn]; parts.append(f"[Sheet: {sn}]")
                for row in ws.iter_rows(values_only=True):
                    rs = "\t".join(str(c) if c is not None else "" for c in row)
                    if rs.strip(): parts.append(rs)
            text = "\n".join(parts)
        except Exception as e: print("openpyxl error:", e)

    elif ext == ".xls":
        try:
            import xlrd
            wb    = xlrd.open_workbook(file_contents=file_bytes)
            parts = []
            for sheet in wb.sheets():
                parts.append(f"[Sheet: {sheet.name}]")
                for ri in range(sheet.nrows):
                    rs = "\t".join(str(sheet.cell_value(ri, ci)) for ci in range(sheet.ncols))
                    if rs.strip(): parts.append(rs)
            text = "\n".join(parts)
        except Exception as e: print("xlrd error:", e)

    elif ext == ".ods":
        try:
            import pandas as pd
            with tempfile.NamedTemporaryFile(delete=False, suffix=".ods") as tmp:
                tmp.write(file_bytes); tmp_path = tmp.name
            df_dict = pd.read_excel(tmp_path, engine="odf", sheet_name=None)
            os.unlink(tmp_path)
            parts = []
            for sheet, df in df_dict.items():
                parts.append(f"[Sheet: {sheet}]"); parts.append(df.to_string(index=False))
            text = "\n\n".join(parts)
        except Exception as e: print("ODS error:", e)

    elif ext in (".pptx", ".ppt"):
        try:
            from pptx import Presentation
            prs   = Presentation(BytesIO(file_bytes))
            parts = []
            for i, slide in enumerate(prs.slides, 1):
                parts.append(f"[Slide {i}]")
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        parts.append(shape.text.strip())
            text = "\n".join(parts)
        except Exception as e: print("PPTX error:", e)

    elif ext == ".rtf":
        try:
            from striprtf.striprtf import rtf_to_text
            text = rtf_to_text(file_bytes.decode("utf-8", errors="ignore"))
        except Exception as e: print("RTF error:", e)

    elif ext in (".csv", ".tsv"):
        try:
            import pandas as pd
            sep = "\t" if ext == ".tsv" else ","
            df  = pd.read_csv(BytesIO(file_bytes), sep=sep)
            text = df.to_string(index=False)
        except Exception as e:
            print("CSV error:", e)
            text = file_bytes.decode("utf-8", errors="ignore")

    elif ext in (".json", ".jsonl"):
        try:
            raw = file_bytes.decode("utf-8", errors="ignore")
            text = json.dumps(json.loads(raw), indent=2) if ext == ".json" else "\n".join(raw.splitlines()[:50])
        except Exception as e:
            print("JSON error:", e)
            text = file_bytes.decode("utf-8", errors="ignore")[:4000]

    elif ext == ".ipynb":
        try:
            nb    = json.loads(file_bytes.decode("utf-8", errors="ignore"))
            parts = []
            for cell in nb.get("cells", []):
                src = "".join(cell.get("source", []))
                if src.strip(): parts.append(f"[{cell.get('cell_type','').upper()}]\n{src}")
            text = "\n\n".join(parts)
        except Exception as e: print("IPYNB error:", e)

    elif ext == ".odt":
        try:
            import odf.opendocument
            with tempfile.NamedTemporaryFile(delete=False, suffix=".odt") as tmp:
                tmp.write(file_bytes); tmp_path = tmp.name
            doc = odf.opendocument.load(tmp_path); os.unlink(tmp_path)
            parts = []
            for el in doc.text.childNodes:
                s = el.plaintext() if hasattr(el, "plaintext") else str(el)
                if s.strip(): parts.append(s.strip())
            text = "\n".join(parts)
        except Exception as e: print("ODT error:", e)

    elif ext == ".xml":
        text = "XML Data:\n" + file_bytes.decode("utf-8", errors="ignore")

    elif ext == ".zip":
        try:
            import zipfile
            parts = []
            with zipfile.ZipFile(BytesIO(file_bytes)) as zf:
                for name in zf.namelist():
                    if name.endswith((".txt",".md",".csv",".json",".xml",".py",".js",".html",".css",".yaml",".yml",".sql")):
                        try:
                            content = zf.read(name).decode("utf-8", errors="ignore")
                            parts.append(f"[File: {name}]\n{content}")
                        except Exception: pass
            text = "\n\n".join(parts)
        except Exception as e: print("ZIP error:", e)

    else:
        text = file_bytes.decode("utf-8", errors="ignore")

    return text.strip()

# ═══════════════════════════════════════════
#  FILE UPLOAD ROUTE
# ═══════════════════════════════════════════
@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"reply": "No file received. Please try again."})

    file     = request.files["file"]
    filename = file.filename.lower()
    _, ext   = os.path.splitext(filename)

    try:
        file_bytes = file.read()
    except Exception as e:
        print("FILE READ ERROR:", e)
        return jsonify({"reply": "Could not read the uploaded file. Please try again."})

    user_question = request.form.get("message", "").strip()
    user_email    = session["user"]["email"] if "user" in session else "guest"

    if ext in IMAGE_EXTENSIONS:
        reply = analyse_image(file_bytes, ext, user_question)
        if not reply:
            reply = "I couldn't analyse the image. Please try again or paste any text from it directly into the chat."
    else:
        text = extract_text(file_bytes, ext)
        if not text:
            return jsonify({"reply": "I opened the file but couldn't extract readable content. If it is a scanned document, try uploading as a PNG or JPG image instead."})

        text        = safe_trim(clean_text(text), 6000)
        instruction = user_question if user_question else "Summarise the key information from this file clearly and concisely."
        msgs = [
            {"role": "system", "content": "You are Pranox AI. A file has been uploaded. Use the extracted content below to answer the user. Be clear, structured, and helpful."},
            {"role": "user",   "content": f"File content:\n\n{text}\n\nUser request: {instruction}"},
        ]
        reply = run_ai(msgs, max_tokens=1200)
        if not reply: reply = "I read the file but had trouble generating a response. Please try again."
        reply = re.sub(r"\n{3,}", "\n\n", reply).strip()

    db = get_db()
    try:
        db.execute("INSERT INTO chats(user_email,role,message) VALUES (?,?,?)", (user_email, "user", f"[File: {file.filename}] {user_question}"))
        db.execute("INSERT INTO chats(user_email,role,message) VALUES (?,?,?)", (user_email, "assistant", reply))
        db.commit()
    finally:
        db.close()

    return jsonify({"reply": reply})

@app.route("/download_resume", methods=["POST"])
def download_resume():
    buffer = BytesIO()
    p      = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 50
    p.setFont("Helvetica-Bold", 14)
    p.drawString(50, y, "Resume")
    y -= 30; p.setFont("Helvetica", 11)
    for line in request.form["resume"].split("\n"):
        if y < 60:
            p.showPage(); y = height - 50; p.setFont("Helvetica", 11)
        line = line.strip()
        if line: p.drawString(50, y, line[:100]); y -= 18
        else: y -= 8
    p.save(); buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="pranox_resume.pdf", mimetype="application/pdf")

@app.route("/api/image", methods=["POST"])
def generate_image():
    data   = request.get_json(force=True)
    prompt = data.get("prompt", "").strip()
    if not prompt: return jsonify({"reply": "Please provide an image description."}), 400
    token   = CLOUDFLARE_API_TOKEN.strip()
    account = CLOUDFLARE_ACCOUNT_ID.strip()
    if not token or not account:
        return jsonify({"reply": "Image generation is not configured."}), 500
    enhanced = f"ultra realistic, 4k, highly detailed, sharp focus, professional photography, {prompt}"
    url      = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/@cf/stabilityai/stable-diffusion-xl-base-1.0"
    headers  = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        response = requests.post(url, headers=headers, json={"prompt": enhanced, "num_steps": 20}, timeout=60)
        if response.status_code == 200:
            ct = response.headers.get("Content-Type", "")
            if "image" in ct or len(response.content) > 1000:
                return response.content, 200, {"Content-Type": "image/png", "Cache-Control": "no-cache"}
            return jsonify({"reply": "Image generation returned unexpected data. Please try again."}), 500
        elif response.status_code == 401:
            return jsonify({"reply": "Image generation auth failed. Check your Cloudflare API token."}), 500
        elif response.status_code == 403:
            return jsonify({"reply": "Image generation permission denied. Verify your Cloudflare account ID and token permissions."}), 500
        else:
            return jsonify({"reply": f"Image generation failed (error {response.status_code})."}), 500
    except requests.Timeout:
        return jsonify({"reply": "Image generation timed out. Please try again."}), 504
    except Exception as e:
        print("IMAGE ERROR:", e)
        return jsonify({"reply": "Server error during image generation."}), 500

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

@app.route("/sitemap.xml")
def sitemap():
    return send_from_directory("static", "sitemap.xml")

@app.route("/robots.txt")
def robots():
    return send_from_directory("static", "robots.txt")

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    app.run(debug=False, host="0.0.0.0", port=port)
