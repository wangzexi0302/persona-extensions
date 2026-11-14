"""What the program has seen, in SQLite in the plugin's data directory: status and inbox.collect answer from here without
the network, and the archive's cursor is a row number."""
import json
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
  id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'group', last_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, chat TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
  sender TEXT NOT NULL DEFAULT '', sender_name TEXT NOT NULL DEFAULT '', mine INTEGER NOT NULL DEFAULT 0,
  type TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', at REAL NOT NULL DEFAULT 0,
  root TEXT NOT NULL DEFAULT '', parent TEXT NOT NULL DEFAULT '', at_me INTEGER NOT NULL DEFAULT 0,
  media TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat, position);
CREATE TABLE IF NOT EXISTS names (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sends (key TEXT PRIMARY KEY, result TEXT);
CREATE TABLE IF NOT EXISTS agent_asks (
  id TEXT PRIMARY KEY, chat TEXT NOT NULL, topic TEXT NOT NULL, sender TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS agent_topics (topic TEXT PRIMARY KEY, chat TEXT NOT NULL, last_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS own_sends (id TEXT PRIMARY KEY, at REAL NOT NULL);
"""


class Store:
    def __init__(self, path):
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        # 0.1 named a direct chat it saw only through the user's own messages 私聊 ("direct chat") for good.
        self._db.execute("UPDATE chats SET name = '' WHERE kind = 'dm' AND name = '私聊'")
        self._db.commit()

    def _all(self, sql, *args):
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    def _one(self, sql, *args):
        rows = self._all(sql, *args)
        return rows[0] if rows else None

    def _do(self, sql, *args):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.rowcount

    def add_message(self, m, sender_name, mine):
        """Keeps a message once; True when it is new."""
        return self._do(
            "INSERT OR IGNORE INTO messages (id, chat, position, sender, sender_name, mine, type, text, at, root, parent, at_me, media)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            m["id"], m["chat"], m.get("position") or 0, m.get("sender") or "", sender_name, 1 if mine else 0,
            m.get("type") or "", m.get("text") or "", m.get("at") or 0, m.get("root") or "", m.get("parent") or "",
            1 if m.get("at_me") else 0, json.dumps(m.get("media") or [], ensure_ascii=False)) > 0

    def touch_chat(self, chat, kind, at):
        self._do("INSERT INTO chats (id, kind, last_at) VALUES (?, ?, ?)"
                 " ON CONFLICT (id) DO UPDATE SET last_at = MAX(last_at, excluded.last_at), kind = excluded.kind", chat, kind, at)

    def name_chat(self, chat, name):
        self._do("UPDATE chats SET name = ? WHERE id = ?", name, chat)

    def chat(self, chat):
        return self._one("SELECT * FROM chats WHERE id = ?", chat)

    def chats(self, limit, query=""):
        like = "%" + query + "%"
        return self._all("SELECT * FROM chats WHERE name LIKE ? ORDER BY last_at DESC LIMIT ?", like, limit)

    def chats_since(self, at, limit):
        return self._all("SELECT * FROM chats WHERE last_at >= ? ORDER BY last_at DESC LIMIT ?", at, limit)

    def message(self, msg_id):
        return _message(self._one("SELECT * FROM messages WHERE id = ?", msg_id))

    def messages(self, chat, before, limit):
        """Up to limit messages of chat, oldest first: the newest ones, or those before position before."""
        where, args = "chat = ?", [chat]
        if before:
            where, args = "chat = ? AND position < ?", [chat, before]
        rows = self._all("SELECT * FROM messages WHERE %s ORDER BY position DESC, at DESC, seq DESC LIMIT ?" % where, *args, limit)
        return [_message(r) for r in reversed(rows)]

    def messages_before(self, chat, position, at, limit):
        """Up to limit messages of chat's own flow (not its topics' replies, whose positions are below 1) before the one
        at position, or before at when that has no position; oldest first."""
        if position > 0:
            where, args = "chat = ? AND position > 0 AND position < ?", [chat, position]
        else:
            where, args = "chat = ? AND position > 0 AND at <= ?", [chat, at]
        rows = self._all("SELECT * FROM messages WHERE %s ORDER BY position DESC, at DESC, seq DESC LIMIT ?" % where, *args, limit)
        return [_message(r) for r in reversed(rows)]

    def topic_messages(self, chat, root, at, limit):
        """Up to limit messages of the topic rooted at root, the root among them, up to at; oldest first."""
        rows = self._all("SELECT * FROM messages WHERE chat = ? AND (id = ? OR root = ?) AND at <= ?"
                         " ORDER BY at DESC, seq DESC LIMIT ?", chat, root, root, at, limit)
        return [_message(r) for r in reversed(rows)]

    def oldest_position(self, chat):
        row = self._one("SELECT MIN(position) AS p FROM messages WHERE chat = ? AND position > 0", chat)
        return (row or {}).get("p") or 0

    def newest_position(self, chat):
        row = self._one("SELECT MAX(position) AS p FROM messages WHERE chat = ?", chat)
        return (row or {}).get("p") or 0

    def after(self, seq, limit):
        """Up to limit messages kept after row seq, in the order they were kept."""
        return [_message(r) for r in self._all("SELECT * FROM messages WHERE seq > ? ORDER BY seq LIMIT ?", seq, limit)]

    def prune(self, before_at, upto_seq):
        """Deletes the messages from before before_at among rows up to upto_seq; how many went."""
        return self._do("DELETE FROM messages WHERE at < ? AND seq <= ?", before_at, upto_seq)

    def name(self, key):
        row = self._one("SELECT name FROM names WHERE id = ?", key)
        return row["name"] if row else ""

    def set_name(self, key, name):
        self._do("INSERT INTO names (id, name) VALUES (?, ?) ON CONFLICT (id) DO UPDATE SET name = excluded.name", key, name)

    def claim_send(self, key):
        """Takes key for one send. Returns (True, "") the first time, else (False, what that send ended with): a send
        that was claimed and has no result may or may not have gone out, and is never tried again."""
        with self._lock:
            cur = self._db.execute("INSERT OR IGNORE INTO sends (key) VALUES (?)", (key,))
            self._db.commit()
            if cur.rowcount > 0:
                return True, ""
            row = self._db.execute("SELECT result FROM sends WHERE key = ?", (key,)).fetchone()
        return False, row["result"] or ""

    def finish_send(self, key, result):
        self._do("UPDATE sends SET result = ? WHERE key = ?", result, key)

    # /agent: the requests handed to the hub, the topics followed, and what this program sent

    def add_ask(self, msg_id, chat, topic, sender, at):
        self._do("INSERT OR IGNORE INTO agent_asks (id, chat, topic, sender, at) VALUES (?, ?, ?, ?, ?)", msg_id, chat, topic, sender, at)

    def ask(self, msg_id):
        return self._one("SELECT * FROM agent_asks WHERE id = ?", msg_id)

    def follow_topic(self, topic, chat, at):
        self._do("INSERT INTO agent_topics (topic, chat, last_at) VALUES (?, ?, ?)"
                 " ON CONFLICT (topic) DO UPDATE SET last_at = MAX(last_at, excluded.last_at)", topic, chat, at)

    def touch_topic(self, topic, at):
        """Something happened in a followed topic: it is followed from at on. Nothing for a topic not followed."""
        self._do("UPDATE agent_topics SET last_at = MAX(last_at, ?) WHERE topic = ?", at, topic)

    def topic(self, topic):
        return self._one("SELECT * FROM agent_topics WHERE topic = ?", topic)

    def end_topic(self, topic):
        self._do("DELETE FROM agent_topics WHERE topic = ?", topic)

    def add_own(self, msg_id, at):
        self._do("INSERT OR IGNORE INTO own_sends (id, at) VALUES (?, ?)", msg_id, at)

    def is_own(self, msg_id):
        return self._one("SELECT id FROM own_sends WHERE id = ?", msg_id) is not None

    def prune_agent(self, before_at):
        """Forgets requests, followed topics and sent ids from before before_at; how many rows went."""
        return sum(self._do("DELETE FROM %s WHERE %s < ?" % (table, column), before_at)
                   for table, column in (("agent_asks", "at"), ("agent_topics", "last_at"), ("own_sends", "at")))


def _message(row):
    if row is None:
        return None
    row = dict(row)
    row["media"] = json.loads(row.get("media") or "[]")
    row["mine"] = bool(row.get("mine"))
    row["at_me"] = bool(row.get("at_me"))
    return row
