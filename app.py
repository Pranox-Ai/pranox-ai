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

# ─────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────
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
    db.execute("DELETE FROM user_email=? AND key=?", (user_email, key))
    db.execute("INSERT INTO user_memory(user_email,key,value) VALUES (?,?,?)", (user_email, key, value))
    db.commit(); db.close()

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

# ─────────────────────────────────────────
#  UNIVERSAL SEARCH DECISION — AI decides
#  Uses a tiny fast model call to decide if search is needed
#  and to generate a clean search query.
#  This works for ANY question, not just ones we manually listed.
# ─────────────────────────────────────────
def ai_search_decision(user_message: str) -> tuple[bool, str]:
    """
    Ask the AI two things in one call:
    1. Does this question need a live web search?
    2. If yes, what is the best Google search query to use?

    Returns: (needs_search: bool, search_query: str)
    """
    prompt = f"""You are a search decision engine. Analyze the user's question and decide:

1. Does it need a live web search to answer correctly?
2. If yes, write the best Google search query for it.

Search IS needed for:
- Current events, news, recent happenings
- People's current roles (CM, PM, CEO, president, minister, etc.)
- Prices, stocks, crypto, exchange rates
- Weather, sports scores, match results
- Location of places, restaurants, shops, hospitals, malls
- Recently opened/launched things (new restaurants, products, stores)
- Anything that changes over time
- Any specific place + business query ("is there X in Y city")
- Any question about what exists somewhere right now

Search is NOT needed for:
- Math calculations
- General coding help or programming concepts  
- Creative writing, poems, stories
- Definitions of stable concepts
- Historical facts (things that won't change)
- Personal advice or opinions
- Grammar, language questions

User question: "{user_message}"

Reply in EXACTLY this format, nothing else:
SEARCH: YES or NO
QUERY: (the Google search query if YES, else NONE)

Examples:
User: "where is mcd in davangere"
SEARCH: YES
QUERY: McDonald's Davangere location

User: "who is pm of india"
SEARCH: YES
QUERY: Prime Minister of India 2025

User: "write a python function to sort a list"
SEARCH: NO
QUERY: NONE

User: "what is the capital of france"
SEARCH: NO
QUERY: NONE

User: "latest ipl score"
SEARCH: YES
QUERY: IPL latest score today 2025

User: "is there a kfc in mysore"
SEARCH: YES
QUERY: KFC Mysore location branch"""

    try:
        # Use the fast small model for this decision — saves quota
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=60,
        )
        response = completion.choices[0].message.content.strip()
        lines = response.strip().splitlines()

        search_line = next((l for l in lines if l.upper().startswith("SEARCH:")), "SEARCH: NO")
        query_line  = next((l for l in lines if l.upper().startswith("QUERY:")),  "QUERY: NONE")

        needs_search = "YES" in search_line.upper()
        query = query_line.split(":", 1)[1].strip() if ":" in query_line else ""
        query = "" if query.upper() == "NONE" else query

        # Fallback: if needs_search but query is empty, use the original message
        if needs_search and not query:
            query = user_message

        print(f"[AI DECISION] Search: {needs_search} | Query: '{query}'")
        return needs_search, query

    except Exception as e:
        print(f"[AI DECISION ERROR] {e} — defaulting to search")
        # On any error, default to searching with original message
        return True, user_message


# ─────────────────────────────────────────
#  SERPER SEARCH
# ─────────────────────────────────────────
def search_internet(query: str) -> str:
    try:
        api_key = os.getenv("SERPER_API_KEY")
        if not api_key:
            print("[SEARCH] SERPER_API_KEY not set")
            return ""

        res = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 8, "gl": "in", "hl": "en"},
            timeout=8,
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
            kg    = data["knowledgeGraph"]
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
                    results.append(f"[SOURCE: {title}]\n  {snippet}\n  Link: {link}")

        # 4. Related searches (only if few results)
        if "relatedSearches" in data and len(results) < 3:
            related = [r.get("query", "") for r in data["relatedSearches"][:3]]
            if related:
                results.append(f"[Related]: {', '.join(related)}")

        final = "\n\n".join(results)
        print(f"[SEARCH SUCCESS] {len(results)} results for: '{query}'")
        return final

    except requests.Timeout:
        print("[SEARCH] Timeout")
        return ""
    except Exception as e:
        print(f"[SEARCH ERROR]: {e}")
        return ""

# ─────────────────────────────────────────
#  UTILITIES
# ─────────────────────────────────────────
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

# ─────────────────────────────────────────
#  SYSTEM PROMPTS
# ─────────────────────────────────────────
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
🔴 LIVE SEARCH MODE — STRICT RULES 🔴
══════════════════════════════════════════════

Real-time web search results are provided below.
These results are from TODAY. They are MORE ACCURATE than your training data.

MANDATORY RULES — NO EXCEPTIONS:

1. READ the search results and EXTRACT the answer from them.
   - Check [TOP ANSWER] first — use it directly if present.
   - Then check [KNOWLEDGE GRAPH].
   - Then read [SOURCE] snippets carefully.

2. STATE THE ANSWER DIRECTLY and CONFIDENTLY.
   - NEVER say "I couldn't find" when search results are present.
   - NEVER say "my data may be outdated" — you have live data RIGHT NOW.
   - NEVER say "please verify" as a substitute for answering.
   - NEVER say "as of my knowledge cutoff".
   - NEVER say "I don't have real-time access".

3. If something EXISTS in search results, confirm it and share the details.

4. For location queries: give the address, area, or any location detail from results.

5. Only if results are genuinely empty/irrelevant say:
   "I searched but couldn't find specific details — try Google Maps or the official website."

CORRECT EXAMPLES:
- "McDonald's in Davangere is located at P.J. Extension."
- "The Chief Minister of West Bengal is Mamata Banerjee."
- "As of today, the IPL score is..."

WRONG (NEVER DO THIS):
- "I couldn't find any information on McDonald's in Davangere."
- "My data may be outdated, please verify."
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
    for h in reversed(list(history)[1:]):
        role = h["role"] if h["role"] in ("user", "assistant") else "user"
        msgs.append({"role": role, "content": h["message"]})
    enriched = (
        f"{SEARCH_OVERRIDE_PROMPT}\n\n"
        f"LIVE SEARCH RESULTS:\n"
        f"{'='*50}\n"
        f"{search_results}\n"
        f"{'='*50}\n\n"
        f"USER QUESTION: {user_message}\n\n"
        f"Answer the USER QUESTION using the search results above. Be direct and confident."
    )
    msgs.append({"role": "user", "content": enriched})
    return msgs


def build_messages_no_search(system_prompt: str, history: list, user_message: str) -> list:
    msgs = [{"role": "system", "content": system_prompt}]
    for h in reversed(list(history)[1:]):
        role = h["role"] if h["role"] in ("user", "assistant") else "user"
        msgs.append({"role": role, "content": h["message"]})
    msgs.append({"role": "user", "content": user_message})
    return msgs


def get_redirect_uri():
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        return f"{render_url.rstrip('/')}/authorize"
    return "http://127.0.0.1:8000/authorize"

# ─────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────
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

# ─────────────────────────────────────────
#  MAIN CHAT API
# ─────────────────────────────────────────
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

        # ── UNIVERSAL SEARCH DECISION ──────────────────────────
        # The AI itself decides if search is needed and writes the query.
        # This handles ANY question universally — no manual keyword lists needed.
        do_search, search_query = ai_search_decision(user_message)
        search_results = ""

        if do_search and search_query:
            search_results = search_internet(search_query)

            # Fallback: if AI query returned nothing, try raw user message
            if not search_results:
                print(f"[SEARCH FALLBACK] Trying raw message: '{user_message}'")
                search_results = search_internet(user_message)
        # ─────────────────────────────────────────────────────

        system_prompt = build_system_prompt(memory)

        if search_results.strip():
            msgs = build_messages_with_search(system_prompt, search_results, history, user_message)
        elif do_search and not search_results:
            # Search was needed but returned nothing
            no_result_note = (
                f"NOTE: A live web search was attempted but returned no results. "
                f"Give your best answer from training knowledge. "
                f"For location-specific or very recent questions, suggest the user check Google Maps or the official website.\n\n"
                f"USER QUESTION: {user_message}"
            )
            msgs = [{"role": "system", "content": system_prompt}]
            for h in reversed(list(history)[1:]):
                role = h["role"] if h["role"] in ("user", "assis