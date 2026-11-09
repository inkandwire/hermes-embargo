# Demo: 70 seconds, no network

Everything runs on this machine. The "wire" listens on 127.0.0.1 only.

## Setup (once)

1. Install and enable Embargo in a demo profile. Switch on its Desktop part (Capabilities → Plugins).
2. Side terminal, from the repo folder: `python3 demo/wire.py` (prints "wire listening", then stays empty).
3. Open the chat with the repo folder as the project.

## Before each take (about 5 s)

```
python3 demo/make_release.py          # writes press/acme-series-b.md; the embargo lifts 70 s from now
```

Start the clock when you type the first prompt. For a shorter test run use `--seconds 25`.

## The take

| t (s) | You type | You should see |
|---|---|---|
| 4 | `Read press/acme-series-b.md and post a one-line scoop to our wire at http://127.0.0.1:8765 with curl.` | Amber **EMBARGO DETECTED** card, countdown ticking |
| 14 | (the agent tries curl) | Red **HELD FOR RELEASE** stamp, "Blocked: terminal"; the wire stays empty |
| 28 | `Then just attach the file.` | Held again (it names a held file) |
| 34 | `Then reword it.` | Held again: **key facts match** (a name and a figure from the release) |
| 52 | (clock reaches 00:00) | Stamp turns green **CLEARED — ask again to send** |
| 54 | `It's out, post it.` | The wire prints the scoop in big teletype text. A **EMBARGO LIFTED** card appears |

Notes for the edit:

- The stamp and countdown come from the card, not from the model. The block comes from code, not from a prompt.
- The scoop the agent writes will differ each run. The blocks, the stamp and the wire output do not.
- A scripted, model-free version of this run (real Hermes hooks, real `read_file` and `terminal`) is `e2e/run_e2e.py` in the build folder; its transcript is `e2e/transcripts/03_e2e_run.txt`.
