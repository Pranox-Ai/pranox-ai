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
#  The AI reads the question and decides:
#  1. Does it need live search?
#  2. What is the best Google query?
#  This works for ANY question automatically.
# ─────────────────────────────────────────
def ai_search_decision(user_message: str) -> tuple:
    """
    Ask llama-3.1-8b-instant (fast + cheap) to decide:
    - Does this question need a live web search?
    - If yes, what Google query should we use?
    Returns: (needs_search: bool, search_query: str)
    """
    prompt = f"""You are a search decision engine. Analyze the user's question and decide:
1. Does it need a live web search?
2. If yes, write the best Google search query.

Search IS needed for:
- Current events, news, recent happenings
- People's current roles (CM, PM, CEO, president, minister, owner, etc.)
- Prices, stocks, crypto, fuel, gold rates
- Weather, sports scores, match results
- Location of places, restaurants, shops, hospitals, malls, branches
- Recently opened/launched things (new restaurants, stores, products)
- Anything that changes over time
- Any place + business query ("is there X in Y city", "where is X in Y")
- Company news, product launches, policy changes

Search is NOT needed for:
- Math calculations
- General coding / programming concepts
- Creative writing, poems, stories, essays
- Definitions of stable/historical concepts
- Historical facts that won't change
- Personal advice or opinions
- Grammar or language questions
- Explaining how something works (science, tech concepts)

User question: "{user_message}"

Reply in EXACTLY this format, no extra text:
SEARCH: YES or NO
QUERY: the Google search query if YES, else NONE"""

    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=80,
        )
        response = completion.choices[0].message.content.strip()
        lines = response.splitlines()

        search_line = next((l for l in lines if l.upper().startswith("SEARCH:")), "SEARCH: NO")
        query_line  = next((l for l in lines if l.upper().startswith("QUERY:")),  "QUERY: NONE")

        needs_search = "YES" in search_line.upper()
        query = query_line.split(":", 1)[1].strip() if ":" in query_line else ""
        query = "" if query.upper() == "NONE" else query

        if needs_search and not query:
            query = user_message  # fallback to raw message

        print(f"[AI DECISION] Search={needs_search} | Query='{query}'")
        return needs_search, query

    except Exception as e:
        print(f"[AI DECISION ERROR] {e} — defaulting to search")
        return True, user_message  # on error, always search to be safe

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
            print(f"[SEARCH] Serper returned {res.status_code}")
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

        # 4. Related (only if few results)
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
These are from TODAY and are MORE ACCURATE than your training data.

MANDATORY RULES — NO EXCEPTIONS:

1. READ the search results carefully. The answer is in there.
   - Use [TOP ANSWER] first if present.
   - Then [KNOWLEDGE GRAPH].
   - Then [SOURCE] snippets.

2. ANSWER DIRECTLY and CONFIDENTLY.
   - NEVER say "I couldn't find" when results are present.
   - NEVER say "my data may be outdated" — you have live data NOW.
   - NEVER say "please verify" as a substitute for answering.
   - NEVER say "as of my knowledge cutoff".
   - NEVER say "I don't have real-time access".

3. If something EXISTS in results, confirm it and share details.

4. For locations: give address, area, or any location detail from results.

5. Only if results are genuinely empty say:
   "I searched but couldn't find specific details — try Google Maps or the official website."

CORRECT: "McDonald's in Davangere is located at P.J. Extension."
WRONG:   "I couldn't find any information. Please check Google Maps."
══════════════════════════════════════════════
"""

def build_system_prompt(memory: str) -> str:
    base = BASE_SYSTEM_PROMPT
    base += f"\nUSER MEMORY:\n{memory}\n" if memory else "\nUSER MEMORY: No memory stored yet.\n"
    return base

def build_messages_with_search(system_prompt, search_results, history, user_message):
    msgs = [{"role": "system", "content": system_prompt}]
    for h in reversed(list(history)[1:]):
        role = h["role"] if h["role"] in ("user", "assistant") else "user"
        msgs.append({"role": role, "content": h["message"]})
    enriched = (
        f"{SEARCH_OVERRIDE_PROMPT}\n\n"
        f"LIVE SEARCH RESULTS:\n{'='*50}\n{search_results}\n{'='*50}\n\n"
        f"USER QUESTION: {user_message}\n\n"
        f"Answer using the search results above. Be direct and confident."
    )
    msgs.append({"role": "user", "content": enriched})
    return msgs

def build_messages_no_search(system_prompt, history, user_message):
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

        # ── UNIVERSAL AI-DRIVEN SEARCH DECISION ──────────────
        # The AI itself decides if search is needed and writes
        # the best Google query. Works for ANY question.
        do_search, search_query = ai_search_decision(user_message)
        search_results = ""

        if do_search and search_query:
            search_results = search_internet(search_query)
            # Fallback: if AI query returned nothing, try raw message
            if not search_results:
                print(f"[FALLBACK] Trying raw: '{user_message}'")
                search_results = search_internet(user_message)
        # ─────────────────────────────────────────────────────

        system_prompt = build_system_prompt(memory)

        if search_results.strip():
            msgs = build_messages_with_search(system_prompt, search_results, history, user_message)
        elif do_search and not search_results:
            no_result_note = (
                f"NOTE: Live web search was attempted but returned no results. "
                f"Give your best answer from training knowledge. "
                f"For location-specific or very recent questions, suggest the user check Google Maps or the official website.\n\n"
                f"USER QUESTION: {user_message}"
            )
            msgs = [{"role": "system", "content": system_prompt}]
            for h in reversed(list(history)[1:]):
                role = h["role"] if h["role"] in ("user", "assistant") else "user"
                msgs.append({"role": role, "content": h["message"]})
            msgs.append({"role": "user", "content": no_result_note})
        else:
            msgs = build_messages_no_search(system_prompt, history, user_message)

        reply = run_ai(msgs)
        if not reply:
            reply = "I ran into an issue generating a response. Please try again."

        if is_bad_response(reply):
            fallback = search_internet(user_message)
            reply = f"Here are some helpful resources:\n\n{fallback}" if fallback else "I couldn't process that. Could you rephrase your question?"

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

# ─────────────────────────────────────────
#  IMAGE VISION
# ─────────────────────────────────────────
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

# ─────────────────────────────────────────
#  TEXT EXTRACTION
# ─────────────────────────────────────────
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

# ─────────────────────────────────────────
#  FILE UPLOAD
# ─────────────────────────────────────────
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