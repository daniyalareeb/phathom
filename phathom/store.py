"""SurrealDB access + schema bootstrap — SPEC §8.

Deviations from SPEC §8 (recorded, all for driver safety):
- Call record ids are plain slugs (`20261002_143005_ahmed_uni`), NOT wrapped in
  unicode angle brackets. The ⟨⟩ in the spec are metasyntax; literal ⟨⟩ in ids
  would need quoting everywhere and break the CLI/recordings-folder mapping.
- All user content goes through query `$vars` (the SDK encodes it); only
  slug-safe record ids are interpolated into SQL.
"""

import asyncio
import fcntl
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from surrealdb import AsyncSurreal

from phathom.config import settings

log = logging.getLogger("phathom.store")

SCHEMA_STATEMENTS = [
    "DEFINE NAMESPACE IF NOT EXISTS phathom",
    "DEFINE DATABASE IF NOT EXISTS main",
    "DEFINE TABLE IF NOT EXISTS call SCHEMAFULL",
    "DEFINE FIELD IF NOT EXISTS caller ON call TYPE string",
    "DEFINE FIELD IF NOT EXISTS started_at ON call TYPE datetime",
    "DEFINE FIELD IF NOT EXISTS ended_at ON call TYPE option<datetime>",
    "DEFINE FIELD IF NOT EXISTS duration_s ON call TYPE option<int>",
    "DEFINE FIELD OVERWRITE mode ON call TYPE string ASSERT $value IN ['notes','assistant','manual','copilot']",
    "DEFINE FIELD IF NOT EXISTS status ON call TYPE string ASSERT $value IN ['live','processing','done','failed']",
    "DEFINE FIELD IF NOT EXISTS audio_dir ON call TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS language ON call TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS title ON call TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS summary ON call TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS key_points ON call TYPE array<string> DEFAULT []",
    "DEFINE FIELD IF NOT EXISTS action_items ON call TYPE array<object> DEFAULT []",
    "DEFINE FIELD IF NOT EXISTS action_items.*.owner ON call TYPE string",
    "DEFINE FIELD IF NOT EXISTS action_items.*.item ON call TYPE string",
    "DEFINE FIELD IF NOT EXISTS action_items.*.due ON call TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS message_for_owner ON call TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS follow_up_needed ON call TYPE bool DEFAULT false",
    "DEFINE FIELD IF NOT EXISTS error ON call TYPE option<string>",
    # v2 UI fields (EXTENSION_SPEC §6.5): starred / owner notes / message read flag.
    "DEFINE FIELD IF NOT EXISTS starred ON call TYPE bool DEFAULT false",
    "DEFINE FIELD IF NOT EXISTS notes ON call TYPE option<string>",
    "DEFINE FIELD IF NOT EXISTS message_read ON call TYPE bool DEFAULT false",
    "DEFINE TABLE IF NOT EXISTS segment SCHEMAFULL",
    "DEFINE FIELD IF NOT EXISTS call ON segment TYPE record<call>",
    "DEFINE FIELD IF NOT EXISTS t_start ON segment TYPE float",
    "DEFINE FIELD IF NOT EXISTS t_end ON segment TYPE float",
    "DEFINE FIELD OVERWRITE speaker ON segment TYPE string ASSERT $value IN ['caller','phathom','owner']",
    "DEFINE FIELD IF NOT EXISTS text ON segment TYPE string",
    "DEFINE FIELD IF NOT EXISTS source ON segment TYPE string ASSERT $value IN ['live','final']",
    "DEFINE INDEX IF NOT EXISTS seg_call ON segment FIELDS call",
    # No snowball(english): transcripts mix Urdu + English; stemming would damage Urdu words.
    "DEFINE ANALYZER IF NOT EXISTS phathom_text TOKENIZERS blank, class, punct FILTERS lowercase, ascii",
    "DEFINE INDEX IF NOT EXISTS seg_text_ft ON segment FIELDS text FULLTEXT ANALYZER phathom_text BM25",
    "DEFINE INDEX IF NOT EXISTS call_sum_ft ON call FIELDS summary FULLTEXT ANALYZER phathom_text BM25",
    # --- Brain (§8.2): chats imported from WhatsApp exports / the Web reader ---
    "DEFINE TABLE IF NOT EXISTS chat SCHEMAFULL",
    "DEFINE FIELD IF NOT EXISTS name ON chat TYPE string",
    "DEFINE FIELD IF NOT EXISTS kind ON chat TYPE string",
    "DEFINE FIELD IF NOT EXISTS last_read_at ON chat TYPE option<datetime>",
    "DEFINE FIELD IF NOT EXISTS msg_count ON chat TYPE int DEFAULT 0",
    "DEFINE FIELD IF NOT EXISTS created_at ON chat TYPE datetime",
    "DEFINE TABLE IF NOT EXISTS message SCHEMAFULL",
    "DEFINE FIELD IF NOT EXISTS chat ON message TYPE record<chat>",
    "DEFINE FIELD IF NOT EXISTS ts ON message TYPE datetime",
    "DEFINE FIELD IF NOT EXISTS sender ON message TYPE string",
    "DEFINE FIELD IF NOT EXISTS is_me ON message TYPE bool",
    "DEFINE FIELD IF NOT EXISTS text ON message TYPE string",
    "DEFINE FIELD IF NOT EXISTS hash ON message TYPE string",
    "DEFINE INDEX IF NOT EXISTS msg_dedup ON message FIELDS chat, hash UNIQUE",
    "DEFINE INDEX IF NOT EXISTS msg_text_ft ON message FIELDS text FULLTEXT ANALYZER phathom_text BM25",
    "DEFINE TABLE IF NOT EXISTS brain SCHEMAFULL",
    "DEFINE FIELD IF NOT EXISTS chat ON brain TYPE record<chat>",
    "DEFINE FIELD IF NOT EXISTS updated_at ON brain TYPE datetime",
    "DEFINE FIELD IF NOT EXISTS summary ON brain TYPE option<string>",
    # FLEXIBLE: on a SCHEMAFULL table, a plain `TYPE array` silently drops every
    # item (objects and strings alike), so brains kept only their summary.
    "DEFINE FIELD OVERWRITE people ON brain FLEXIBLE TYPE array DEFAULT []",
    "DEFINE FIELD OVERWRITE facts ON brain FLEXIBLE TYPE array DEFAULT []",
    "DEFINE FIELD OVERWRITE open_loops ON brain FLEXIBLE TYPE array DEFAULT []",
    "DEFINE FIELD OVERWRITE decisions ON brain FLEXIBLE TYPE array DEFAULT []",
    "DEFINE FIELD OVERWRITE timeline ON brain FLEXIBLE TYPE array DEFAULT []",
    "DEFINE FIELD IF NOT EXISTS last_msg_ts ON brain TYPE option<datetime>",
]


EMBEDDED_SCHEMES = ("surrealkv://", "rocksdb://", "mem://", "memory")


class DatabaseBusy(RuntimeError):
    """Another process (normally `phathom serve`) has the embedded DB open."""


def is_embedded(url: str) -> bool:
    return url.startswith(EMBEDDED_SCHEMES)


# Embedded engine: ONE connection per (url, event loop) per process. Two engine
# instances on the same files don't see each other's writes, so every Store
# shares this connection, and an flock keeps a second process out.
_shared: dict[tuple[str, int], AsyncSurreal] = {}
_bootstrapped: set[tuple[str, int, str, str]] = set()
_locks: dict[str, int] = {}
# Concurrent first requests must not open/bootstrap in parallel: overlapping
# DEFINE statements on one embedded connection corrupt reads ("Invalid revision").
_open_lock: dict[int, asyncio.Lock] = {}
# Embedded engine transactions are optimistic: overlapping writes fail with a
# retryable conflict. One user, small queries — run them one at a time.
_query_lock: dict[int, asyncio.Lock] = {}


def _lock_embedded(url: str) -> None:
    if url.startswith(("mem://", "memory")) or url in _locks:
        return
    path = Path(url.split("://", 1)[1])
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise DatabaseBusy(
            "the Phathom server is running and owns the database — use the dashboard, "
            "or stop the server (Ctrl+C) and run this again") from None
    _locks[url] = fd


async def close_shared() -> None:
    """Flush + close embedded connections (server shutdown / end of CLI command)."""
    for key, db in list(_shared.items()):
        try:
            await db.close()
        except Exception as e:  # noqa: BLE001
            log.warning("embedded DB close failed: %r", e)
        _shared.pop(key, None)
    _bootstrapped.clear()
    _open_lock.clear()
    _query_lock.clear()
    for url, fd in list(_locks.items()):
        os.close(fd)
        _locks.pop(url, None)


def slug_caller(caller: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (caller or "unknown").lower()).strip("_")
    return slug or "unknown"


def make_call_id(caller: str, started: datetime | None = None) -> str:
    started = started or datetime.now(timezone.utc)
    return f"{started.strftime('%Y%m%d_%H%M%S')}_{slug_caller(caller)}"


def _rid(call_id: str) -> str:
    """Record id for SQL. Ids are slug-safe; backticks because they start with digits."""
    assert re.fullmatch(r"[A-Za-z0-9_]+", call_id), f"unsafe call_id: {call_id!r}"
    return f"call:`{call_id}`"


def _norm(value):
    """Convert SDK return types to plain JSON-safe python."""
    if isinstance(value, dict):
        return {str(k): _norm(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    if hasattr(value, "isoformat"):  # datetime-like
        try:
            return value.isoformat()
        except Exception:
            pass
    classname = type(value).__name__
    if classname in ("RecordID", "RecordId", "Table", "Datetime", "Duration", "Decimal"):
        return str(value)
    return value


class Store:
    def __init__(self, url: str | None = None, user: str | None = None,
                 password: str | None = None, ns: str | None = None, db: str | None = None):
        self.url = url or settings.SURREAL_URL
        self.user = user or settings.SURREAL_USER
        self.password = password or settings.SURREAL_PASS
        self.ns = ns or settings.SURREAL_NS
        self.dbname = db or settings.SURREAL_DB
        self._db = None

    async def connect(self) -> "Store":
        if is_embedded(self.url):
            loop_id = id(asyncio.get_running_loop())
            key = (self.url, loop_id)
            boot = (*key, self.ns, self.dbname)
            if boot in _bootstrapped:
                self._db = _shared[key]
                return self
            async with _open_lock.setdefault(loop_id, asyncio.Lock()):
                if key not in _shared:
                    _lock_embedded(self.url)
                    db = AsyncSurreal(self.url)
                    await db.connect()
                    await db.use(self.ns, self.dbname)
                    _shared[key] = db
                self._db = _shared[key]
                if boot not in _bootstrapped:
                    await self.bootstrap()
                    _bootstrapped.add(boot)
            return self
        self._db = AsyncSurreal(self.url)
        await self._db.connect()
        await self._db.signin({"user": self.user, "pass": self.password})
        await self._db.use(self.ns, self.dbname)
        await self.bootstrap()
        return self

    async def close(self) -> None:
        if self._db is not None:
            if not is_embedded(self.url):  # embedded: shared, closed by close_shared()
                await self._db.close()
            self._db = None

    async def query(self, sql: str, vars: dict | None = None):
        assert self._db is not None, "Store not connected"
        if not is_embedded(self.url):
            return _norm(await self._db.query(sql, vars or {}))
        lock = _query_lock.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())
        for attempt in range(4):
            try:
                async with lock:
                    return _norm(await self._db.query(sql, vars or {}))
            except Exception as e:  # noqa: BLE001
                if "can be retried" not in str(e) or attempt == 3:
                    raise
                await asyncio.sleep(0.05 * (attempt + 1))

    async def bootstrap(self) -> None:
        for stmt in SCHEMA_STATEMENTS:
            try:
                await self.query(stmt)
            except Exception as e:
                # The embedded engine is SurrealDB 2.x: full-text indexes are
                # `SEARCH ANALYZER` there (`FULLTEXT ANALYZER` is 3.x syntax).
                if " FULLTEXT ANALYZER " in stmt and "parse error" in repr(e).lower():
                    await self.query(stmt.replace(" FULLTEXT ANALYZER ", " SEARCH ANALYZER "))
                    continue
                if " FLEXIBLE " in stmt and "parse error" in repr(e).lower():
                    log.warning("engine rejects FLEXIBLE; brain arrays may lose items: %s", stmt)
                    await self.query(stmt.replace(" FLEXIBLE ", " "))
                    continue
                log.error("schema statement failed: %s -> %r", stmt, e)
                raise

    # ---- calls ----

    async def create_call(self, call_id: str, caller: str, started_at: str,
                          mode: str, status: str = "live", audio_dir: str | None = None) -> dict:
        rows = await self.query(
            f"CREATE {_rid(call_id)} CONTENT {{ caller: $caller, started_at: type::datetime($started_at),"
            " mode: $mode, status: $status, audio_dir: $audio_dir } RETURN id, caller, started_at, mode, status",
            {"caller": caller, "started_at": started_at,
             "mode": mode, "status": status, "audio_dir": audio_dir},
        )
        return rows[0] if isinstance(rows, list) else rows

    async def update_call(self, call_id: str, fields: dict) -> dict | None:
        if not fields:
            return await self.get_call(call_id)
        plain = {k: v for k, v in fields.items() if k not in ("started_at", "ended_at")}
        if plain:
            rows = await self.query(f"UPDATE {_rid(call_id)} MERGE $fields RETURN *", {"fields": plain})
        for k in ("started_at", "ended_at"):
            if k in fields:
                rows = await self.query(
                    f"UPDATE {_rid(call_id)} SET {k} = type::datetime(${k}) RETURN *", {k: fields[k]})
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows

    async def get_call(self, call_id: str) -> dict | None:
        rows = await self.query(f"SELECT * FROM {_rid(call_id)}")
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows

    async def list_calls(self, limit: int = 20) -> list[dict]:
        rows = await self.query(
            "SELECT id, caller, started_at, ended_at, duration_s, mode, status, title"
            " FROM call ORDER BY started_at DESC LIMIT $limit", {"limit": limit}
        )
        return rows if isinstance(rows, list) else []

    # ---- segments ----

    async def add_segments(self, call_id: str, segments: list[dict]) -> None:
        for seg in segments:
            await self.query(
                f"CREATE segment CONTENT {{ call: {_rid(call_id)}, t_start: $t0, t_end: $t1,"
                " speaker: $speaker, text: $text, source: $source }",
                {"t0": float(seg["t_start"]), "t1": float(seg["t_end"]),
                 "speaker": seg["speaker"], "text": seg["text"], "source": seg["source"]},
            )

    async def delete_segments(self, call_id: str, source: str | None = None) -> None:
        if source:
            await self.query(f"DELETE segment WHERE call = {_rid(call_id)} AND source = $source",
                             {"source": source})
        else:
            await self.query(f"DELETE segment WHERE call = {_rid(call_id)}")

    async def get_segments(self, call_id: str) -> list[dict]:
        rows = await self.query(
            f"SELECT t_start, t_end, speaker, text, source FROM segment"
            f" WHERE call = {_rid(call_id)} ORDER BY t_start",
        )
        return rows if isinstance(rows, list) else []

    # ---- brain (§8.2) ----

    async def upsert_chat(self, chat_id: str, name: str, kind: str = "direct") -> dict:
        if await self.get_chat(chat_id):
            rows = await self.query(
                "UPDATE type::record($id) SET name = $name, kind = $kind RETURN *",
                {"id": f"chat:{chat_id}", "name": name, "kind": kind})
        else:
            rows = await self.query(
                "CREATE type::record($id) CONTENT { name: $name, kind: $kind,"
                " msg_count: 0, created_at: time::now() } RETURN *",
                {"id": f"chat:{chat_id}", "name": name, "kind": kind})
        return rows[0] if isinstance(rows, list) and rows else {}

    async def list_chats(self) -> list[dict]:
        rows = await self.query(
            "SELECT id, name, kind, last_read_at, msg_count FROM chat ORDER BY name")
        return rows if isinstance(rows, list) else []

    async def get_chat(self, chat_id: str) -> dict | None:
        rows = await self.query("SELECT * FROM type::record($id)", {"id": f"chat:{chat_id}"})
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows

    async def add_messages(self, chat_id: str, messages: list[dict]) -> int:
        """Insert messages, skipping duplicates on (chat, hash). Returns added count."""
        added = 0
        for m in messages:
            try:
                await self.query(
                    "CREATE message CONTENT { chat: type::record($chat),"
                    " ts: type::datetime($ts), sender: $sender, is_me: $is_me,"
                    " text: $text, hash: $hash }",
                    {"chat": f"chat:{chat_id}", "ts": m["ts"], "sender": m["sender"],
                     "is_me": bool(m["is_me"]), "text": m["text"], "hash": m["hash"]})
                added += 1
            except Exception as e:  # noqa: BLE001 — UNIQUE violation = duplicate, skip
                msg = repr(e).lower()
                if "unique" not in msg and "duplicate" not in msg and "already contains" not in msg:
                    raise
        if added:
            await self.query(
                "UPDATE type::record($id) SET msg_count += $n, last_read_at = time::now()",
                {"id": f"chat:{chat_id}", "n": added})
        return added

    async def get_messages_since(self, chat_id: str, since: str | None,
                                 limit: int = 5000) -> list[dict]:
        if since:
            rows = await self.query(
                "SELECT ts, sender, is_me, text FROM message WHERE chat = type::record($chat)"
                " AND ts > type::datetime($since) ORDER BY ts LIMIT $limit",
                {"chat": f"chat:{chat_id}", "since": since, "limit": limit})
        else:
            rows = await self.query(
                "SELECT ts, sender, is_me, text FROM message WHERE chat = type::record($chat)"
                " ORDER BY ts LIMIT $limit",
                {"chat": f"chat:{chat_id}", "limit": limit})
        return rows if isinstance(rows, list) else []

    async def save_brain(self, chat_id: str, brain: dict) -> None:
        content = {"chat": f"chat:{chat_id}",
                   "summary": brain.get("summary"), "people": brain.get("people", []),
                   "facts": brain.get("facts", []), "open_loops": brain.get("open_loops", []),
                   "decisions": brain.get("decisions", []), "timeline": brain.get("timeline", [])}
        if await self.get_brain(chat_id):
            merge = {k: v for k, v in content.items() if k != "chat"}
            await self.query("UPDATE type::record($id) MERGE $m",
                             {"id": f"brain:{chat_id}", "m": merge})
            await self.query("UPDATE type::record($id) SET updated_at = time::now()",
                             {"id": f"brain:{chat_id}"})
        else:
            await self.query(
                "CREATE type::record($id) CONTENT { chat: type::record($chat),"
                " updated_at: time::now(), summary: $summary, people: $people, facts: $facts,"
                " open_loops: $open_loops, decisions: $decisions, timeline: $timeline }",
                {"id": f"brain:{chat_id}", **content})
        if brain.get("last_msg_ts"):
            await self.query("UPDATE type::record($id) SET last_msg_ts = type::datetime($ts)",
                             {"id": f"brain:{chat_id}", "ts": brain["last_msg_ts"]})

    async def get_brain(self, chat_id: str) -> dict | None:
        rows = await self.query("SELECT * FROM type::record($id)", {"id": f"brain:{chat_id}"})
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows

    async def delete_brain(self, chat_id: str) -> None:
        await self.query("DELETE type::record($cid)", {"cid": f"chat:{chat_id}"})
        await self.query("DELETE message WHERE chat = type::record($cid)", {"cid": f"chat:{chat_id}"})
        await self.query("DELETE type::record($bid)", {"bid": f"brain:{chat_id}"})
