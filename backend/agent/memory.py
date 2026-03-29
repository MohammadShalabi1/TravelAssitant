import pyodbc
import uuid

conn_str = (
    "DRIVER={ODBC Driver 17 for SQL Server};"
    "SERVER=localhost\SQLEXPRESS03;"
    "DATABASE=ai_agent_db;"
    "Trusted_Connection=yes;"
)


def init_db():
    conn = pyodbc.connect(conn_str)
    cur = conn.cursor()

    cur.execute("""
    IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='conversations' AND xtype='U')
    CREATE TABLE conversations (
        id BIGINT IDENTITY(1,1) PRIMARY KEY,
        session_id NVARCHAR(100) UNIQUE,
        created_at DATETIME DEFAULT GETDATE()
    )
    """)

    cur.execute("""
    IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='messages' AND xtype='U')
    CREATE TABLE messages (
        id BIGINT IDENTITY(1,1) PRIMARY KEY,
        conversation_id BIGINT,
        role NVARCHAR(20),
        content NVARCHAR(MAX),
        created_at DATETIME DEFAULT GETDATE(),
        FOREIGN KEY (conversation_id) REFERENCES conversations(id)
    )
    """)

    conn.commit()
    conn.close()


def create_session():
    session_id = str(uuid.uuid4())
    conn = pyodbc.connect(conn_str)
    cur = conn.cursor()
    cur.execute("INSERT INTO conversations (session_id) VALUES (?)", session_id)
    conn.commit()
    conn.close()
    return session_id


def get_conversation_id(session_id):
    conn = pyodbc.connect(conn_str)
    cur = conn.cursor()
    cur.execute("SELECT id FROM conversations WHERE session_id = ?", session_id)
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def save_message(session_id, role, content):
    conversation_id = get_conversation_id(session_id)
    conn = pyodbc.connect(conn_str)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (conversation_id, role, content)
        VALUES (?, ?, ?)
    """, conversation_id, role, content)
    conn.commit()
    conn.close()


def load_history(session_id):
    conversation_id = get_conversation_id(session_id)
    conn = pyodbc.connect(conn_str)
    cur = conn.cursor()
    cur.execute("""
        SELECT role, content
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id ASC
    """, conversation_id)
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_sessions():
    """
    Return all sessions ordered by most recent first.
    Each row: (session_id, created_at)
    Used by the sidebar to list past conversations.
    """
    conn = pyodbc.connect(conn_str)
    cur = conn.cursor()
    cur.execute("""
        SELECT session_id, created_at
        FROM conversations
        ORDER BY created_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows