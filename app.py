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

def search_internet(query):
    try:
        api_key = os.getenv("SERPER_API_KEY")
        if not api_key: return ""
        res = requests.post("https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": 5}, timeout=5)
        data = res.json()
        results = []
        if "answerBox" in data:
            ab = data["answerBox"]
            answer = ab.get("answer") or ab.get("snippet") or ab.get("title", "")
            if answer:
                results.append(f"[Direct Answer]: {answer}")
        if "organic" in data:
            for r in data["organic"][:5]:
                results.append(f"{r['title']}: {r['link']}\n  {r.get('snippet','')}")
        return "\n\n".join(results)
    except Exception as e:
        print("SERPER ERROR:", e)
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
                model=MODELS[i], messages=messages, temperature=0.7, max_tokens=max_tokens)
            return completion.choices[0].message.content.strip()
        except Exception as e:
            print(f"AI ERROR (model {MODELS[i]}):", e)
    return None

def is_bad_response(reply):
    if not reply: return True
    bad_patterns = ["here's the corrected code", "flask application", "example of how you could", "missing code"]
    return any(p in reply.lower() for p in bad_patterns)

# ═══════════════════════════════════════════
#  SMART SEARCH TRIGGER
#  Determines whether a query needs live web search
# ═══════════════════════════════════════════

# Keywords that always trigger a search
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
]

# Regex patterns that always trigger a search
SEARCH_PATTERNS = [
    r"\bwho is (the )?(current |new |latest )?(cm|chief minister|pm|prime minister|president|ceo|founder|owner|governor|minister|mayor|chancellor|king|queen|coo|cto|cfo)\b",
    r"\bwhat is (the )?(current |latest |new )?(price|rate|status|situation|update)\b",
    r"\b(cm|chief minister|pm|prime minister|president|ceo) of \w+",
    r"\bcurrent (government|ruling party|leader|head)\b",
    r"\blatest (news|update|development|result)\b",
    r"\brecently (happened|announced|launched|released|arrested|elected)\b",
    r"\bwho (leads?|runs?|heads?|controls?|owns?) \w+",
    r"\bis \w+ (still|currently|now)\b",
    r"\bwhat happened (to|with|in|at)\b",
    r"\bhow much (does|is|are|did)\b",
]

def needs_live_search(message: str) -> bool:
    """Return True if the query requires live internet search."""
    msg_lower = message.lower()

    # Check plain keywords
    if any(kw in msg_lower for kw in SEARCH_KEYWORDS):
        return True

    # Check regex patterns
    for pattern in SEARCH_PATTERNS:
        if re.search(pattern, msg_lower):
            return True

    return False

def build_system_prompt(memory: str, has_search_results: bool = False) -> str:
    search_instruction = ""
    if has_search_results:
        search_instruction = """
========================
LIVE SEARCH RESULTS (CRITICAL)
========================
Live web search results have been provided to you in this conversation.
- You MUST use these search results to answer the user's question.
- The search results are MORE ACCURATE than your training data for current/recent information.
- Always prefer search result data over anything from your training.
- If the answer is clearly in the search results, state it directly and confidently.
- Do NOT say "I don't have real-time data" or "as of my knowledge cutoff" when search results are available.
- Cite from the search results naturally (e.g., "According to recent sources..." or "Latest information shows...").
"""

    return f"""You are Pranox AI — a next-generation AI assistant, highly intelligent, friendly, and precise.

========================
IDENTITY
========================
Founder of Pranox AI:
Chetansaipranav R

Created in:
January 2026

If user asks:
- who created you
- who is your founder
- tell me about pranox

Always answer clearly:
"Chetansaipranav R is the founder of Pranox AI. He created it in January 2026."

========================
THINKING (IMPORTANT)
========================
- Understand the question deeply
- Break into logical steps internally
- Do NOT show reasoning or thinking steps
- Give only the final clean answer

========================
REAL-TIME INFORMATION (CRITICAL)
========================
Your training data has a knowledge cutoff and may be OUTDATED for:
- Political leaders (CM, PM, President, Ministers)
- Company CEOs, founders, owners
- Sports scores and results
- Stock prices, crypto rates
- Recent news and events
- Election results
- Government policies

For ALL such questions:
1. Use the live search results provided (if available)
2. If no search results: clearly tell the user your data may be outdated and suggest they verify
3. NEVER confidently state outdated information as current fact
4. NEVER say a person holds a position if search results contradict it
{search_instruction}

========================
GREETING RULE (IMPORTANT)
========================
- If user says greetings like:
  "hi", "hello", "hey", "hii", "good morning", "good evening"

Then:
- Respond with a friendly greeting
- If user's name exists in memory → include it

Examples:
- "Hi! How can I help you today?"
- "Hello Pranav! What can I do for you?"
- "Hey there! Need any help?"

Rules:
- Never say bye for greetings
- Never give weird or unrelated responses
- Even if user repeats greetings, respond politely
- Keep it short and natural

========================
PERSONALIZATION (IMPORTANT)
========================
- If user's name is available in memory:
  Use it naturally in responses — mainly in greetings or first line
- Do NOT overuse the name
- If user tells their name: respond like "Nice to meet you <name>!"

========================
CONTEXT
========================
- Use previous conversation
- Maintain continuity
- Do not repeat same answer unnecessarily

========================
CODE RULES
========================
- Always give proper code blocks
- Clean indentation
- Separate explanation and code

========================
FOLLOW-UP
========================
- Suggest 1-2 useful follow-up questions (only if relevant)

========================
IMPORTANT RULES
========================
- Never give messy output
- Keep answers clean and readable

========================
PRANOX LINKS (STRICT)
========================
Instagram: https://www.instagram.com/pranoxgroups
X: https://x.com/Pranoxgroups
LinkedIn: https://www.linkedin.com/in/chetansaipranav-r-a6b18333b
Product Hunt: https://www.producthunt.com/@pranoxai

ONLY share these when user asks about Pranox AI social media or official pages.
NEVER mix Pranox links with other topics.

========================
EMAIL (STRICT)
========================
Email: pranoxoffical@gmail.com
ONLY provide when user explicitly asks for contact/email info.

========================
BEHAVIOR
========================
- Friendly, smart, helpful
- Never robotic
- Keep answers clean and structured
- Use bullet points only when helpful
- For greetings: respond warmly and briefly

========================
RESPONSE FORMAT
========================
1. Clear explanation first
2. Bullets when needed
3. Clean spacing
4. Code in proper code blocks
5. Suggest follow-up questions when relevant

========================
MEMORY
========================
{memory if memory else "No memory stored yet."}
"""

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

        # Auto-extract name from message
        name_match = re.search(r"my name is ([a-zA-Z ]+)", user_message, re.IGNORECASE)
        if name_match:
            save_memory(user_email, "name", name_match.group(1).strip().title())
            memory = get_memory(user_email)

        # Auto-extract age from message
        age_match = re.search(r"i(?:'m| am) (\d+) years? old", user_message, re.IGNORECASE)
        if age_match:
            save_memory(user_email, "age", age_match.group(1))
            memory = get_memory(user_email)

        # Fetch chat history
        history = db.execute(
            "SELECT role,message FROM chats WHERE user_email=? ORDER BY id DESC LIMIT 20",
            (user_email,)
        ).fetchall()
        history = [h for h in history if "couldn't fully process" not in h["message"].lower()]

        # ── SMART SEARCH ─────────────────────────────────────
        do_search      = needs_live_search(user_message)
        search_results = ""
        if do_search:
            print(f"[SEARCH TRIGGERED] Query: {user_message}")
            search_results = search_internet(user_message)
            if search_results:
                print(f"[SEARCH RESULTS] {search_results[:200]}...")
            else:
                print("[SEARCH] No results returned")
        # ─────────────────────────────────────────────────────

        has_results = bool(search_results.strip())

        # Build messages — inject search BEFORE user message so model sees it as context
        msgs = [{"role": "system", "content": build_system_prompt(memory, has_search_results=has_results)}]

        # Inject search results as a system message right before the conversation
        if has_results:
            msgs.append({
                "role": "system",
                "content": (
                    f"[LIVE WEB SEARCH RESULTS — use these to answer the user's question accurately]\n\n"
                    f"{search_results}\n\n"
                    f"These results are from a real-time search. Trust them over your training data."
                )
            })

        # Add conversation history
        for h in reversed(history):
            role = h["role"] if h["role"] in ("user", "assistant") else "user"
            msgs.append({"role": role, "content": h["message"]})

        # Add current user message
        msgs.append({"role": "user", "content": user_message})

        reply = run_ai(msgs)
        if not reply:
            reply = "I ran into an issue generating a response. Please try again."

        if is_bad_response(reply):
            links = search_internet(user_message)
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
