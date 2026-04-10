from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file, send_from_directory
import os
from dotenv import load_dotenv
from auth import oauth, init_oauth
from groq import Groq
from io import BytesIO
from reportlab.pdfgen import canvas
import sqlite3
import requests
import re

# FILE SUPPORT
import pdfplumber
from PIL import Image
import pytesseract

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY","dev-secret-key")

init_oauth(app)
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ================= DATABASE =================
def get_db():
    conn = sqlite3.connect("pranox.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    db = get_db()

    db.execute("""
    CREATE TABLE IF NOT EXISTS chats(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT,
        role TEXT,
        message TEXT
    )
    """)

    db.execute("""
    CREATE TABLE IF NOT EXISTS user_memory(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_email TEXT,
        key TEXT,
        value TEXT
    )
    """)

    db.commit()

init_db()

# ================= MEMORY =================
def save_memory(user_email, key, value):
    db = get_db()
    db.execute("INSERT INTO user_memory(user_email,key,value) VALUES (?,?,?)",
               (user_email, key, value))
    db.commit()

def get_memory(user_email):
    db = get_db()
    rows = db.execute("SELECT key,value FROM user_memory WHERE user_email=?",
                      (user_email,)).fetchall()
    return "\n".join([f"{r['key']}: {r['value']}" for r in rows])

# ================= SEARCH =================
def search_internet(query):
    try:
        api_key = os.getenv("SERPER_API_KEY")
        if not api_key:
            return ""

        res = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query},
            timeout=5
        )

        data = res.json()

        results=[]
        if "organic" in data:
            for r in data["organic"][:5]:
                results.append(f"{r['title']}: {r['link']}")
        return "\n".join(results)

    except:
        return ""
# ================= 🔥 LARGE INPUT FIX (ADD ONLY) =================
def safe_trim(text, limit=4000):
    if len(text) <= limit:
        return text
    return text[:limit]

# ================= AI =================
def run_ai(messages):
    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            temperature=0.7,
            max_tokens=900
        )
        return completion.choices[0].message.content.strip()

    except Exception as e:
        print("AI ERROR:", e)
        return None   # 🔥 IMPORTANT CHANGE (no error string)
    
def is_bad_response(reply):
        if not reply:
             return True

        bad_patterns = [
            "here's the corrected code",
            "it seems like",
            "flask application",
            "example of how you could",
            "missing code",
        ]

        reply_lower = reply.lower()

        for pattern in bad_patterns:
            if pattern in reply_lower:
                return True

        return False
    
# ================= ROUTES =================
@app.route("/")
def landing():
    return render_template("landing.html"),
200

@app.route("/login")
def login():
    return oauth.google.authorize_redirect(url_for("authorize", _external=True))

@app.route("/authorize")
def authorize():
    oauth.google.authorize_access_token()
    user = oauth.google.get("https://openidconnect.googleapis.com/v1/userinfo").json()
    session["user"] = user
    return redirect("/dashboard")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")
    return render_template("dashboard.html", user=session["user"])

@app.route("/chat")
def chat():
    return render_template("chat.html")

# ================= EMAIL =================
@app.route("/email", methods=["GET","POST"])
def email():
    if "user" not in session:
        return redirect("/login")

    output=""
    if request.method=="POST":
        topic=request.form.get("topic")
        tone=request.form.get("tone")

        output = run_ai([
            {"role":"system","content":"Write a professional email with clear paragraphs. No markdown."},
            {"role":"user","content":f"{tone} email about {topic}"}
        ])

        # 🔥 ADD THIS
        if not output:
            output = "Couldn't generate email. Please try again."

        output=re.sub(r"\n{3,}", "\n\n", output)
        output=re.sub(r"[ \t]+", " ", output)
        output=re.sub(r"[*#_`]", "", output)

    return render_template("email.html", email=output)

# ================= RESUME =================
@app.route("/resume", methods=["GET","POST"])
def resume():
    if "user" not in session:
        return redirect("/login")

    output=""
    if request.method=="POST":
        prompt=f"""
Create a professional resume.

Name: {request.form.get("name")}
Role: {request.form.get("role")}
Skills: {request.form.get("skills")}
Experience: {request.form.get("experience")}
Education: {request.form.get("education")}
"""

        output = run_ai([
            {"role":"system","content":"Professional resume writer. Clean format."},
            {"role":"user","content":prompt}
        ])

        # 🔥 ADD THIS
        if not output:
            output = "Couldn't generate resume. Please try again."

        output=re.sub(r"\n{3,}", "\n\n", output)
        output=re.sub(r"[ \t]+", " ", output)
        output=re.sub(r"[*#_`]", "", output)

    return render_template("resume.html", resume=output)

# ================= CHAT =================
@app.route("/api/chat", methods=["POST"])
def api_chat():

    if "user" not in session:
        return jsonify({"reply":"Login required"})

    user_message = request.json.get("message","").strip()
    user_message = safe_trim(user_message)   # 🔥 ADD THIS
    user_email = session["user"]["email"]

    db = get_db()
    db.execute(
            "INSERT INTO chats(user_email,role,message) VALUES (?,?,?)",
            (user_email,"user",user_message)
        )


    db.commit()

    memory = get_memory(user_email)

    if "my name is" in user_message.lower():
        match = re.search(r"my name is ([a-zA-Z ]+)", user_message, re.IGNORECASE)
        if match:
            name = match.group(1).strip().title()
            save_memory(user_email, "name", name)

    history = db.execute(
        "SELECT role,message FROM chats WHERE user_email=? ORDER BY id DESC LIMIT 15",
        (user_email,)
    ).fetchall()

    # 🔥 REMOVE BAD RESPONSES FROM HISTORY
    history = [h for h in history if "couldn't fully process" not in h["message"].lower()]

    search_results = search_internet(user_message)

    # ✅ YOUR ORIGINAL RULES (UNCHANGED)
    # ✅ YOUR ORIGINAL RULES (UNCHANGED - only small fix applied)
    messages = [{
        "role": "system",
        "content": f"""
You are Pranox AI — an advanced assistant similar to ChatGPT.

========================
🔹 IDENTITY
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
🔹 PRANOX LINKS (STRICT CONTROL)
========================

Instagram: https://www.instagram.com/pranoxgroups?utm_source=ig_web_button_share_sheet&igsh=ZDNlZDc0MzIxNw==

X: https://x.com/Pranoxgroups

Linkedin:
https://www.linkedin.com/in/chetansaipranav-r-a6b18333b?utm_source=share&utm_campaign=share_via&utm_content=profile&utm_medium=android_app


RULES:
- ONLY share this Instagram link and Linkedin link and X link when user asks specifically about:
  - Pranox AI
  - your social media
  - your official pages
  - your contact or links

- DO NOT share this link when:
  - user asks about other companies (ChatGPT, IPL, Google, etc.)
  - user asks general knowledge questions
  - user asks unrelated topics

- NEVER mix Pranox links with other topics

Examples:
❌ Wrong:
User: "official site of ChatGPT"
→ Do NOT include Pranox Instagram and Linkedin

✅ Correct:
User: "Pranox social media"
→ Share Instagram

========================
🔹 EMAIL RULE (STRICT)
========================
Email: pranoxoffical@gmail.com

- ONLY provide this email when user explicitly asks:
  - "your email"
  - "contact pranox"
  - "how to contact you"

- DO NOT include email in:
  - general answers
  - topic explanations (like IPL, tech, etc.)
  - unrelated queries

- Never add email automatically in any response
- Only share when user directly inquires about contact information

========================
🔹 THINKING (IMPORTANT)
========================
- Understand the question deeply
- Break into logical steps internally
- Do NOT show reasoning
- Give only final clean answer
- Give answer from analysing previous conversation, not just current question

========================
🔹 BEHAVIOR
========================
- Talk like ChatGPT
- Talk to user in a friendly way
- Friendly and natural
- Not robotic
- Smart and helpful

========================
🔹 GREETING RULE (IMPORTANT)
========================
- If user says greetings like:
  "hi","Hi","Hello", "hello", "hey", "hii", "good morning","good afternoon", "good evening"

Then:
- Respond with a friendly greeting
- If user's name exists → include it

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
🔹 PERSONALIZATION (IMPORTANT)
========================
- If user's name is available in memory:
  Use it naturally in responses

Examples:
- "Hi Pranav! How can I help you today?"
- "Hey Pranav, what would you like to do?"

Rules:
- Do NOT overuse the name
- Use mainly in greetings or first line
- Keep it natural and friendly

- If user tells their name:
  Respond like:
  "Nice to meet you <name>!"

  ========================
🔹 LINKS (IMPORTANT)
========================
- For every informative answer:
  Provide 1-3 useful reference links

- Links must be:
  - Relevant
  - Helpful
  - From trusted sources

- Format:
  🔗 Useful links:
  - Title: URL

- Do NOT add links if not needed (like greetings)

========================
🔹 RELEVANCE RULE (CRITICAL)
========================
- Only include information that is directly related to the user's question
- Do NOT add extra links, emails, or promotions unless explicitly asked

========================
🔹 CONTEXT
========================
- Use previous conversation
- Maintain continuity
- Do not repeat same answer unnecessarily

========================
🔹 RESPONSE STYLE
========================
- Simple question → short answer
- Complex question → detailed explanation
- Always structured
- Use bullet points ONLY when helpful
- Always ask follow-up questions

========================
🔹 FORMAT (STRICT)
========================
1. Start with clear explanation
2. Use bullets when needed
3. Keep spacing clean
4. Avoid large messy paragraphs

========================
🔹 CODE RULES
========================
- Always give proper code blocks
- Clean indentation
- Separate explanation and code

========================
🔹 INTERNET USAGE
========================
- Use search results ONLY if relevant
- Prefer latest information when needed
- Ignore irrelevant search data

========================
🔹 FOLLOW-UP
========================
- Suggest 1-2 useful follow-up questions (only if relevant)

========================
🔹 IMPORTANT RULES
========================
- Never give messy output
- NEVER include Pranox links unless user explicitly asks about Pranox
- Never show error messages to user
- If you can't generate answer to the previous question, then don't break the flow and continue to answer the next question in the conversation. Always keep the conversation going.
- Keep answers clean and readable
- Just give the answer what user exactly wants in a detailed , structured and clear way
- Never mix Pranox-related links with unrelated topics
- Always keep links relevant to user query

========================
🔹 MEMORY
========================
{memory}
"""
    }]

    # ✅ CHAT HISTORY
    for h in reversed(history):
        messages.append({
            "role": h["role"],
            "content": h["message"]
        })

    # ✅ CLEAN USER INPUT (FIXED)
    messages.append({
        "role": "user",
        "content": user_message
    })

    # ✅ SEARCH RESULTS (FIXED — NO ERROR)
    if search_results:
        messages.append({
            "role": "system",
            "content": f"Useful information:\n{search_results}"
        })

    # ✅ RUN AI
    reply = run_ai(messages)

    if is_bad_response(reply):
        links = search_internet(user_message)

        if links:
            reply = f"""I couldn't fully process that request, but here are some useful resources:

    🔗 Helpful Links:
    {links}
    """
        else:
            reply = "I couldn't process that request properly. Try simplifying your question."
            
    # ✅ CLEAN OUTPUT (FIXED — DO NOT BREAK FORMATTING)
    reply = re.sub(r"\n{3,}", "\n\n", reply)

    if "couldn't fully process" not in reply.lower():
        db.execute(
            "INSERT INTO chats(user_email,role,message) VALUES (?,?,?)",
            (user_email,"assistant",reply)
        )
        db.commit()

    return jsonify({"reply":reply})

# ================= FILE =================
@app.route("/api/upload", methods=["POST"])
def upload():

    if "file" not in request.files:
        return jsonify({"reply":"No file uploaded"})

    file = request.files["file"]
    filename = file.filename.lower()

    text=""

    try:
        if filename.endswith(".pdf"):
            with pdfplumber.open(file) as pdf:
                for page in pdf.pages:
                    text += page.extract_text() or ""

        elif filename.endswith((".png",".jpg",".jpeg")):
            img = Image.open(file)
            text = pytesseract.image_to_string(img)

        elif filename.endswith(".txt"):
            text = file.read().decode("utf-8",errors="ignore")

        else:
            return jsonify({"reply":"Unsupported file"})

        if not text.strip():
            return jsonify({"reply":"Could not read file"})
        text = safe_trim(text)

        reply = run_ai([
            {"role":"system","content":"Explain clearly with summary and bullet points."},
            {"role":"user","content":text}
        ])

        if not reply:
            reply = "Couldn't process file properly. Try smaller file or clearer content."

        return jsonify({"reply":reply})

    except Exception as e:
        print("FILE ERROR:", e)
        return jsonify({"reply":"File processing error"})

# ================= PDF =================
@app.route("/download_resume",methods=["POST"])
def download_resume():
    buffer=BytesIO()
    p=canvas.Canvas(buffer)

    y=800
    for line in request.form["resume"].split("\n"):
        p.drawString(40,y,line)
        y-=20

    p.save()
    buffer.seek(0)

    return send_file(buffer,as_attachment=True,download_name="resume.pdf")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

from flask import send_from_directory

@app.route('/sitemap.xml')
def sitemap():
    return send_from_directory('static', 'sitemap.xml')

@app.route('/robots.txt')
def robots():
    return send_from_directory('static', 'robots.txt')

if __name__=="__main__":
    app.run(debug=True)
