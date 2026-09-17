#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A SessionStart hook that makes an always-on agent tell you how it woke up.

It answers two questions the agent cannot answer for itself:

  source=startup|resume|clear   "What state am I starting in?"   -> §4 of the README
  source=compact                "What was I doing before?"       -> §2 of the README

Register it in `~/.claude/settings.json`:

    {
      "hooks": {
        "SessionStart": [
          {"hooks": [{"type": "command",
                      "command": "/path/to/session_start_hook.py"}]}
        ]
      }
    }

stdin (given by Claude Code):

    {"session_id": "...", "transcript_path": "...", "cwd": "...",
     "hook_event_name": "SessionStart",
     "source": "compact|startup|resume|clear", "model": "..."}

stdout (injected into the model's context):

    {"hookSpecificOutput": {"hookEventName": "SessionStart",
                            "additionalContext": "..."}}

Editing THIS FILE does not require a restart. What `settings.json` snapshots is
the command string; the script is exec'd fresh on every trigger. Only changing
the *registration* — event name, matcher, or this file's path — needs a
restart. That asymmetry is the same one described in §4 of the README, and it
is why you can iterate on a hook inside a month-old session.

Configuration lives in `vitals.json` next to this file (see
`vitals.example.json`). It is gitignored on purpose: the names of your agents,
the paths to their homes, and the text you inject are yours, and this repo is
not where they go.

No dependencies. Standard library only.
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("SESSION_VITALS_CONFIG") or os.path.join(HERE, "vitals.json")
LOG_PATH = os.environ.get("SESSION_VITALS_LOG") or "/tmp/session_vitals_hook.log"

MAX_CHARS = 2600   # over this, drop the optional half rather than re-inflate context
TAIL_EVENTS = 60   # how many recent user messages to look back through
MAX_LINES = 4000   # how much of the tail of the transcript to parse at all


def log(msg):
    """Append one line. This is the only thing standing between a silent hook
    failure and an incident you can explain — see the bottom of this file."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def sh(args, timeout=3):
    """Run a command. Any failure is an empty string. A self-check must never
    be the reason a session fails to start."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or "").strip()
    except Exception:
        return ""


def load_json(path):
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ------------------------------------------------------------------ identity

def which_agent(config):
    """Which agent am I? Ask tmux. Never infer it from the working directory.

    An always-on agent and a human operator can both have this hook installed,
    and the operator's shell wanders into the agent's directories all the time
    — to read its files, to fix its scripts. The working directory tells you
    *whose files you are looking at*. It does not tell you *who you are*. Wire
    those two together and the operator eventually speaks in the agent's voice
    without either of them noticing.

    `SESSION_VITALS_AGENT` overrides, so the tests can run outside tmux.
    """
    agents = config.get("agents") or {}
    forced = os.environ.get("SESSION_VITALS_AGENT", "").strip()
    if forced:
        return forced if forced in agents else None
    if not os.environ.get("TMUX"):
        return None
    name = sh(["tmux", "display-message", "-p", "#{session_name}"])
    return name if name in agents else None


# ------------------------------------------------------- what was I doing

def read_tail_user_texts(path, limit=TAIL_EVENTS):
    """Read the transcript backwards and collect recent user message text.

    Two things this deliberately does not do:

    - It does not slice the last N *lines* as text. A JSONL transcript being
      written to can end mid-line, and a blind slice can also cut a tool_use
      away from its tool_result. Every line is parsed on its own and a line
      that won't parse is skipped.

    - It does not treat everything typed as a message. Slash-command echoes and
      system injections arrive wrapped in tags — `<command-name>`,
      `<local-command-stdout>`, `<system-reminder>`. They look exactly like an
      unprefixed human message, which is the one thing `detect_mode` reads as
      "a person is talking to me right now".
    """
    texts = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception:
        return texts

    for line in reversed(lines[-MAX_LINES:]):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "user" or d.get("isMeta"):
            continue
        content = (d.get("message") or {}).get("content")
        chunks = []
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    chunks.append(part.get("text") or "")
        for c in chunks:
            if c.lstrip().startswith("<"):
                continue
            texts.append(c)
        if len(texts) >= limit:
            break
    return texts


def detect_mode(texts, compact):
    """Which channel woke this session, judged by the prefix on recent messages.

    Rules are `{"mode": ..., "prefix": ...}` or `{"mode": ..., "contains": ...}`
    from the config, tried in order — put the mode you would most regret
    guessing wrong about first.

    An unprefixed message means a person is typing directly, which is
    `direct_mode`. But a message with *some other* bracketed prefix means a
    channel this config does not know about, and that is not the same thing —
    so those are skipped rather than read as a human.

    Anything unclear returns "unknown". Guessing here is worse than admitting
    it, because the whole point of the injected text is to stop the agent
    acting on an impression.
    """
    rules = compact.get("detect") or []
    joined = "\n".join(texts[:20])
    for rule in rules:
        if rule.get("contains") and rule["contains"] in joined:
            return rule["mode"]

    opener = tuple(compact.get("prefix_chars") or ["[", "【"])
    for t in texts:
        head = t.lstrip()[:40]
        for rule in rules:
            if rule.get("prefix") and head.startswith(rule["prefix"]):
                return rule["mode"]
        if head.startswith(opener):
            continue
        return compact.get("direct_mode") or "unknown"
    return "unknown"


def build_compact_context(agent, mode, compact):
    """The text injected after a context compaction.

    Everything in here points at where the truth is. None of it *is* the truth.
    A compaction summary is a note to yourself: it can tell you which direction
    to look, and it cannot tell you what is currently the case.
    """
    fields = dict(agent)
    fields["time"] = time.strftime("%H:%M")
    fields["mode"] = mode

    blocks = compact.get("blocks") or {}
    block = blocks.get(mode) or blocks.get("unknown") or ""

    head = (compact.get("header") or "").format(**fields)
    body = block.format(**fields)
    tail = (compact.get("footer") or "").format(**fields)

    text = "\n".join(x for x in (head, body, tail) if x.strip())
    if len(text) > MAX_CHARS:
        # Rather than inject an enormous note, drop the optional half. A note
        # too long to read is the same as no note, but it costs context.
        text = "\n".join(x for x in (head, body) if x.strip())
    return text


# ------------------------------------------------------- what state am I in

def _fmt_mtime(path):
    return time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(path)))


def run_startup_checks(agent, checks):
    """Every check answers a question about the machine, not about intent.

    A check earns its place by being something the agent would otherwise
    *assume*. "The relay is running" and "the config says effort is high" are
    assumptions until something looks.
    """
    rows = []

    def row(label, ok, detail):
        rows.append("%s %s — %s" % ("OK  " if ok else "WARN", label, detail))

    home = agent.get("home") or "~"
    user_settings = load_json(agent.get("settings") or "~/.claude/settings.json")
    project_settings = load_json(os.path.join(os.path.expanduser(home), ".claude", "settings.json"))
    env = user_settings.get("env") or {}

    def settings_get(key):
        for src in (project_settings, user_settings):
            cur, ok = src, True
            for part in key.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    ok = False
                    break
            if ok:
                return cur
        return None

    for c in checks.get("settings") or []:
        got = settings_get(c["key"])
        if got is None:
            row(c["label"], False, c.get("if_unset") or "not set")
        elif "expect" in c:
            row(c["label"], got == c["expect"], str(got))
        else:
            rows.append("     %s — %s" % (c["label"], got))

    for c in checks.get("env") or []:
        got = env.get(c["key"])
        row(c["label"], bool(got),
            (c.get("if_set") or "{value}").format(value=got) if got
            else (c.get("if_unset") or "not set"))

    hooks = user_settings.get("hooks") or {}
    for c in checks.get("hooks") or []:
        got = hooks.get(c["event"])
        row(c["label"], bool(got), "registered" if got else (c.get("if_unset") or "not registered"))

    for c in checks.get("files") or []:
        p = os.path.expanduser(c["path"].format(home=home))
        ok = os.path.isfile(p)
        row(c["label"], ok, ("last edited %s" % _fmt_mtime(p)) if ok else "missing (%s)" % p)

    for c in checks.get("processes") or []:
        found = sh(["pgrep", "-f", c["pattern"]])
        row(c["label"], bool(found),
            "%d running" % len(found.split()) if found else (c.get("if_unset") or "not running"))

    for c in checks.get("http") or []:
        body = sh(["curl", "-s", "-m", str(c.get("timeout", 2)), c["url"]])
        row(c["label"], bool(body), "responding" if body else "no answer from %s" % c["url"])

    # A scheduled job you never look at is a job you have stopped running. The
    # log file's mtime is the cheapest possible proof that it ran — and unlike
    # the job's own success message, it cannot be printed by a job that did
    # nothing. See §5.
    for c in checks.get("freshness") or []:
        p = os.path.expanduser(c["path"])
        if not os.path.isfile(p):
            row(c["label"], False, "never ran — %s does not exist" % p)
            continue
        age_h = (time.time() - os.path.getmtime(p)) / 3600.0
        row(c["label"], age_h < float(c.get("max_age_hours", 30)),
            "last ran %s (%.1fh ago)" % (_fmt_mtime(p), age_h))

    return rows


def build_startup_context(agent, config, source):
    rows = run_startup_checks(agent, config.get("startup_checks") or {})
    template = config.get("startup_template") or DEFAULT_STARTUP_TEMPLATE
    fields = dict(agent)
    fields.update(time=time.strftime("%H:%M"), source=source, rows="\n".join(rows))
    return template.format(**fields)


DEFAULT_STARTUP_TEMPLATE = """[wake-up check | {time}] (source={source})

You are {name}. This list was produced by a hook — **the fact that you can see
it at all is the proof that the hook itself is alive.**

{rows}

Only what is on disk can be checked this way. "Loaded" is a different claim and
you have to verify it yourself: **make one real tool call now**, before you do
anything else, rather than describing one in prose. A tool you have never
called is a tool you do not know you have.

If any line above says WARN, say so out loud instead of working around it.
"""


# ----------------------------------------------------------------- main

def main():
    raw = sys.stdin.read() if not sys.stdin.isatty() else "{}"
    try:
        data = json.loads(raw or "{}")
    except Exception:
        data = {}

    source = data.get("source") or ""
    transcript = data.get("transcript_path") or ""

    config = load_json(CONFIG_PATH)
    if not config:
        log("skip: no config at %s" % CONFIG_PATH)
        sys.exit(0)

    key = which_agent(config)
    if not key:
        log("skip: not a configured agent  source=%s cwd=%s" % (source, data.get("cwd")))
        sys.exit(0)
    agent = dict(config["agents"][key])
    agent.setdefault("name", key)
    agent.setdefault("key", key)

    if source == "compact":
        compact = config.get("compact") or {}
        mode = detect_mode(read_tail_user_texts(transcript), compact) if transcript else "unknown"
        text = build_compact_context(agent, mode, compact)
        log("agent=%-4s source=compact  mode=%-9s injected %d chars" % (key, mode, len(text)))
    elif source in ("startup", "resume", "clear"):
        text = build_startup_context(agent, config, source)
        log("agent=%-4s source=%-8s checked, injected %d chars" % (key, source, len(text)))
    else:
        log("skip: unknown source=%r" % source)
        sys.exit(0)

    json.dump({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": text,
    }}, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Catch everything and exit 0: a broken hook must never be the reason a
        # session won't start.
        #
        # But never silently. A catch-all that protects the session will also
        # hide the failure, and a hook that has been dead for three weeks looks
        # exactly like a hook with nothing to report. The log line below is the
        # whole difference. Do not remove it to make the output tidy.
        log("ERROR %r" % (e,))
        sys.exit(0)
