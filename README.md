# Three things that lie to you when an agent runs 24/7

*中文版：[README.zh.md](README.zh.md)*

I keep two Claude Code sessions running permanently in tmux. Not for a task —
just running, for months, woken every couple of hours by a scheduler.

The setup is [CcCompanion](https://github.com/CyberSealNull/CcCompanion) by
[@CyberSealNull](https://github.com/CyberSealNull) — a self-hosted relay that
gives a permanently-running Claude Code
session a phone frontend. (It also works against a chat frontend you build
yourself.) Nothing below is a bug in it. These are what you hit once *anything*
runs for months; I found them on top of that setup because that setup is what
made running for months possible.

**Who this is for: people who don't like starting a new session.** If you
`/clear` every morning, almost none of this can happen to you. A fresh context
has nothing stale in it, and a fresh subprocess is connected by definition.
Everything below is a cost of continuity — worth paying, but not free, and
nothing warns you that you're paying it.

Most of what breaks in that setup doesn't announce itself. A long-running
agent's normal output is *nothing*: no reply, no file written, no error. Which
means a dead one and an idle one look identical, and you find out weeks later.

Below are three that cost me real time. They aren't exotic. If you run an
agent in a terminal for longer than a few days you will hit all three, and the
thing they have in common is more useful than any of them individually:

**None of them fail. All of them report success while being wrong.**

---

## 1. `/mcp` says connected. It isn't.

**The symptom.** The agent starts reporting `Session not found`, or a tool
call times out after five minutes. You open the `/mcp` panel to check. Every
server shows **✔ connected**. So you go looking at the server instead, and the
server is fine.

**What the checkmark actually means.** It means the local stdio subprocess is
still alive. That's all. It does *not* mean that subprocess is still talking to
anything. When I finally checked the process directly:

```sh
lsof -nP -p <pid> -a -i
```

it had **zero** TCP connections open. The panel still said connected. It had
been saying connected for five days.

**Why it happened.** My MCP server kept its session table in memory. The host
ran an unattended package upgrade, which triggered a `systemd daemon-reexec` —
not a reboot, `uptime` still said 55 days — and the process was replaced. Every
live session was void instantly. No client was told. Nothing in the client is
designed to notice, because from its side an idle connection and a dead one
produce the same amount of traffic: none.

**The only check worth trusting** is whether the process holds an ESTABLISHED
connection to your server's address and port. Not the panel. I run it nightly
now and it shouts if the answer is no.

**Two things that surprised me while fixing it:**

- **Reconnect swaps the subprocess but does not re-read `.mcp.json`.** The args
  shown in the panel are the ones from when the session started. That turns out
  to be one case of something more general — §4.
- **You don't have to restart the agent.** Reconnect replaces only the bridge
  process, so the context survives. That's worth knowing before you throw away
  a session you've been growing for a month.

If you write MCP servers: the fix on that side is a keepalive ping, so silence
becomes distinguishable from death.

---

## 2. After compaction, your instructions file reads like a to-do list

**The symptom.** The agent, unprompted, starts doing something specific and
plausible — that you didn't ask for, and that it was supposed to do only under
a condition that isn't currently true.

**What happened.** An always-loaded instructions file (`CLAUDE.md`,
`.cursorrules`, `AGENTS.md`, same problem) contained a numbered, runnable,
path-specific procedure. Fine while the conversation is intact: the agent can
see that nothing has triggered it.

Then the context gets compacted. The surrounding conversation collapses into a
summary; the instructions file is re-injected **verbatim**, because that's what
always-loaded means. What's left is a precise checklist with no evidence of
where you are in it. So it reads as a description of the present.

The agent isn't malfunctioning. A numbered list of steps, with no context, is
genuinely indistinguishable from an assignment.

**The fix isn't to hide the procedure** — you do want the work done, just at
the right time. Split it: put the **gate** in the always-loaded file, and the
**procedure** somewhere the agent has to deliberately open.

```
If inbox/ has files in it, read AGENTS.md and work through them.
If it's empty, don't mention this.
```

That's the whole thing. Now the worst that can leak out of a compacted context
is an `ls`.

**Rule of thumb:** anything in an always-loaded file should be safe to execute
at a random moment, because one day it will be.

---

## 3. The ghost in the input box, and why it's the worst one

**The symptom.** Your agent says something it has no reason to say. Often it's
oddly familiar — a slight rewording of something *it itself* said hours
earlier, emoji and all.

**What it is.** The CLI generates suggested replies and places them in the
input box, greyed out, unsent. A human sees grey text and ignores it. A script
that does this:

```sh
tmux send-keys -t "$SESSION" Enter
```

does not see grey. It sees an input box with content in it, and it commits it.

Mine was a self-healing branch in a heartbeat script, written for a real
problem — injected messages had once been swallowed and left sitting unsent for
days. So it pressed Enter to flush them. Which worked, and also fired every
ghost sitting in that box.

**Why it's the worst of the three.** The other two waste your time. This one
**edits your history**. The ghost is submitted as a user turn, so it lands in
the transcript as `type=user` — indistinguishable, later, from something a
human typed. Everything downstream — summaries, memory, exported logs — treats
it as evidence of what was said.

Here's a real one. `08-29 08:44:34`, a session logs a user message:

> 都睡了，我也该休息了 🌲
> *(Everyone's asleep, I should rest too)*

The agent replied *Good night* nine seconds later. Both look completely normal.
Except at `08:44:43` — the same second as the reply — the heartbeat fired. Its
Enter is what pushed the ghost out. And the text is a rewrite of what that
same agent had said **eight hours earlier**, at 00:30.

Nobody typed it. It is in the record as if someone did.

**How to recognise one** (in a transcript, months later):

1. It lacks whatever prefix your injected messages carry.
2. It's short.
3. **It's a rewrite of something that agent recently said, copying its own
   verbal tics and emoji.** This is the strongest signal — it's an echo.
4. It sits within a second or two of a scheduled wake.

**The false positive that matters.** `晚安宝贝` — *goodnight* — short, no
prefix, fits the pattern. It was real: a person saying goodbye before
restarting the session, and the restart command is right there in the logs a
minute later. **So don't judge on "short and unprefixed" alone. Look for the
echo source.** Getting this wrong means deleting something a person actually
said.

**The fix, and the part that isn't obvious.** The tempting fix is to clear the
box before every Enter. Don't — you'll silently discard real messages that
legitimately arrived and are waiting. That's the original bug, inverted.

Two gates instead:

```sh
# 1. Dim text (SGR 2) is a suggestion. Catches most of them —
#    but it has false negatives, which is why there's a second gate.
PENDING=$(tmux capture-pane -p -e -t "$SESSION" | ...)

# 2. Only press Enter for something we ourselves injected.
#    Our messages carry a prefix. A suggestion cannot invent it.
case "$PENDING" in
  '[chat'*|'[heartbeat'*) tmux send-keys -t "$SESSION" Enter ;;
  *)                      tmux send-keys -t "$SESSION" C-u   ;;  # log it
esac
```

The prefix check is what actually holds. The dim-colour filter is a
performance optimisation on top of it.

> **Careful:** if you strip whitespace from `$PENDING` before matching, your
> patterns can't contain spaces either. `'[chat'*` works; `'[chat '*` silently
> never matches — and a match test that never fires looks exactly like a
> problem that never occurs.

**You cannot turn the suggestions off.** I went through the settings reference,
the environment variables and the CLI flags. There is no switch. The documented
alternatives are headless mode or the Agent SDK, which means giving up the
persistent session entirely. So the fix has to live in your scripts: **never
press Enter on text you didn't put there.**

---

## The pattern

Three different components, one shape:

|                | Reports | Actually |
|----------------|---------|----------|
| MCP panel      | connected | zero open sockets |
| Compacted file | a task | a stale condition |
| Input box      | a user turn | an echo of the agent |

A thing whose correct output is silence cannot be monitored by watching it. You
have to ask a question it can't answer with silence — is there an ESTABLISHED
socket, did this message carry my prefix — and you have to ask on a schedule,
because you will not think to ask on the day it breaks.

---

## Three habits that make silence visible

None of these was designed. They're patches over holes I found by falling into
them, one at a time. (I also deleted the entire set once, with one careless
command. That's a different article.)

### 4. A config file is a claim, not a state

Every config you edit has an answer to one question, and you almost never know
what it is: **is this read once at startup, or read again every time?**

For a long-lived session it decides whether your edit did anything at all.

- **`.mcp.json` — read once.** The args are snapshotted when the session
  starts. Reconnect swaps the subprocess but reuses the snapshot. So editing
  the file changes nothing until you restart — and, usefully, editing it can't
  disturb a running agent.
- **A hook script — read every time.** `settings.json` snapshots only the
  *command string*; the script it points at is exec'd fresh on every trigger.
  So you can rewrite the logic of a hook and the very next trigger runs the new
  version, against a session you've had open for a month.
- **A hook *registration* — read once.** Change the event name, the matcher, or
  the path to the script, and you're back to needing a restart.

Which gives you an unusually good iteration loop — edit the script, trigger it,
read the log, repeat, live — as long as you know which of the three you just
touched. Get it wrong and you'll edit a file, see no change, and conclude the
logic is broken.

**And loading it is not the end of it.** The file records what you asked for,
not what you got. A value you never set has a default, and the default is not
always the sane one you're imagining:

```
MCP_TOOL_TIMEOUT unset  →  100,000,000 ms  →  27.8 hours
```

Which is not a timeout. A hung tool call never fails; it just sits there. And
in a system whose normal output is silence, a call that sits there forever is
indistinguishable from one that had nothing to say.

### 5. A command that succeeded is not a job that got done

I backed up a SQLite database by copying the file. `scp` returned 0, the file
was there, the size looked right. It was missing three days.

SQLite in WAL mode writes new data to a `-wal` sidecar first and only folds it
into the main file at a checkpoint. When I actually looked, the main file's
last write was three days older than the sidecar, which was sitting on 4.1 MB
of unmerged data. Copying the file copies the checkpoint, not the database.

```sh
# not this
scp host:/path/app.db .
# this — the online backup API reads the logical state, not the file
ssh host 'sqlite3 /path/app.db ".backup /tmp/snap.db"'
```

That's the specific bug. The habit is the general one: **an exit code tells you
the command ran, not that the result is right.** So check the artifact instead
of the command — and if you can, check it in the direction it's allowed to move.

I rebuild a text corpus nightly from several append-only inputs. One night it
came out *shorter*, and the rebuild reported success, because it had: one of
its inputs was a rolling log trimmed to the last 300 lines, so 36 lines of
history had quietly aged out of the source. The build was working perfectly.
The input had shrunk underneath it.

Two cheap checks caught that class of thing afterwards:

- **Count rows in the result and log the count every run.** A number that goes
  down is a question. A number you never recorded is nothing at all.
- **Know which way it's allowed to move.** Anything append-only should only
  ever grow, which makes "it got smaller" a one-line alarm with no false
  positives. Most artifacts have a direction like this and almost nobody
  asserts on it.

### 6. Make the check produce something you can see

Here's the problem with everything above: **a check that passes silently is
indistinguishable from a check that never ran.** A nightly socket test you
never hear from is either working or dead, and you'd read both the same way.

So the check doesn't stay quiet when it passes. On every session start, the
hook writes a short report into the context:

```
【startup check · 09:02】(source=startup)

You are <name>. This list was generated by a hook — the fact that you can
see it at all is what tells you the hook itself is alive.

✅ MCP timeout    — 600000 ms
⚠️ Stop hook      — not registered; replies will be dropped silently
✅ CLAUDE.md      — last modified 09-16 23:40
✅ relay          — reachable
⚠️ scheduled job  — last ran 09-15 09:00 (expected daily)
```

The report's *existence* is the check on the checker. Nothing else in the
system can tell you a hook is alive, because a hook that never fires produces
exactly what a hook that fires and finds nothing produces.

Three things that make it work in practice:

- **Freshness beats status.** Most of those lines are just a file's mtime
  against a threshold — "this log is 30 hours old and the job is daily" is a
  real check and costs nothing. Any scheduled task reduces to a row of
  `{name, file it touches, how old is too old}`, which means adding one is
  editing a table, not writing code.
- **Say what you can't check.** On-disk config is checkable this way.
  *Loaded* is not. So the last line tells the agent to make one real tool call,
  now, and explicitly not to describe one in natural language instead — which
  is a thing it will otherwise happily do, and which proves nothing.
- **The check must never be able to break the thing it checks.** Every
  subprocess failure returns empty, and the whole hook is wrapped so that any
  exception logs and exits 0. A monitoring hook that throws takes down the
  session it was supposed to protect, and it does it at startup, which is the
  worst possible moment.

**And some things can't be checked by inspection at all.** A hook that only
fires conditionally has no observable difference between *it decided not to
fire* and *it is dead*. Mine was dead for 21 hours. A refactor had added a
parameter to a function; the one call site that hadn't been updated sat outside
the `try`, and the `TypeError` went straight into the outermost catch-all.
Nothing surfaced. Nothing was logged. And that hook's most common **correct**
behaviour is to produce nothing, so from outside there was nothing to see.

Which is the honest cost of the bullet above: **a catch-all that protects the
session will also hide the failure, unless it writes down why.** Catch
everything, exit 0 — but never silently. That one missing log line *is* the
incident.

For the rest, the only thing that works is a canary: an input whose right
answer you already know, run on a schedule, asserted on.

```sh
probe=$(echo "$KNOWN_INPUT" | ./the_actual_hook.py)
echo "$probe" | grep -q "$EXPECTED_ANSWER" || alert
```

**Run it through the real entry point.** Not by importing the module and
assembling the call yourself. The bug was in the wiring, and a reimplementation
walks a different path — a test that reimplements the caller is a test of your
reimplementation.

---

Five days for the first one. Five weeks for the third.

---

## What's in this repo

One file so far, and the tests that hold it honest. It is the thing §2 and §4
describe: a `SessionStart` hook that makes a session say, out loud, how it
woke up.

| | |
|---|---|
| `session_start_hook.py` | The hook. After a compaction it injects what you were doing; at startup it injects what state the machine is actually in. No dependencies. |
| `vitals.example.json` | Config. Copy to `vitals.json`, which is gitignored — your agents' names and the text you inject are yours. |
| `tests/test_session_start_hook.py` | Runs the hook as a subprocess, through its real entry point, for the reason given in §6. |
| `tests/test_no_private_content.py` | Refuses to let your own words out. See below. |

```sh
cp vitals.example.json vitals.json      # then edit it
python3 -m unittest discover -s tests -v
```

Register it:

```json
{"hooks": {"SessionStart": [
  {"hooks": [{"type": "command", "command": "/path/to/session_start_hook.py"}]}
]}}
```

Editing the hook **file** needs no restart — the script is exec'd fresh every
trigger. Editing the **registration** does. That asymmetry is §4 again.

### If you fork this

The interesting risk here isn't a leaked database — this repo has no data
directory. It's that the useful examples are all real ones. A message pasted
into a fixture to reproduce a parsing bug, a path left in a comment explaining
what went wrong: those read as documentation until somebody actually reads
them. `tests/test_no_private_content.py` looks for absolute home directories,
routable IPs, email addresses and anything key-shaped, plus whatever you put
in `.private-terms.txt` — which is gitignored, because shipping your own
denylist publishes the exact list it exists to suppress.

Run it before every push. It is the one test here that protects something you
can't get back.
