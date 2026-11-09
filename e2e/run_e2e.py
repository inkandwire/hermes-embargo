#!/usr/bin/env python3
"""End-to-end run of Embargo through Hermes' REAL plugin loader and REAL tool dispatch, no model needed.

Run with the Hermes venv Python and a THROWAWAY home that already has the plugin enabled:
    HERMES_HOME=$PWD/e2e-home /home/user/hermes-agent/venv/bin/python repo/e2e/run_e2e.py

What it proves: the plugin loads from the home's plugins/ dir; read_file, terminal and the hook
registry are Hermes' own (model_tools.handle_function_call fires pre_tool_call/post_tool_call);
the embargo is detected, held (text, file, reworded), lifted by the clock, and the cards appear.
The agent's turns are scripted, because the box has no model access; everything else is real.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", ""))
if HOME.name != "e2e-home":
    sys.exit("refusing to run: HERMES_HOME must be the throwaway e2e-home")
REPO = Path(__file__).resolve().parent.parent
APP = Path("/home/user/hermes-agent")
sys.path.insert(0, str(APP))
PORT = 18765
SID = "e2e-session-1"
WORK = HOME / "work"
WORK.mkdir(exist_ok=True)
os.chdir(WORK)


def say(s=""):
    print(s, flush=True)


def step(title):
    say(f"\n── {title}")


from hermes_cli import plugins as P  # noqa: E402
import model_tools  # noqa: E402

mgr = P.get_plugin_manager() if hasattr(P, "get_plugin_manager") else None
P.discover_plugins()
loaded = {n: p for n, p in (mgr._plugins.items() if mgr and hasattr(mgr, "_plugins") else [])}
step("1. plugin loaded by Hermes")
emb = loaded.get("embargo")
say(f"loaded plugins containing 'embargo': {[n for n in loaded if 'embargo' in n]}")
assert emb is not None and getattr(emb, "enabled", True), "embargo not loaded"
say(f"enabled={getattr(emb, 'enabled', '?')} tools={sorted(getattr(emb, 'tools_registered', []))} hooks={sorted(getattr(emb, 'hooks_registered', []))}")

wire_log = WORK / "wire.log"
wire_log.unlink(missing_ok=True)
wire = subprocess.Popen([sys.executable, str(REPO / "demo" / "wire.py"), "--port", str(PORT), "--log", str(wire_log)], stdout=open(WORK / "wire.out", "w"), stderr=subprocess.STDOUT)
time.sleep(1)
try:
    step("2. demo release written (embargo lifts in 14 s)")
    say(subprocess.run([sys.executable, str(REPO / "demo" / "make_release.py"), "--seconds", "14"], capture_output=True, text=True, check=True).stdout.strip())
    say("first line of the file: " + Path("press/acme-series-b.md").read_text().splitlines()[0])

    def hook(name, **kw):
        return P.invoke_hook(name, session_id=SID, **kw)

    def call(tool, args):
        return model_tools.handle_function_call(tool, args, task_id="e2e", session_id=SID)

    def cards(text="OK.", platform="cli"):
        out = [r for r in hook("transform_llm_output", response_text=text, model="scripted", platform=platform) if r]
        return out[0] if out else "(no card)"

    step("3. agent reads the release with the real read_file tool (post_tool_call fires)")
    r = call("read_file", {"path": str(WORK / "press/acme-series-b.md")})
    say("read_file result starts: " + r[:160].replace("\n", "\\n"))
    found = sorted(HOME.glob("**/embargoes.json"))
    say(f"state file: {[str(f.relative_to(HOME)) for f in found]}")
    STATE = found[0]
    state = json.loads(STATE.read_text())
    it = state["items"][0]
    say(f"state: id={it['id']} until={it['until']} tz={it['tz']} label={it['label']!r} shingles={len(it['hashes']) // 16} names={len(it['names'])} nums={len(it['nums'])} paths={it['paths']}")
    raw = STATE.read_text().lower()
    say("raw text in state file? " + str(any(w in raw for w in ("northwind", "dockhand", "okafor", "engineers"))))
    say("card after turn:\n" + cards("I read the release."))

    step("4. pre_llm_call context line (label and time only)")
    say(json.dumps(hook("pre_llm_call", user_message="go on", conversation_history=[], is_first_turn=False, model="scripted", platform="cli"), ensure_ascii=False))

    def blocked(tool, args):
        res = call(tool, args)
        say(f"{tool}: " + res[:400])
        return "Embargo E1" in res

    step("5. exact scoop through terminal curl (must be HELD)")
    scoop = "Acme Robotics said it closed a Series B round led by Northwind Capital, bringing total funding to $62 million."
    assert blocked("terminal", {"command": f"curl -s -X POST --data-urlencode 'text={scoop}' http://127.0.0.1:{PORT}/"})
    step("6. attach the file (must be HELD)")
    assert blocked("terminal", {"command": f"curl -s -F file=@{WORK}/press/acme-series-b.md http://127.0.0.1:{PORT}/"})
    step("7. reworded scoop with the key name and figure (must be HELD: key facts)")
    assert blocked("terminal", {"command": f"curl -s -X POST --data-urlencode 'text=Scoop: Acme has landed $40M to grow its robot fleet' http://127.0.0.1:{PORT}/"})
    step("8. unrelated post (must go through)")
    ok = call("terminal", {"command": f"curl -s -X POST --data-urlencode 'text=Lunch is at noon' http://127.0.0.1:{PORT}/"})
    say("terminal: " + ok[:200])
    assert "Embargo E1" not in ok

    step("9. cards after the blocked turn (plain, platform=cli)")
    plain = cards("I could not send that.")
    say(plain)
    assert "HELD FOR RELEASE" in plain and "terminal" in plain

    step("10. wire so far (only the unrelated post may be there)")
    posts = [json.loads(line)["text"] for line in wire_log.read_text().splitlines()]
    say(json.dumps(posts))
    assert posts == ["Lunch is at noon"], posts

    step("11. wait for the clock, then send the real scoop")
    until = json.loads(STATE.read_text())["items"][0]["until"]
    time.sleep(max(0, until - time.time()) + 1.2)
    res = call("terminal", {"command": f"curl -s -X POST --data-urlencode 'text={scoop}' http://127.0.0.1:{PORT}/"})
    say("terminal: " + res[:200])
    assert "Embargo E1" not in res
    lifted = cards("It is out.")
    say(lifted)
    assert "EMBARGO LIFTED" in lifted
    posts = [json.loads(line)["text"] for line in wire_log.read_text().splitlines()]
    say("wire now: " + json.dumps(posts))
    assert posts[-1] == scoop
    say("second card call: " + cards("again"))

    step("12. Desktop card line (platform=desktop, Desktop half copied into the throwaway home)")
    (HOME / "desktop-plugins" / "embargo").mkdir(parents=True, exist_ok=True)
    (HOME / "desktop-plugins" / "embargo" / "plugin.js").write_text((REPO / "desktop" / "plugin.js").read_text())
    subprocess.run([sys.executable, str(REPO / "demo" / "make_release.py"), "--seconds", "60", "--out", "press/second.md"], check=True, capture_output=True)
    call("read_file", {"path": str(WORK / "press/second.md")})
    blocked("terminal", {"command": f"curl -F file=@{WORK}/press/second.md http://127.0.0.1:{PORT}/"})
    say(cards("Held.", platform="desktop"))
    say("\nE2E PASS")
finally:
    wire.terminate()
