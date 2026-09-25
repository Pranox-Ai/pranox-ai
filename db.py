import os
from dotenv import load_dotenv  # ADD THIS
load_dotenv() 
import psycopg2
from psycopg2.extras import RealDictCursor

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    # Users table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            picture TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    
    # Chat sessions table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            session_id TEXT UNIQUE NOT NULL,
            title TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Chats table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            session_id TEXT,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # Deep research table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deep_research (
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            session_id TEXT,
            role TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    # User memory table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_memory (
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_email, key)
        )
    """)

    # Email history table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_history (
            id SERIAL PRIMARY KEY,
            user_email TEXT NOT NULL,
            prompt TEXT,
            result TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("✅ Database initialized successfully")

if __name__ == "__main__":
    init_db()