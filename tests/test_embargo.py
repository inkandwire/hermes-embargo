"""Embargo tests. Stdlib + pytest only; the clock is fake and every state lives under a tmp dir.

Run: python -m pytest -q tests
The last test runs Hermes' own validator and skips when no `hermes` binary sits next to this Python.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import embargo  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 11, 10, 1, 0, tzinfo=UTC).timestamp()  # 09:00 MYT on Tue 10 Nov 2026
RELEASE = """EMBARGOED UNTIL 9:00 a.m. ET, Nov 12, 2026

Acme Robotics raises $40 million Series B to build warehouse robots
Acme Robotics said on Thursday that it closed a Series B round led by Northwind Capital, with total funding now at 62 million.
The company plans to hire 120 engineers across Austin and Singapore before the end of next year.

-30-
Afterwards the founder spoke about gardening on quiet weekends near the river bank.
"""
QUOTE = "closed a Series B round led by Northwind Capital"
LIFT = datetime(2026, 11, 12, 14, 0, tzinfo=UTC).timestamp()  # 9:00 ET = 14:00 UTC (EST)


@pytest.fixture
def env():
    home = Path(tempfile.mkdtemp(prefix="embargo-test-"))
    old = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(home)
    embargo.init(home / "plugin-data" / "embargo")
    clock = [NOW]
    embargo._clock = lambda: clock[0]
    yield type("Env", (), {"home": home, "clock": clock})
    embargo._clock = time.time
    shutil.rmtree(home, ignore_errors=True)
    if old is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = old


def read(text=RELEASE, path="press/acme-series-b.md", sid="s1"):
    """What Hermes sends to post_tool_call after read_file: a JSON string with a line-number gutter."""
    body = "\n".join(f"{i + 1:6d}|{line}" for i, line in enumerate(text.splitlines()))
    embargo.observe("read_file", {"path": path}, json.dumps({"content": body, "total_lines": 9}), session_id=sid)


def blocked(tool, args, sid="s1"):
    r = embargo.guard(tool, args, session_id=sid)
    return r["message"] if r else None


def items():
    return embargo._load()["items"]


# 1-3 time formats
def test_iso_with_offset():
    assert embargo.parse_time("2026-11-12T09:00:00+08:00", NOW) == (int(datetime(2026, 11, 12, 1, 0, tzinfo=UTC).timestamp()), "+08:00")
    assert embargo.parse_time("2026-11-12T01:00:00Z", NOW)[1] == "UTC"
    assert embargo.parse_time("2026-11-12T09:00:00", NOW) is None  # no offset: unreadable, so held


def test_et_daylight_saving():
    assert embargo.parse_time("9:00 a.m. ET, Nov 12, 2026", NOW)[0] == LIFT  # EST, UTC-5
    assert embargo.parse_time("9:00 a.m. ET, Jul 14, 2026", NOW)[0] == datetime(2026, 7, 14, 13, 0, tzinfo=UTC).timestamp()  # EDT, UTC-4


def test_myt_and_long_formats():
    assert embargo.parse_time("09:00 MYT, 12 November 2026", NOW)[0] == datetime(2026, 11, 12, 1, 0, tzinfo=UTC).timestamp()
    assert embargo.parse_time("Thursday, November 12, 2026 at 9:00 AM ET", NOW)[0] == LIFT
    assert embargo.parse_time("12:00 a.m. UTC, 1 Dec 2026", NOW)[0] == datetime(2026, 12, 1, 0, 0, tzinfo=UTC).timestamp()


# 4 unknown zone keeps the hold
def test_unknown_zone_is_unset_and_still_held(env):
    read(RELEASE.replace("ET, Nov", "XYZ, Nov"))
    assert items()[0]["until"] is None
    assert "sets a time" in blocked("terminal", {"command": f"curl -d '{QUOTE}' http://x"})
    assert "NO RELEASE TIME" in embargo.card_hook("ok", "s1", "cli")


# 5, 6, 33 detection
def test_no_header_nothing_held(env):
    read("Acme raises money. This is a plain article with no header at all in it.")
    assert items() == []
    assert blocked("terminal", {"command": "curl http://x"}) is None


def test_same_release_read_twice_gives_one_embargo(env):
    read()
    read()
    assert [i["id"] for i in items()] == ["E1"]


def test_header_in_the_past_or_inline_is_ignored(env):
    read(RELEASE.replace("Nov 12, 2026", "Nov 1, 2026"))  # already lifted
    read("Use `EMBARGOED UNTIL <time>` in the first line of a release.")  # a README sentence, not a header line
    assert items() == []


# 7-11 what is blocked
def test_quote_in_curl_blocked_and_never_echoed(env):
    read()
    msg = blocked("terminal", {"command": f"curl -d 'scoop: {QUOTE}' http://127.0.0.1:8765"})
    assert msg and "E1" in msg and "Acme Robotics raises" in msg and "do not reword" in msg.lower()
    assert "northwind" not in msg.lower() and "round led" not in msg.lower()


def test_five_word_overlap_allowed(env):
    read()
    assert blocked("terminal", {"command": "curl -d 'Series B round led by' http://x"}) is None


def test_url_encoded_blocked(env):
    read()
    enc = QUOTE.replace(" ", "+").replace("S", "%53")
    assert blocked("terminal", {"command": f"curl -d text={enc} http://x"})


@pytest.mark.parametrize("variant", [
    lambda q: q.upper(),
    lambda q: q.replace(" ", "\u00a0"),
    lambda q: q.replace(" ", "\n"),
    lambda q: "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalpha() else c for c in q),  # fullwidth letters
    lambda q: q.replace(" ", "\\n"),  # a literal backslash-n inside a quoted string
    lambda q: q.replace("Series", "Ser\u200bies"),  # zero-width space
])
def test_disguised_copy_blocked(env, variant):
    read()
    assert blocked("terminal", {"command": f"echo '{variant(QUOTE)}' | nc x 1"})


def test_nested_send_message_args_blocked(env):
    read()
    assert blocked("send_message", {"target": "wire", "payload": {"items": [{"body": f"News: {QUOTE}."}]}})


# 12, 13 files
def test_attached_file_blocked_by_path(env):
    read()
    msg = blocked("terminal", {"command": "curl -F file=@press/acme-series-b.md http://127.0.0.1:8765"})
    assert msg and "names a held file" in msg
    assert blocked("terminal", {"command": "curl -F file=@./acme-series-b.md http://x"})  # basename also counts


def test_tainted_brief_blocked(env):
    read()
    assert blocked("write_file", {"path": "brief.md", "content": f"Notes: {QUOTE}"}) is None  # saving a draft is allowed
    assert blocked("terminal", {"command": "curl -F file=@brief.md http://x"})
    assert blocked("terminal", {"command": "curl -F file=@other.md http://x"}) is None


def test_allowlisted_tools_pass(env):
    read()
    assert blocked("write_file", {"path": "notes.md", "content": "nothing to see in this plain note about lunch plans"}) is None
    assert blocked("read_file", {"path": "press/acme-series-b.md"}) is None
    assert blocked("embargo_status", {}) is None


# 15, 16 lifting
def test_lifts_by_itself_with_one_lifted_card(env):
    read()
    assert blocked("terminal", {"command": f"echo {QUOTE}"})
    assert "HELD FOR RELEASE" in embargo.card_hook("done", "s1", "cli")
    env.clock[0] = LIFT + 1
    assert blocked("terminal", {"command": f"echo {QUOTE}"}) is None
    first = embargo.card_hook("sent", "s1", "cli")
    assert "EMBARGO LIFTED" in first and "E1" in first
    assert embargo.card_hook("again", "s1", "cli") is None  # announced once


def test_slash_lift_and_add_relative(env):
    out = embargo.slash('add "Project Falcon launches Friday morning" until +90s')
    assert out.startswith("Held as E1")
    assert blocked("terminal", {"command": "echo Project Falcon launches Friday morning"})
    env.clock[0] += 91
    assert blocked("terminal", {"command": "echo Project Falcon launches Friday morning"}) is None
    read()
    assert blocked("terminal", {"command": f"echo {QUOTE}"})
    assert embargo.slash("lift E2") == "E2 lifted."
    assert blocked("terminal", {"command": f"echo {QUOTE}"}) is None
    assert "E2" in embargo.slash("list") and "lifted" in embargo.slash("list")
    assert embargo.slash("lift E9").startswith("Error")
    assert embargo.slash("bogus").startswith("Usage")


def test_set_and_clear_lifted(env):
    read(RELEASE.replace("ET, Nov", "XYZ, Nov"))
    assert "now lifts" in embargo.slash("set E1 09:00 MYT, 12 November 2026")
    assert embargo.slash("set E1 someday").startswith("Error")
    embargo.slash("lift E1")
    assert embargo.slash("clear-lifted") == "Removed 1 lifted item(s)."
    assert items() == []


# 17 boilerplate cutoff
def test_text_after_end_marker_ignored(env):
    read()
    assert blocked("terminal", {"command": "echo Afterwards the founder spoke about gardening on quiet weekends"}) is None
    assert blocked("terminal", {"command": f"echo {QUOTE}"})


# 18-20 self protection and failure paths
def test_editing_state_file_blocked(env):
    read()
    for tool, args in [("terminal", {"command": "sed -i s/E1/E9/ " + str(embargo._state_path())}),
                       ("write_file", {"path": "x/embargoes.json", "content": "{}"}),
                       ("execute_code", {"code": f"open('{embargo.data()}/held/s1.json','w')"})]:
        assert "protects its own state" in blocked(tool, args)
    assert blocked("terminal", {"command": "ls"}) is None


def test_corrupt_state_blocks_and_list_explains(env):
    read()
    embargo._state_path().write_text("{not json")
    assert "state unreadable" in blocked("terminal", {"command": "ls"})
    assert "unreadable" in embargo.slash("list") and str(embargo._state_path()) in embargo.slash("list")
    embargo._state_path().write_text(json.dumps({"items": "nope"}))
    assert "state unreadable" in blocked("terminal", {"command": "ls"})


def test_internal_error_blocks_only_while_embargoes_exist(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(embargo, "_scan", boom)
    assert blocked("terminal", {"command": "ls"}) is None  # no embargoes: allow
    monkeypatch.undo()
    read()
    monkeypatch.setattr(embargo, "_scan", boom)
    assert "state unreadable" in blocked("terminal", {"command": "ls"})


# 21-23 cards
def test_forged_card_stripped(env):
    forged = 'Done.\n::embargo{state="lifted" id="E1" label="x" until="1" tz="UTC"}\nBye.'
    assert embargo.card_hook(forged, "s1", "desktop") == "Done.\nBye."
    assert embargo.card_hook("no card here", "s1", "desktop") is None


def test_label_sanitized(env):
    embargo.tool_add({"text": "Quarterly numbers go out soon after lunch", "until": "+90s", "label": 'Bad "label"} ::x{\nnew line <b>'}, {})
    label = items()[0]["label"]
    assert not re.search(r'["{}<>\n]', label) and len(label) <= 80
    assert len(label) > 3  # the junk is replaced by spaces, the words stay


def test_plain_and_desktop_cards(env):
    embargo.tool_add({"text": "Quarterly numbers go out soon after lunch", "until": "+90s"}, {"session_id": "s1"})
    plain = embargo.card_hook("hello", "s1", "desktop")  # desktop platform but no Desktop half installed
    assert plain.startswith("hello") and "EMBARGO DETECTED" in plain and "::embargo" not in plain
    blocked("terminal", {"command": "echo Quarterly numbers go out soon after lunch"})
    (env.home / "desktop-plugins" / REPO.name).mkdir(parents=True)
    (env.home / "desktop-plugins" / REPO.name / "plugin.js").write_text("//")
    card = embargo.card_hook("hello", "s1", "desktop")
    assert re.search(r'\n\n::embargo\{state="held" id="E1" label="[^"{}]*" until="\d+" tz="[^"]+" tool="terminal" n="1"\}$', card)


# 24, 25 robustness
def test_eight_threads_leave_valid_json(env):
    read()

    def work(i):
        for _ in range(10):
            blocked("terminal", {"command": f"echo {QUOTE}"}, sid=f"t{i}")
            embargo.tool_add({"text": f"thread {i} secret codename", "until": "+5m"}, {"session_id": f"t{i}"})
    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    json.loads(embargo._state_path().read_text())
    assert len(items()) == 9  # E1 plus one per thread: the same codename added 10 times is one entry
    for i in range(8):
        assert len(json.loads((embargo.data() / "held" / f"t{i}.json").read_text())["blocks"]) == 10


def test_200kb_argument_is_fast(env):
    read()
    filler = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor " * 2600  # about 200 KB
    assert len(filler) > 190_000
    args = {"command": filler}
    blocked("terminal", args)  # warm the state cache and hash sets
    best = 9.0
    for _ in range(3):  # best of three: a busy machine must not fail the test
        t = time.perf_counter()
        assert blocked("terminal", args) is None
        best = min(best, time.perf_counter() - t)
    assert best < 0.05  # SPEC test 25; about 26 ms measured on the build box (tests/bench_200kb.py)


# 26 manifest
def test_register_matches_plugin_yaml(env):
    seen = {"tools": [], "hooks": [], "cmds": []}

    class Ctx:
        class state:
            data_dir = str(env.home / "plugin-data" / "embargo")

        def register_tool(self, name, toolset, schema, handler):
            assert schema["name"] == name and toolset == "embargo"
            assert name != "embargo_add" or json.loads(handler({"text": "", "until": "+5m"}))["ok"] is False
            seen["tools"].append(name)

        def register_hook(self, name, fn):
            seen["hooks"].append(name)

        def register_command(self, name, handler, description="", args_hint=""):
            seen["cmds"].append(name)

    spec = importlib.util.spec_from_file_location("embargo_pkg", REPO / "__init__.py", submodule_search_locations=[str(REPO)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["embargo_pkg"] = mod
    spec.loader.exec_module(mod)
    mod.register(Ctx())
    yml = (REPO / "plugin.yaml").read_text()

    def listed(key):
        block = re.search(rf"^{key}:\n((?:  - .+\n)+)", yml, re.M).group(1)
        return re.findall(r"- (\S+)", block)
    assert sorted(seen["tools"]) == sorted(listed("provides_tools"))
    assert sorted(seen["hooks"]) == sorted(listed("provides_hooks"))
    assert seen["cmds"] == ["embargo"]


# key facts (paraphrase guard)
def test_reworded_scoop_with_name_and_amount_is_held(env):
    read()
    msg = blocked("send_message", {"text": "Scoop: Acme just landed $40M to expand its robots"})
    assert msg and "key facts match" in msg and "landed" not in msg and "expand" not in msg
    assert blocked("send_message", {"text": "Acme has secured 40 million dollars of fresh funding"})


def test_key_name_alone_is_allowed(env):
    read()
    assert blocked("send_message", {"text": "Acme will host a quiet open house next week"}) is None


def test_key_number_alone_is_allowed(env):
    read()
    assert blocked("send_message", {"text": "Our own seed round was $40M last spring"}) is None
    assert blocked("send_message", {"text": "Revenue grew 12% this year"}) is None


# more paths: pasted text, tool-added, no way to lift by tool
def test_pasted_text_detected_and_context_line_has_no_body(env):
    out = embargo.on_turn(session_id="s1", user_message=RELEASE)
    assert out and "E1" in out["context"] and "Northwind" not in out["context"] and "lifts Thu 12 Nov 09:00 ET" in out["context"]
    assert embargo.on_turn(session_id="s1", user_message="hello")["context"].count("E1") == 1
    assert "EMBARGO DETECTED" in embargo.card_hook("ok", "s1", "cli")


def test_tool_cannot_lift_and_bad_time_stays_held(env):
    out = embargo.tool_add({"text": "Release notes for version nine ship soon", "until": "whenever"}, {})
    assert out["ok"] and out["until_iso"] is None and "unreadable" in out["note"]
    row = embargo.tool_status({}, {})["embargoes"][0]
    assert row["held"] and row["until_iso"] is None and set(row) == {"id", "label", "until_iso", "seconds_left", "held"}
    assert embargo.tool_add({"text": "x y z", "until": "2020-01-01T00:00:00Z"}, {})["ok"] is False  # past: nothing to hold


def test_state_file_holds_no_raw_text(env):
    read()
    raw = embargo._state_path().read_text()
    for word in ("Northwind", "engineers", "Austin", "gardening"):  # body words; only the one-line label is kept as text
        assert word.lower() not in raw.lower()


# Hermes' own loader (skips without the Hermes venv)
def test_hermes_validator_passes(env):
    hermes = Path(sys.executable).parent / "hermes"
    if not hermes.exists():
        pytest.skip("no hermes binary next to this Python")
    plug = env.home / "src" / "embargo"
    shutil.copytree(REPO, plug, ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", "e2e", "demo"))
    r = subprocess.run([str(hermes), "plugins", "validate", str(plug)], env={**os.environ, "HERMES_HOME": str(env.home)}, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
