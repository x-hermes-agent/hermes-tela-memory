"""Tela memory provider for Hermes Agent.

A fixed set of tela wiki pages acts as the agent's long-term memory: they are fetched once per
session and injected into the system prompt (a frozen snapshot, like the built-in MEMORY.md /
USER.md), and the ``tela_memory`` tool edits them. People can read and edit the same pages in
tela, and tela's revision history shows who changed what.

Configuration (``$HERMES_HOME/tela_memory.json``, env vars win):
    url        TELA_URL           tela base URL; falls back to TELA_MCP_URL minus ``/api/mcp``
    pages      TELA_MEMORY_PAGES  pages to load, in prompt order: ids and/or titles
                                  (JSON list or "41,42,43"); default: four starter pages
    space      TELA_MEMORY_SPACE  space id or name for pages given by title (default "Memory")
    org_id     TELA_MEMORY_ORG_ID org that owns the space when it has to be created (optional)
    max_chars  TELA_MEMORY_MAX_CHARS  per-page size cap for writes (default 4000)
    timeout    TELA_MEMORY_TIMEOUT    HTTP timeout in seconds (default 8)
Secret: TELA_API_KEY (a tela personal access token, write scope).

Pages given by title are looked up in the space and created when missing (the space too), so a
fresh install needs only the URL and the token. Resolved ids are written back to the config file.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

__version__ = "1.0.0"

CONFIG_FILE = "tela_memory.json"
CACHE_DIR = ("cache", "tela_memory")
DEFAULT_MAX_CHARS = 4000
DEFAULT_TIMEOUT = 8
USER_AGENT = f"hermes-tela-memory/{__version__}"
MCP_PROTOCOL_VERSION = "2025-06-18"
DEFAULT_SPACE = "Memory"
# Starter pages (title -> intro line) used when no pages are configured.
DEFAULT_PAGES = {
    "User": "Who the user is: lasting facts, preferences and how they like to work.",
    "Agent Notes": "Lasting decisions, lessons learned and facts needed in every conversation.",
    "Ongoing Work": "Work in progress, one line each with status and due date. Finished items are removed.",
    "Map": "Where information lives: one line per tela space or page and what it holds.",
}

SEPARATOR = "═" * 46
PROMPT_HEADER = (
    "TELA MEMORY (your long-term memory; snapshot taken at session start)\n"
    "These tela pages are your persistent memory. The user can read and edit them too.\n"
    "- Save what should outlive this conversation: lasting facts and preferences about the user, "
    "decisions, lessons learned, ongoing work. Do not save one-off details or secrets.\n"
    "- Write each note as one short line: the fact or rule itself. Leave out test logs, dates, "
    "how you found it and caveats about things not yet tried.\n"
    "- Edit with the `tela_memory` tool. Keep pages short and current: update or remove stale "
    "lines instead of piling up new ones; put long details on other tela pages and leave a "
    "one-line pointer here.\n"
    "- Changes reach this prompt in the next session; use `tela_memory` action=read for the "
    "live text."
)

TOOL_SCHEMA = {
    "name": "tela_memory",
    "description": (
        "Edit your long-term memory pages in tela (the TELA MEMORY block of your system prompt). "
        "Actions: read (current text of a page), add (append text at the end of a page), "
        "replace (swap an exact snippet that occurs once for new text), remove (delete an exact "
        "snippet that occurs once). Use replace to put text under a specific heading."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "add", "replace", "remove"]},
            "page": {"type": "string", "description": "Page title or id, as shown in TELA MEMORY."},
            "content": {"type": "string", "description": "Text to add, or the new text for replace."},
            "old_text": {"type": "string", "description": "Exact current snippet for replace/remove."},
        },
        "required": ["action", "page"],
    },
}


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


def _load_settings(home: Path) -> dict[str, Any]:
    """File config overlaid with env vars; never raises."""
    settings: dict[str, Any] = {}
    try:
        settings = json.loads((home / CONFIG_FILE).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("tela memory: unreadable %s: %s", home / CONFIG_FILE, exc)

    url = os.environ.get("TELA_URL") or settings.get("url") or ""
    if not url and os.environ.get("TELA_MCP_URL", "").rstrip("/").endswith("/api/mcp"):
        url = os.environ["TELA_MCP_URL"].rstrip("/")[: -len("/api/mcp")]
    pages = os.environ.get("TELA_MEMORY_PAGES") or settings.get("pages") or list(DEFAULT_PAGES)
    if isinstance(pages, str):
        pages = json.loads(pages) if pages.strip().startswith("[") else pages.split(",")
    space = os.environ.get("TELA_MEMORY_SPACE") or settings.get("space") or DEFAULT_SPACE
    org_id = os.environ.get("TELA_MEMORY_ORG_ID") or settings.get("org_id")
    return {
        "url": url.rstrip("/"),
        "pages": [_page_ref(p) for p in pages if str(p).strip()],
        "space": _page_ref(space),
        "org_id": int(org_id) if org_id else None,
        "persist": not os.environ.get("TELA_MEMORY_PAGES") and not os.environ.get("TELA_MEMORY_SPACE"),
        "max_chars": int(os.environ.get("TELA_MEMORY_MAX_CHARS") or settings.get("max_chars") or DEFAULT_MAX_CHARS),
        "timeout": float(os.environ.get("TELA_MEMORY_TIMEOUT") or settings.get("timeout") or DEFAULT_TIMEOUT),
    }


def _page_ref(ref: Any) -> int | str:
    """Ids stay ints; anything else is a title (or space name)."""
    text = str(ref).strip()
    return int(text) if text.isdigit() else text


def _fold(text: str) -> str:
    """Case- and accent-insensitive key, so "süren işler" matches "Süren İşler" (Turkish İ/ı)."""
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("ı", "i"))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).strip()


def _scan(content: str) -> Optional[str]:
    """Hermes' own injection/exfiltration scan for prompt-bound memory, when available."""
    try:
        from tools.memory_tool_store import _scan_memory_content
    except Exception:
        return None
    return _scan_memory_content(content)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _page_fields(payload: dict[str, Any]) -> dict[str, Any]:
    page = payload.get("page", payload)
    return {"id": page["id"], "title": page["title"], "body": page.get("body") or "",
            "version": page.get("version")}


def _session_header(session: str) -> dict[str, str]:
    return {"Mcp-Session-Id": session} if session else {}


def _rpc_message(body: str, msg_id: int) -> dict[str, Any]:
    """The JSON-RPC message with ``msg_id`` from a plain JSON or server-sent-events response."""
    candidates = [body] if body.lstrip().startswith("{") else [
        line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
    for raw in candidates:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == msg_id:
            return message
    raise TelaError(f"unexpected tela MCP response: {body[:300]}")


class TelaError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class TelaMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._settings: dict[str, Any] = {}
        self._token = ""
        self._cache_dir: Optional[Path] = None
        self._home: Optional[Path] = None
        self._page_ids: list[int] = []
        self._pages: list[dict[str, Any]] = []  # snapshot loaded at initialize()
        self._prompt = ""
        self._mcp_session = ""
        self._mcp_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "tela"

    # -- lifecycle -----------------------------------------------------------

    def is_available(self) -> bool:
        settings = _load_settings(_hermes_home())
        return bool(os.environ.get("TELA_API_KEY") and settings["url"])

    def unavailable_reason(self) -> str:
        settings = _load_settings(_hermes_home())
        missing = [name for name, ok in (
            ("TELA_API_KEY", os.environ.get("TELA_API_KEY")),
            ("TELA_URL (or TELA_MCP_URL)", settings["url"]),
        ) if not ok]
        return f"missing {', '.join(missing)}" if missing else ""

    def initialize(self, session_id: str, **kwargs) -> None:
        home = Path(kwargs.get("hermes_home") or _hermes_home())
        self._settings = _load_settings(home)
        self._token = os.environ.get("TELA_API_KEY", "")
        self._home = home
        self._cache_dir = home.joinpath(*CACHE_DIR)
        self._page_ids = self._resolve_pages()
        with ThreadPoolExecutor(max_workers=min(8, len(self._page_ids) or 1)) as pool:
            self._pages = [p for p in pool.map(self._load_page, self._page_ids) if p]
        self._prompt = self._render_prompt()

    def system_prompt_block(self) -> str:
        return self._prompt

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [TOOL_SCHEMA]

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "api_key", "description": "tela personal access token", "secret": True,
             "required": True, "env_var": "TELA_API_KEY"},
            {"key": "url", "description": "tela base URL, e.g. https://tela.example.com", "required": True},
            {"key": "pages", "description": "Memory pages (ids or titles), comma separated, in prompt order; "
             "empty = create starter pages"},
            {"key": "space", "description": "Space id or name for pages given by title", "default": DEFAULT_SPACE},
            {"key": "org_id", "description": "Org that owns the space if it has to be created", "type": "integer"},
            {"key": "max_chars", "description": "Per-page size cap for writes", "type": "integer",
             "default": DEFAULT_MAX_CHARS, "minimum": 500},
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        path = Path(hermes_home) / CONFIG_FILE
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            current = {}
        for key in ("url", "pages", "space", "org_id", "max_chars", "timeout"):
            if values.get(key) not in (None, ""):
                current[key] = values[key]
        if isinstance(current.get("pages"), str):
            current["pages"] = [_page_ref(p) for p in current["pages"].split(",") if p.strip()]
        _write_json(path, current)

    # -- tela API: REST for reads, MCP tools for writes --------------------------
    # Writes go through tela's MCP tools: tela records them as agent edits in the revision
    # history and resets the page's live-editor state, so the editor opens the new text. A REST
    # PATCH does neither.

    def _http(self, path: str, data: Optional[dict] = None,
              headers: Optional[dict] = None) -> tuple[str, Any]:
        req = urllib.request.Request(
            f"{self._settings['url']}/api/{path}",
            method="POST" if data is not None else "GET",
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream", "User-Agent": USER_AGENT,
                     **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._settings["timeout"]) as resp:
                return resp.read().decode("utf-8"), resp.headers
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise TelaError(f"tela HTTP {exc.code}: {detail}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TelaError(f"tela unreachable: {exc}") from exc

    def _get(self, path: str) -> dict[str, Any]:
        return json.loads(self._http(path)[0])

    def _page(self, page_id: int) -> dict[str, Any]:
        return _page_fields(self._get(f"pages/{page_id}"))

    def _mcp(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tela MCP tool and return its JSON result; one retry with a fresh session."""
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        with self._mcp_lock:
            for attempt in range(2):
                if not self._mcp_session:
                    self._mcp_session = self._mcp_open()
                try:
                    message = _rpc_message(self._http("mcp", call, _session_header(self._mcp_session))[0], 2)
                except TelaError as exc:
                    if attempt == 0 and exc.status in (400, 404):  # session expired on the server
                        self._mcp_session = ""
                        continue
                    raise
                if "error" in message:
                    error = str(message["error"].get("message", message["error"]))
                    if attempt == 0 and "session" in error.lower():
                        self._mcp_session = ""
                        continue
                    raise TelaError(f"tela MCP error: {error}")
                result = message.get("result") or {}
                text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
                if result.get("isError"):
                    raise TelaError(text or f"{tool} failed", 409 if "version_conflict" in text else 0)
                return json.loads(text) if text.strip().startswith("{") else {}
        raise TelaError("tela MCP session could not be established")

    def _mcp_open(self) -> str:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "hermes-tela-memory", "version": __version__}}}
        body, headers = self._http("mcp", init)
        if "error" in _rpc_message(body, 1):
            raise TelaError(f"tela MCP initialize failed: {body[:300]}")
        session = headers.get("Mcp-Session-Id") or ""
        self._http("mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, _session_header(session))
        return session

    # -- bootstrap -------------------------------------------------------------

    def _resolve_pages(self) -> list[int]:
        """Page ids in prompt order. Titles are found in the space or created there (the space too,
        if missing); the ids are saved to the config file so this runs once."""
        refs = self._settings["pages"]
        if all(isinstance(ref, int) for ref in refs):
            return list(refs)
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self._cache_dir / ".bootstrap.lock", "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)  # one creator when several sessions start at once
                space_id = self._ensure_space()
                existing = {_fold(p["title"]): p["id"] for p in
                            self._get(f"pages?space_id={space_id}").get("pages", [])}
                ids = []
                for ref in refs:
                    if isinstance(ref, int):
                        ids.append(ref)
                    elif _fold(ref) in existing:
                        ids.append(existing[_fold(ref)])
                    else:
                        created = self._mcp("create_page", {
                            "space_id": space_id, "title": ref, "body": DEFAULT_PAGES.get(ref, "") + "\n"})
                        ids.append(created.get("page", created)["id"])
                        logger.info("tela memory: created page %r in space %s", ref, space_id)
        except TelaError as exc:
            logger.warning("tela memory: could not set up memory pages: %s", exc)
            return [ref for ref in refs if isinstance(ref, int)]
        if self._settings["persist"]:
            self._persist({"space": space_id, "pages": ids})
        return ids

    def _ensure_space(self) -> int:
        space = self._settings["space"]
        if isinstance(space, int):
            return space
        org_id = self._settings["org_id"]
        for candidate in self._get("spaces").get("spaces", []):
            owner = (candidate.get("owner_org") or {}).get("id")
            if _fold(candidate["name"]) == _fold(space) and (org_id is None or owner == org_id):
                return candidate["id"]
        created = self._mcp("create_space", {"name": space, **({"org_id": org_id} if org_id else {})})
        logger.info("tela memory: created space %r", space)
        return created.get("space", created)["id"]

    def _persist(self, values: dict[str, Any]) -> None:
        path = self._home / CONFIG_FILE
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            current = {}
        except Exception as exc:
            logger.warning("tela memory: not saving resolved pages, %s is unreadable: %s", path, exc)
            return
        try:
            _write_json(path, {**current, **values})
        except OSError as exc:
            logger.warning("tela memory: could not save resolved pages to %s: %s", path, exc)

    def _cache_path(self, page_id: int) -> Path:
        return self._cache_dir / f"{page_id}.json"

    def _write_cache(self, page: dict[str, Any]) -> None:
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path(page["id"]).with_suffix(".tmp")
            tmp.write_text(json.dumps({**page, "fetched_at": time.time()}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._cache_path(page["id"]))
        except OSError as exc:
            logger.debug("tela memory: cache write failed for page %s: %s", page["id"], exc)

    def _load_page(self, page_id: int) -> Optional[dict[str, Any]]:
        """Live page, or the cached copy (marked stale) when tela can't be reached."""
        try:
            page = self._page(page_id)
            self._write_cache(page)
            return page
        except TelaError as exc:
            logger.warning("tela memory: page %s: %s", page_id, exc)
        try:
            cached = json.loads(self._cache_path(page_id).read_text(encoding="utf-8"))
            cached["stale_since"] = cached.get("fetched_at")
            return cached
        except (OSError, ValueError):
            return None

    # -- prompt ----------------------------------------------------------------

    def _render_prompt(self) -> str:
        if not self._pages:
            return ""
        sections = []
        for page in self._pages:
            heading = f"## {page['title']} (page {page['id']})"
            if page.get("stale_since"):
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(page["stale_since"]))
                heading += f" [tela unreachable; cached copy from {when}]"
            sections.append(f"{heading}\n{page['body'].strip()}")
        return f"{SEPARATOR}\n{PROMPT_HEADER}\n{SEPARATOR}\n\n" + "\n\n".join(sections)

    # -- tool ------------------------------------------------------------------

    def _resolve_page(self, ref: str) -> Optional[int]:
        ref = str(ref or "").strip()
        if ref.isdigit() and int(ref) in self._page_ids:
            return int(ref)
        folded = _fold(ref)
        return next((p["id"] for p in self._pages if _fold(p["title"]) == folded), None)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        if tool_name != "tela_memory":
            return json.dumps({"success": False, "error": f"unknown tool {tool_name}"})
        try:
            return json.dumps(self._memory_tool(args or {}), ensure_ascii=False)
        except TelaError as exc:
            return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)

    def _memory_tool(self, args: dict[str, Any]) -> dict[str, Any]:
        action = args.get("action")
        page_id = self._resolve_page(args.get("page"))
        if page_id is None:
            titles = ", ".join(p["title"] for p in self._pages)
            return {"success": False, "error": f"unknown page {args.get('page')!r}; memory pages: {titles}"}

        if action == "read":
            page = self._page(page_id)
            self._write_cache(page)
            return {"success": True, "page": page["title"], "body": page["body"]}

        content, old_text = args.get("content") or "", args.get("old_text") or ""
        if action in ("add", "replace") and not content.strip():
            return {"success": False, "error": f"{action} needs content"}
        if action in ("replace", "remove") and not old_text:
            return {"success": False, "error": f"{action} needs old_text"}
        if action not in ("add", "replace", "remove"):
            return {"success": False, "error": f"unknown action {action!r}"}
        if content and (threat := _scan(content)):
            return {"success": False, "error": threat}

        for attempt in range(2):  # one retry when someone else edited the page meanwhile
            page = self._page(page_id)
            body = page["body"]
            if action == "add":
                new_body = f"{body.rstrip()}\n{content.strip()}\n" if body.strip() else f"{content.strip()}\n"
            else:
                count = body.count(old_text)
                if count != 1:
                    return {"success": False, "error": f"old_text found {count} times; it must match exactly once",
                            "hint": "use action=read for the current text"}
                if action == "replace":
                    new_body = body.replace(old_text, content.strip())
                elif body.count(old_text + "\n") == 1:  # a whole line: drop its newline too
                    new_body = body.replace(old_text + "\n", "")
                else:
                    new_body = body.replace(old_text, "")
            if len(new_body) > self._settings["max_chars"]:
                return {"success": False,
                        "error": f"page would be {len(new_body)} chars (limit {self._settings['max_chars']})",
                        "hint": "shorten or remove stale lines, or move details to another tela page"}
            try:
                saved = _page_fields(self._mcp("update_page", {
                    "id": page_id, "body": new_body, "base_version": page["version"]}))
            except TelaError as exc:
                if exc.status == 409 and attempt == 0:
                    continue
                raise
            self._write_cache(saved)
            return {"success": True, "done": True, "page": saved["title"], "chars": len(saved["body"]),
                    "note": "Saved. Shows in the prompt next session — do not repeat this write."}
        return {"success": False, "error": "page kept changing; try again"}


def register(ctx) -> None:
    ctx.register_memory_provider(TelaMemoryProvider())
