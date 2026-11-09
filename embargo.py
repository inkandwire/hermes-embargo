"""Embargo: embargoed text cannot leave through any tool until its release time.

Detect "EMBARGOED UNTIL <time>" in what the agent reads, keep hashed fingerprints (never raw text),
block outbound tool calls that carry the text, and lift by itself when the clock passes.
Stdlib only. No network, no LLM calls.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shlex
import threading
from contextlib import contextmanager
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote_plus

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: thread lock only
    fcntl = None
try:
    import hermes_constants
except ImportError:  # pragma: no cover - only outside Hermes
    hermes_constants = None

log = logging.getLogger("embargo")
_data_dir: Path | None = None
_lock = threading.RLock()
_clock = time.time  # tests replace this
_cache: dict = {}

SHINGLE = 6
MAX_SHINGLES = 20_000
MAX_NUMS = 20
ALLOW = {"read_file", "write_file", "patch", "search_files", "todo", "clarify", "skill_view", "skills_list"}
SELF_TOOLS = {"terminal", "execute_code", "write_file", "patch"}
READ_TOOLS = {"read_file", "web_extract"}
STATE_MSG = "Embargo state unreadable — /embargo list"
SELF_MSG = "Embargo protects its own state files. Use /embargo list, /embargo set or /embargo lift."
USAGE = ('Usage: /embargo list | add "<text>" until <time|+90s> | set <id> <time> | lift <id> | clear-lifted\n'
         "Time: 2026-11-12T09:00:00+08:00, 09:00 MYT, 12 November 2026, or +90s / +5m / +2h")


class StateError(Exception):
    pass


# ---------------------------------------------------------------- state
def init(data_dir) -> None:
    global _data_dir
    _data_dir = Path(data_dir)
    _cache.clear()


def data() -> Path:
    if _data_dir:
        return _data_dir
    return _home() / "plugin-data" / "embargo"


def _home() -> Path:
    if hermes_constants:
        return Path(hermes_constants.get_hermes_home())
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _state_path() -> Path:
    return data() / "embargoes.json"


def _atomic_write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def _parse_state(text: str) -> dict:
    try:
        s = json.loads(text)
        if not isinstance(s, dict) or not isinstance(s.get("items"), list) or not all(isinstance(i, dict) and "id" in i for i in s["items"]):
            raise ValueError("bad shape")
        s.setdefault("next", 1)
        return s
    except (ValueError, TypeError) as e:
        raise StateError(str(e)) from e


def _load() -> dict:
    """Cached, read-only view of the state. Mutate only through _update()."""
    p = _state_path()
    try:
        st = p.stat()
    except FileNotFoundError:
        return {"next": 1, "items": []}
    key = (str(p), st.st_mtime_ns, st.st_size, st.st_ino)
    hit = _cache.get("state")
    if hit and hit[0] == key:
        return hit[1]
    try:
        s = _parse_state(p.read_text(encoding="utf-8"))
    except OSError as e:
        raise StateError(str(e)) from e
    _cache["state"] = (key, s)
    return s


@contextmanager
def _flock():
    """One lock across processes (CLI, gateway, cron) so load-modify-save cannot lose an update."""
    with _lock:
        if fcntl is None:
            yield
            return
        data().mkdir(parents=True, exist_ok=True)
        with open(data() / ".lock", "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield


def _update(fn):
    with _flock():
        p = _state_path()
        s = _parse_state(p.read_text(encoding="utf-8")) if p.exists() else {"next": 1, "items": []}
        out = fn(s)
        _atomic_write(p, s)
        _cache.pop("state", None)
        return out


def _sid(session_id) -> str:
    return re.sub(r"[^\w.-]", "_", str(session_id or "default"))[:80]


def _session_update(session_id, fn):
    with _flock():
        p = data() / "held" / f"{_sid(session_id)}.json"
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            s = {}
        for k, v in (("detect", []), ("blocks", []), ("lifted", [])):
            if not isinstance(s.get(k), list):
                s[k] = v
        out = fn(s)
        _atomic_write(p, s)
        return out


@lru_cache(maxsize=64)
def _hset(hexstr: str) -> frozenset:
    return frozenset(int(hexstr[i:i + 16], 16) for i in range(0, len(hexstr), 16))


def _active(it: dict, now: float) -> bool:
    return not it.get("lifted_at") and (it.get("until") is None or now < it["until"])


# ---------------------------------------------------------------- text: normalize, tokens, hashes
_CF = re.compile("[" + "".join(map(chr, (0xAD, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2061, 0x2062, 0x2063, 0x2064, 0xFEFF))) + "]")
_TOK = re.compile(r"[\w$%]+(?:[.,]\d+)*")


def _norm(s: str) -> str:
    s = unquote_plus(s)
    if "&" in s:
        s = html.unescape(s)
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"\\[nrt]", " ", s)  # a literal \n inside a quoted shell or JSON string
    return _CF.sub("", s).casefold()


def _h(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8).digest(), "big")


def _shingles(toks: list, n: int):
    for i in range(len(toks) - n + 1):
        yield _h(" ".join(toks[i:i + n]))


_NUM = re.compile(r"(?P<cur>[$€£¥])?\s?(?P<n>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?P<u>%|percent|per cent|billion|bn|b|million|mln|mn|mm|m|thousand|k)?(?![a-z\d])")
_MULT = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mln": 1e6, "mn": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}


def _numbers(norm_text: str) -> set:
    """Money, percent and large numbers as one normalized form: `$40M` and `40 million` both give 40000000."""
    out = set()
    for m in _NUM.finditer(norm_text):
        n, u, cur = m["n"], m["u"], m["cur"]
        if not (cur or u or "," in n or len(n.split(".")[0]) >= 5):
            continue  # a bare 4-digit number is probably a year
        try:
            v = float(n.replace(",", ""))
        except ValueError:
            continue
        out.add(f"{v:g}%" if u in ("%", "percent", "per cent") else f"{v * _MULT.get(u, 1):.0f}")
    return out


_STOP = {"the", "and", "for", "with", "from", "into", "series", "round", "raises", "announces", "inc", "llc", "ltd", "new", "its"}


def _names(label: str) -> set:
    return {w.casefold() for w in re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", label)} - _STOP


# ---------------------------------------------------------------- time
_FIXED = {"UTC": 0, "GMT": 0, "Z": 0, "EST": -5 * 3600, "EDT": -4 * 3600, "CST": -6 * 3600, "CDT": -5 * 3600, "MST": -7 * 3600, "MDT": -6 * 3600,
          "PST": -8 * 3600, "PDT": -7 * 3600, "CET": 3600, "CEST": 7200, "BST": 3600, "AEST": 36000, "AEDT": 39600, "MYT": 8 * 3600, "SGT": 8 * 3600, "HKT": 8 * 3600, "JST": 9 * 3600, "KST": 9 * 3600, "IST": 19800}
_IANA = {"ET": "America/New_York", "CT": "America/Chicago", "MT": "America/Denver", "PT": "America/Los_Angeles"}
_OFF = re.compile(r"\b(?:UTC|GMT)\s*([+-])(\d{1,2})(?::?(\d{2}))?\b")
_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_MON = r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE1 = re.compile(_MON + r"\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})", re.I)
_DATE2 = re.compile(r"(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"\.?,?\s+(?P<y>\d{4})", re.I)
_DATE3 = re.compile(r"(?<![\d/])(?P<mo>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}|\d{2})(?![\d/])")
_DATE4 = re.compile(r"\b" + _MON + r"\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?\b(?!\s*,?\s*\d{4})", re.I)  # no year: the next such date
_AMPM = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\b\.?", re.I)
_H24 = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_ZONE = re.compile(r"\b(" + "|".join(sorted(list(_FIXED) + list(_IANA), key=len, reverse=True)) + r")\b")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*(Z|[+-]\d{2}:?\d{2})")


def _offset_tz(txt: str):
    sign = -1 if txt[0] == "-" else 1
    d = txt[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(d[:2]), minutes=int(d[2:])))


def _tzinfo(tz: str):
    if tz in _FIXED:
        return timezone(timedelta(seconds=_FIXED[tz]))
    if tz in _IANA:
        from zoneinfo import ZoneInfo
        return ZoneInfo(_IANA[tz])
    return _offset_tz(tz)


def _local_tz_label(ts: float) -> str:
    off = datetime.fromtimestamp(ts).astimezone().utcoffset() or timedelta(0)
    m = int(off.total_seconds() // 60)
    return f"{'-' if m < 0 else '+'}{abs(m) // 60:02d}:{abs(m) % 60:02d}"


def _ymd(s: str):
    m = _DATE1.search(s) or _DATE2.search(s) or _DATE4.search(s)
    if m:
        return _MONTHS[m["mon"][:3].lower()], int(m["d"]), m.groupdict().get("y")
    m = _DATE3.search(s)
    return (int(m["mo"]), int(m["d"]), m["y"]) if m else None


def parse_time(s: str, now: float | None = None):
    """Return (epoch_seconds, tz_label) or None. None means: unreadable, so the caller keeps the hold."""
    s = (s or "").strip()
    now = _clock() if now is None else now
    try:
        m = re.fullmatch(r"\+(\d+)\s*([smhd])", s, re.I)
        if m:
            ts = now + int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2].lower()]
            return int(ts), _local_tz_label(ts)
        m = _ISO.search(s)
        if m:
            y, mo, d, H, M, S, z = m.groups()
            tz = "UTC" if z == "Z" else (z if ":" in z else z[:3] + ":" + z[3:])
            return int(datetime(int(y), int(mo), int(d), int(H), int(M), int(S or 0), tzinfo=_tzinfo(tz)).timestamp()), tz
        off = _OFF.search(s)
        names = set(_ZONE.findall(s)) - ({"UTC", "GMT"} if off else set())
        if len(names) + bool(off) > 1:
            return None  # two different zones in one line: do not guess, keep the hold
        zone = (None if not (off or names) else
                [f"{off[1]}{int(off[2]):02d}:{off[3] or '00'}" if off else names.pop()])
        date = _ymd(s)
        t = _AMPM.search(s)
        if t:
            H, M = int(t[1]) % 12 + (12 if t[3].lower() == "p" else 0), int(t[2] or 0)
            if not 1 <= int(t[1]) <= 12:
                return None
        else:
            t = _H24.search(s)
            if not t:
                return None
            H, M = int(t[1]), int(t[2])
        if not (zone and date):
            return None
        zone = zone[0]
        mon, d, y = date
        tzi = _tzinfo(zone)
        if y is None:  # no year given: the next such date, counted in the stated zone
            y = datetime.fromtimestamp(now, tzi).year
            if datetime(y, mon, d, H, M, tzinfo=tzi).timestamp() <= now:
                y += 1
        y = int(y) + (2000 if int(y) < 100 else 0)
        dt = datetime(y, mon, d, H, M, tzinfo=tzi)
        return int(dt.timestamp()), zone
    except Exception:  # bad date, missing tzdata, odd input: all mean "unreadable"
        return None


def _fmt(until, tz: str) -> str:
    if until is None:
        return "a time you set"
    dt = datetime.fromtimestamp(until, _tzinfo(tz or "UTC"))
    zone = tz if tz.isalpha() else f"UTC{tz}" if tz else "UTC"
    return f"{dt:%a %d %b %H:%M} {zone}"


def _left(sec: float) -> str:
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    if sec < 86400:
        return f"{sec // 3600}h{sec % 3600 // 60:02d}m"
    return f"{sec // 86400}d{sec % 86400 // 3600:02d}h"


# ---------------------------------------------------------------- detect and fingerprint
_HDR = re.compile(r"(?im)^[ \t>*#_\[(-]*(?:EMBARGOED\s+UNTIL|UNDER\s+EMBARGO\s+UNTIL|(?P<bare>EMBARGO\s*:|HOLD\s+FOR\s+RELEASE)|NOT\s+FOR\s+RELEASE\s+(?:BEFORE|UNTIL))[ \t:]*(?P<rest>[^\n]*)$")
_END = re.compile(r"(?i)^\s*(?:about\s|media\s+contact|#{3,}\s*$|-30-\s*$)")


def _clean_label(s: str) -> str:
    s = re.sub(r"[^\w .,'’&$%:;/()#+-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:80]


def _body(text: str) -> str:
    out = []
    for line in text.splitlines():
        if _END.match(line):
            break
        out.append(line)
    return "\n".join(out)


def _label(body: str, fallback: str) -> str:
    for line in body.splitlines():
        line = re.sub(r"^[#*_>\s-]+", "", line)
        if len(line.split()) >= 3:
            return _clean_label(line) or fallback
    return fallback


def _make_item(body: str, label: str, until, tz: str, source: str, paths: list) -> dict:
    toks = _TOK.findall(_norm(body))
    terms = []
    if 0 < len(toks) < SHINGLE and len(toks) <= 5:  # too short to shingle: a user term (codename), hashed as a token run
        terms.append([len(toks), f"{_h(' '.join(toks)):016x}"])
    hashes = list(dict.fromkeys(_shingles(toks, SHINGLE)))[:MAX_SHINGLES]
    nums = sorted(_h(n) for n in _numbers(_norm(body)))[:MAX_NUMS]
    names = sorted(_h(n) for n in _names(label))[:6]
    return {"label": label, "until": until, "tz": tz, "hashes": "".join(f"{h:016x}" for h in hashes), "terms": terms,
            "nums": [f"{n:016x}" for n in nums], "names": [f"{n:016x}" for n in names], "paths": paths,
            "source": source, "added": int(_clock()), "lifted_at": None}


def _commit(item: dict, session_id: str = "") -> str | None:
    """Add the item unless the same document is already held. Returns the new id, or None for a duplicate."""
    def fn(s):
        hs = _hset(item["hashes"])
        for it in s["items"]:
            old = _hset(it["hashes"])
            same = (hs and old and len(hs & old) / len(hs) >= 0.8) or (not hs and not old and it["label"] == item["label"] and it["source"] == item["source"])
            if same and it.get("until") == item["until"]:
                it["paths"] = list(dict.fromkeys(it["paths"] + item["paths"]))
                return None
        item["id"] = f"E{s['next']}"
        s["next"] += 1
        s["items"].append(item)
        return item["id"]
    new = _update(fn)
    if new and session_id:
        _session_update(session_id, lambda S: S["detect"].append(new))
    return new


def ingest(text: str, source: str = "pasted", path: str | None = None, session_id: str = "") -> str | None:
    """Find an embargo header in `text`; hold what follows it. Returns the new embargo id."""
    for m in _HDR.finditer(text or ""):
        parsed = parse_time(m["rest"])
        if parsed or not m["bare"]:  # a bare `EMBARGO:` line needs a readable time to count
            break
    else:
        return None
    until, tz = parsed if parsed else (None, "")
    if until is not None and until <= _clock():
        return None  # the release time has passed: nothing to hold
    body = _body(text[m.end():])
    paths = [os.path.normpath(os.path.expanduser(path))] if path else []
    return _commit(_make_item(body, _label(body, os.path.basename(path or "") or "Embargoed text"), until, tz, source, paths), session_id)


# ---------------------------------------------------------------- hold
def _strings(obj, out=None, depth=0) -> list:
    out = [] if out is None else out
    if isinstance(obj, str):
        out.append(obj)
    elif depth < 12 and isinstance(obj, dict):
        for k, v in obj.items():
            _strings(k, out, depth + 1)
            _strings(v, out, depth + 1)
    elif depth < 12 and isinstance(obj, (list, tuple)):
        for v in obj:
            _strings(v, out, depth + 1)
    return out


def _scan(items: list, strs: list, check_paths: bool) -> list:
    """Return [(item, reason)] with reason in text | path | facts."""
    raw = "\n".join(strs)
    norm = _norm(raw)
    toks = _TOK.findall(norm)
    cache: dict = {}

    def shingles(n):
        if n not in cache:
            cache[n] = set(_shingles(toks, n))
        return cache[n]
    nums = _numbers(norm)
    num_h = {_h(n) for n in nums} if nums else set()
    tok_h = None
    hits = []
    for it in items:
        if check_paths and any(re.search(rf"(?<![\w.-]){re.escape(os.path.basename(p))}(?![\w-])", raw) for p in it["paths"] if os.path.basename(p)):
            hits.append((it, "path"))
        elif it["hashes"] and shingles(SHINGLE) & _hset(it["hashes"]):
            hits.append((it, "text"))
        elif any(shingles(n) & {int(h, 16)} for n, h in it["terms"]):
            hits.append((it, "text"))
        elif it["names"] and it["nums"] and num_h and num_h & {int(h, 16) for h in it["nums"]}:
            tok_h = tok_h if tok_h is not None else {_h(t) for t in set(toks)}
            if tok_h & {int(h, 16) for h in it["names"]}:
                hits.append((it, "facts"))
    return hits


def _block(msg: str) -> dict:
    return {"action": "block", "message": msg}


def _why(it: dict, reason: str, now: float) -> str:
    what = {"text": "its text matches", "path": "it names a held file", "facts": "key facts match"}[reason]
    when = ("held until the user sets a time (/embargo set)" if it.get("until") is None
            else f"lifts {_fmt(it['until'], it['tz'])}, {_left(it['until'] - now)} left")
    return (f'Embargo {it["id"]} "{it["label"]}": {what}; {when}. '
            "Do not reword it to get around the hold; tell the user, or save a local draft.")


def _taint(it_ids: list, paths: list) -> None:
    def fn(s):
        for it in s["items"]:
            if it["id"] in it_ids:
                it["paths"] = list(dict.fromkeys(it["paths"] + paths))
    _update(fn)


def guard(tool_name: str = "", args=None, session_id: str = "", **_kw):
    """pre_tool_call: veto a tool call that carries held text. Fails closed while embargoes exist."""
    try:
        return _guard(tool_name, args if isinstance(args, dict) else {}, session_id)
    except Exception as e:
        log.warning("embargo guard error: %r", e)
        try:
            unknown_or_active = any(_active(i, _clock()) for i in _load()["items"])
        except Exception:
            unknown_or_active = True  # state unreadable: we cannot prove there is no embargo
        return _block(STATE_MSG) if unknown_or_active else None


def _guard(tool, args: dict, session_id: str):
    strs = _strings(args)
    if tool in SELF_TOOLS and any("embargoes.json" in s or str(data()) in s for s in strs):
        return _block(SELF_MSG)
    allow = tool in ALLOW or tool.startswith("embargo_")
    if allow and tool not in ("write_file", "patch"):
        return None
    now = _clock()
    active = [i for i in _load()["items"] if _active(i, now)]
    if not active:
        return None
    hits = _scan(active, strs, check_paths=not allow)
    if allow:  # write_file / patch: allowed, but the file now carries held text
        if hits:
            text = "\n".join(strs)
            files = [args.get("path")] + re.findall(r"\*\*\* (?:Update|Add) File: (.+)", text)
            _taint([i["id"] for i, _ in hits], [os.path.normpath(os.path.expanduser(f)) for f in files if isinstance(f, str) and f])
        return None
    if not hits:
        return None
    it, reason = hits[0]
    _session_update(session_id, lambda S: S["blocks"].append({"id": it["id"], "tool": re.sub(r"[^\w.-]", "", tool)[:40], "t": int(now), "c": 0}))
    return _block(_why(it, reason, now) + (f" Also held: {', '.join(i['id'] for i, _ in hits[1:])}." if len(hits) > 1 else ""))


# ---------------------------------------------------------------- observe (post_tool_call, pre_llm_call)
def _result_text(result) -> str:
    try:
        return "\n".join(_strings(json.loads(result))) if isinstance(result, str) else "\n".join(_strings(result))
    except ValueError:
        return result if isinstance(result, str) else ""


def observe(tool_name: str = "", args=None, result=None, session_id: str = "", **_kw):
    """post_tool_call: look for an embargo header in files and pages the agent read. Return value is ignored by Hermes."""
    if tool_name not in READ_TOOLS and not tool_name.startswith("browser_"):
        return None
    try:
        text = _result_text(result)
        if not re.search(r"embargo|not\s+for\s+release|hold\s+for\s+release", text, re.I):
            return None
        path = None
        if tool_name == "read_file":
            text = re.sub(r"(?m)^\s*\d+\|", "", text)  # drop the line-number gutter
            path = (args or {}).get("path") if isinstance(args, dict) else None
        ingest(text, path or tool_name, path if isinstance(path, str) else None, session_id)
    except Exception as e:  # an observer must never break the turn
        log.warning("embargo observe error: %r", e)
    return None


def _context_line(it: dict, now: float) -> str:
    when = "has no release time; held until the user sets one" if it.get("until") is None else f"lifts {_fmt(it['until'], it['tz'])}"
    return f'Embargo {it["id"]} "{it["label"]}" {when}.'


def on_turn(session_id: str = "", user_message: str = "", **_kw):
    """pre_llm_call: detect pasted embargoed text, then remind the model what is held (label and time only)."""
    try:
        if isinstance(user_message, str) and _HDR.search(user_message):
            ingest(user_message, "pasted", None, session_id)
        if os.environ.get("EMBARGO_REMINDER", "").strip().lower() == "off":
            return None  # reminder off: only the hard guard remains (for testing it)
        now = _clock()
        lines = [_context_line(i, now) for i in _load()["items"] if _active(i, now)][:5]
        if lines:
            return {"context": "Embargoed text is held by the Embargo plugin. Do not send it, or a reworded copy, through any tool before it lifts. "
                    + " ".join(lines)}
    except Exception as e:
        log.warning("embargo on_turn error: %r", e)
    return None


# ---------------------------------------------------------------- cards (transform_llm_output)
_FORGED = re.compile(r"(?m)^[ \t]*::embargo(?:\{[^{}]*\})?[ \t]*$\n?|::embargo(?:\{[^{}]*\})?")


def _desktop_half_ready(plugin: str) -> bool:
    home = _home()
    roots = [home] + ([home.parent.parent] if home.parent.name == "profiles" else [])
    return any((r / "desktop-plugins" / plugin / "plugin.js").is_file() for r in roots)


def _card(state: str, it: dict, tool: str = "", n: int = 0, desktop: bool = False, now: float = 0) -> str:
    label, until, tz = it["label"], it.get("until"), it.get("tz") or "UTC"
    if desktop:
        a = f'state="{state}" id="{it["id"]}" label="{label}" until="{int(until or 0)}" tz="{re.sub(r"[^A-Za-z0-9+:-]", "", tz)[:12]}"'
        return "::embargo{" + a + (f' tool="{tool}" n="{n}"' if n else "") + "}"
    blocked = f" Blocked: {tool}." if tool else ""
    if state == "lifted":
        return f'✅ EMBARGO LIFTED — {it["id"]} "{label}". Ask again to send it.'
    if state == "detected":
        return f'⚠ EMBARGO DETECTED — {it["id"]} "{label}" lifts {_fmt(until, tz)}. Held from now on.'
    if until is None:
        return f'⛔ HELD, NO RELEASE TIME — {it["id"]} "{label}". Held until you set one: /embargo set {it["id"]} <time>.{blocked}'
    return f'⛔ HELD FOR RELEASE — {it["id"]} "{label}" lifts {_fmt(until, tz)} (in {_left(until - now)}).{blocked}'


def card_hook(response_text: str = "", session_id: str = "", platform: str = "", **_kw):
    """transform_llm_output: drop forged cards the model wrote, then append a card for each new event."""
    src = response_text
    if isinstance(src, str):
        response_text = _FORGED.sub("", src)
        if response_text != src:
            response_text = response_text.rstrip()
    changed = isinstance(src, str) and response_text != src
    try:
        cards = _new_cards(session_id, platform)
    except Exception as e:
        log.warning("embargo card error: %r", e)
        cards = []
    if not cards or not isinstance(response_text, str):
        return (response_text or "(Embargo removed a forged card.)") if changed else None
    return (response_text + "\n\n" if response_text else "") + "\n\n".join(cards)


def _new_cards(session_id: str, platform: str) -> list:
    now = _clock()
    items = {i["id"]: i for i in _load()["items"]}
    desktop = platform == "desktop" and _desktop_half_ready(Path(__file__).parent.name)
    out = []

    def fn(S):
        for eid in S["detect"]:
            it = items.get(eid)
            if it:
                out.append(_card("unset" if it.get("until") is None else "detected", it, desktop=desktop, now=now))
        S["detect"] = []
        fresh: dict = {}
        for b in S["blocks"]:
            if not b.get("c"):
                fresh.setdefault(b["id"], []).append(b["tool"])
                b["c"] = 1
        for eid, tools in fresh.items():
            it = items.get(eid)
            if it and _active(it, now):
                tool = ", ".join(f"{t} ×{tools.count(t)}" for t in dict.fromkeys(tools))
                out.append(_card("unset" if it.get("until") is None else "held", it, tool if not desktop else tools[-1], len(tools), desktop, now))
        for eid in dict.fromkeys(b["id"] for b in S["blocks"]):
            it = items.get(eid)
            if it and not _active(it, now) and eid not in S["lifted"]:
                S["lifted"].append(eid)
                out.append(_card("lifted", it, "", sum(b["id"] == eid for b in S["blocks"]), desktop, now))
    if items:
        _session_update(session_id, fn)
    return out


# ---------------------------------------------------------------- tools and command
def _status_row(it: dict, now: float) -> dict:
    act = _active(it, now)
    return {"id": it["id"], "label": it["label"], "until_iso": None if it.get("until") is None else datetime.fromtimestamp(it["until"], timezone.utc).isoformat(),
            "seconds_left": None if it.get("until") is None or not act else max(0, int(it["until"] - now)), "held": act}


def tool_add(args: dict, kw: dict) -> dict:
    text = args.get("text")
    if not isinstance(text, str) or not text.strip():
        return {"ok": False, "error": "text is required"}
    parsed = parse_time(str(args.get("until") or ""))
    if parsed and parsed[0] <= _clock():
        return {"ok": False, "error": "that time has already passed; nothing to hold"}
    until, tz = parsed if parsed else (None, "")
    label = _clean_label(str(args.get("label") or "")) or _label(text, "") or _clean_label(" ".join(text.split()[:6]))
    eid = _commit(_make_item(text, label, until, tz, "tool", []), str(kw.get("session_id") or ""))
    if eid is None:
        return {"ok": True, "note": "already held"}
    it = next(i for i in _load()["items"] if i["id"] == eid)
    row = _status_row(it, _clock())
    out = {"ok": True, "id": eid, "until_iso": row["until_iso"], "seconds_left": row["seconds_left"]}
    if until is None:
        out["note"] = "time unreadable: held until the user sets one with /embargo set"
    return out


def tool_status(args: dict, kw: dict) -> dict:
    now = _clock()
    return {"ok": True, "embargoes": [_status_row(i, now) for i in _load()["items"]]}


def slash(raw_args: str = "", **_kw) -> str:
    try:
        parts = shlex.split(raw_args or "")
    except ValueError:
        return USAGE
    verb = parts[0] if parts else "list"
    try:
        if verb == "list" and len(parts) <= 1:
            return _list()
        if verb == "add" and len(parts) >= 4 and parts[2] == "until":
            parsed = parse_time(" ".join(parts[3:]))
            if not parsed:
                return "Error: unreadable time. " + USAGE.splitlines()[1]
            if parsed[0] <= _clock():
                return "Error: that time has already passed."
            eid = _commit(_make_item(parts[1], _label(parts[1], "") or _clean_label(" ".join(parts[1].split()[:6])), parsed[0], parsed[1], "manual", []))
            return f"Held as {eid}. Lifts {_fmt(*parsed)}." if eid else "Already held."
        if verb in ("set", "lift") and len(parts) >= 2 and re.fullmatch(r"E\d{1,4}", parts[1]):
            return _set_or_lift(verb, parts[1], " ".join(parts[2:]))
        if verb == "clear-lifted" and len(parts) == 1:
            now = _clock()
            return f"Removed {_update(lambda s: _drop(s, now))} lifted item(s)."
    except StateError:
        return f"Error: embargo state unreadable. Move {_state_path()} aside to start fresh (held items are forgotten)."
    return USAGE


def _drop(s: dict, now: float) -> int:
    n = len(s["items"])
    s["items"] = [i for i in s["items"] if _active(i, now)]
    return n - len(s["items"])


def _list() -> str:
    try:
        items, now = _load()["items"], _clock()
    except StateError:
        return f"Error: embargo state unreadable. Move {_state_path()} aside to start fresh (held items are forgotten)."
    rows = []
    for i in items:
        state = "held" if _active(i, now) else "lifted"
        when = "no time set" if i.get("until") is None else f"lifts {_fmt(i['until'], i['tz'])}" + (f" (in {_left(i['until'] - now)})" if state == "held" else "")
        rows.append(f'{i["id"]}  {state:6}  "{i["label"]}"  {when}  files:{len(i["paths"])}')
    return "\n".join(rows) or "No embargoes."


def _set_or_lift(verb: str, eid: str, rest: str) -> str:
    now = _clock()
    parsed = None
    if verb == "set":
        parsed = parse_time(rest)
        if not parsed:
            return "Error: unreadable time. " + USAGE.splitlines()[1]

    def fn(s):
        for it in s["items"]:
            if it["id"] == eid:
                if verb == "lift":
                    it["lifted_at"] = int(now)
                else:
                    it["until"], it["tz"], it["lifted_at"] = parsed[0], parsed[1], None
                return True
        return False
    if not _update(fn):
        return f"Error: no embargo {eid}."
    return f"{eid} lifted." if parsed is None else f"{eid} now lifts {_fmt(*parsed)}."
