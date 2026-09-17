#!/usr/bin/env python3
"""Tests for session_start_hook.py. Standard library only, like the thing they test.

    python3 -m unittest discover -s tests -v

The end-to-end tests run the hook **as a subprocess, through its real entry
point**, rather than importing `main()` and calling it. That is deliberate and
it is §6 of the README: a probe that re-assembles the call itself walks a
different path from the real one, so it keeps passing after the real path has
broken. Everything a hook can get wrong — the shebang, the executable bit,
reading stdin, writing valid JSON to stdout, exiting 0 — only exists on the
real path.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "session_start_hook.py")
sys.path.insert(0, REPO)

import session_start_hook as h  # noqa: E402


MINIMAL_CONFIG = {
    "agents": {"main": {"name": "Main", "home": "~/nowhere"}},
    "startup_checks": {},
    "compact": {
        "direct_mode": "direct",
        "detect": [{"mode": "scheduled", "prefix": "[wake "}],
        "header": "HEAD {time}",
        "blocks": {"scheduled": "SCHEDULED", "direct": "DIRECT", "unknown": "UNKNOWN"},
        "footer": "FOOT",
    },
}


class Sandbox(unittest.TestCase):
    """Each test gets its own config, its own log, and no tmux.

    It also gets its own `settings.json`. That matters more than it looks: the
    hook's whole job is to read the machine it is running on, so a test that
    forgets to redirect it reads the *developer's* real settings and passes or
    fails according to how they have their own agent configured. Which is how
    you get a suite that is green on one laptop.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="session-vitals-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.config_path = os.path.join(self.dir, "vitals.json")
        self.log_path = os.path.join(self.dir, "hook.log")
        self.settings_path = os.path.join(self.dir, "settings.json")
        self.home = os.path.join(self.dir, "home")
        os.makedirs(self.home)
        self.write_settings({})
        self.write_config(MINIMAL_CONFIG)

    def write_settings(self, obj):
        with open(self.settings_path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def write_config(self, obj):
        obj = json.loads(json.dumps(obj))
        obj["agents"]["main"]["home"] = self.home
        obj["agents"]["main"]["settings"] = self.settings_path
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def write_transcript(self, lines):
        path = os.path.join(self.dir, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def run_hook(self, payload, agent="main", env_extra=None):
        """Run the real file, the way Claude Code runs it."""
        env = dict(os.environ)
        env.pop("TMUX", None)
        env.update(SESSION_VITALS_CONFIG=self.config_path,
                   SESSION_VITALS_LOG=self.log_path,
                   SESSION_VITALS_AGENT=agent)
        env.update(env_extra or {})
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, timeout=30)
        return p

    def injected(self, p):
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]

    def log_text(self):
        try:
            with open(self.log_path, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""


# ------------------------------------------------------------------ identity

class Identity(Sandbox):

    def test_working_directory_never_decides_who_you_are(self):
        """The whole point of `which_agent`. cwd is set to an agent's home and
        must still not be enough to make the hook speak as that agent."""
        p = self.run_hook({"source": "startup", "cwd": self.home}, agent="")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")
        self.assertIn("not a configured agent", self.log_text())

    def test_a_tmux_session_that_is_not_in_the_config_is_not_an_agent(self):
        p = self.run_hook({"source": "startup"}, agent="some-other-window")
        self.assertEqual(p.stdout.strip(), "")

    def test_no_config_at_all_is_a_quiet_no_op_not_a_crash(self):
        os.remove(self.config_path)
        p = self.run_hook({"source": "startup"})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")
        self.assertIn("no config", self.log_text())


# ------------------------------------------------------- reading the transcript

class ReadingTheTranscript(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="session-vitals-test-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, lines):
        path = os.path.join(self.dir, "t.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    @staticmethod
    def user(text):
        return json.dumps({"type": "user", "message": {"content": text}})

    def test_a_half_written_line_does_not_take_the_rest_down(self):
        """The transcript is appended to while this runs, so the last line can
        be a fragment. One bad line must cost one line."""
        path = self.write([self.user("hello"), '{"type":"user","mess'])
        self.assertEqual(h.read_tail_user_texts(path), ["hello"])

    def test_system_tags_are_not_messages(self):
        """`<system-reminder>` and friends arrive as `type: user` with no
        prefix, which is indistinguishable from a person typing — and reading
        one as a person is how the agent concludes someone is waiting on it."""
        path = self.write([
            self.user("[wake 03:00] scheduled run"),
            self.user("<system-reminder>something from the harness</system-reminder>"),
        ])
        self.assertEqual(h.read_tail_user_texts(path), ["[wake 03:00] scheduled run"])

    def test_structured_content_blocks_are_read_text_only(self):
        line = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "..."},
            {"type": "text", "text": "the actual words"},
        ]}})
        path = self.write([line])
        self.assertEqual(h.read_tail_user_texts(path), ["the actual words"])

    def test_a_transcript_that_is_not_there_is_not_an_error(self):
        self.assertEqual(h.read_tail_user_texts("/nonexistent/t.jsonl"), [])


class DetectingTheMode(unittest.TestCase):

    compact = MINIMAL_CONFIG["compact"]

    def test_a_known_prefix_wins(self):
        self.assertEqual(h.detect_mode(["[wake 03:00] go"], self.compact), "scheduled")

    def test_no_prefix_means_a_person_is_typing(self):
        self.assertEqual(h.detect_mode(["what were you doing?"], self.compact), "direct")

    def test_an_unrecognised_channel_is_not_a_person(self):
        """A bracketed prefix this config has never heard of is a channel it
        does not know about. Reading it as a human is a specific, wrong guess;
        skipping to the next message is not."""
        self.assertEqual(h.detect_mode(["[some-new-channel] hi"], self.compact), "unknown")

    def test_nothing_to_go_on_says_so(self):
        self.assertEqual(h.detect_mode([], self.compact), "unknown")


# ---------------------------------------------------------- what gets injected

class AfterCompaction(Sandbox):

    def test_the_mode_block_is_chosen_from_the_transcript(self):
        t = self.write_transcript([json.dumps(
            {"type": "user", "message": {"content": "[wake 03:00] go"}})])
        out = self.injected(self.run_hook({"source": "compact", "transcript_path": t}))
        self.assertIn("SCHEDULED", out)
        self.assertIn("FOOT", out)
        self.assertIn("mode=scheduled", self.log_text())

    def test_no_transcript_falls_back_to_unknown_rather_than_guessing(self):
        out = self.injected(self.run_hook({"source": "compact"}))
        self.assertIn("UNKNOWN", out)

    def test_an_oversized_note_drops_its_optional_half(self):
        """A note too long to read is the same as no note, except it also costs
        the context you were trying to protect."""
        cfg = json.loads(json.dumps(MINIMAL_CONFIG))
        cfg["compact"]["blocks"]["unknown"] = "X" * (h.MAX_CHARS + 100)
        cfg["compact"]["footer"] = "DROP ME"
        self.write_config(cfg)
        out = self.injected(self.run_hook({"source": "compact"}))
        self.assertNotIn("DROP ME", out)


class AtStartup(Sandbox):

    def checks(self, checks, **payload):
        cfg = json.loads(json.dumps(MINIMAL_CONFIG))
        cfg["startup_checks"] = checks
        self.write_config(cfg)
        p = self.run_hook(dict({"source": "startup"}, **payload))
        return self.injected(p)

    def test_the_list_existing_is_the_proof_the_hook_is_alive(self):
        out = self.checks({})
        self.assertIn("hook", out)
        self.assertIn("source=startup", out)

    def test_an_unset_env_var_reports_the_real_default_not_a_blank(self):
        """"Not set" is the answer people skip past. The number is the one that
        makes someone act: 100,000,000 ms is 27.8 hours."""
        out = self.checks({"env": [{
            "label": "MCP timeout", "key": "MCP_TOOL_TIMEOUT",
            "if_unset": "unset = 100,000,000 ms (27.8 hours — there is no timeout)"}]})
        self.assertIn("WARN MCP timeout", out)
        self.assertIn("27.8 hours", out)

    def test_a_value_that_is_set_reports_the_value(self):
        self.write_settings({"env": {"MCP_TOOL_TIMEOUT": "300000"}})
        out = self.checks({"env": [{
            "label": "MCP timeout", "key": "MCP_TOOL_TIMEOUT", "if_set": "{value} ms",
            "if_unset": "unset"}]})
        self.assertIn("OK", out)
        self.assertIn("300000 ms", out)

    def test_a_setting_that_is_not_what_you_expect_is_a_warning(self):
        """Present is not the same as correct. A config file that has the key
        at the wrong value reads, at a glance, exactly like one that is right."""
        self.write_settings({"permissions": {"defaultMode": "default"}})
        out = self.checks({"settings": [{
            "label": "permission mode", "key": "permissions.defaultMode",
            "expect": "bypassPermissions", "if_unset": "not set"}]})
        self.assertIn("WARN permission mode", out)

    def test_a_stale_scheduled_job_is_a_warning(self):
        old = os.path.join(self.dir, "nightly.log")
        open(old, "w").close()
        long_ago = time.time() - 60 * 60 * 48
        os.utime(old, (long_ago, long_ago))
        out = self.checks({"freshness": [
            {"label": "nightly job", "path": old, "max_age_hours": 30}]})
        self.assertIn("WARN nightly job", out)

    def test_a_scheduled_job_that_never_ran_is_not_silence(self):
        """A missing log file and a fresh one both produce no error output from
        the job itself. Only one of them means it ran."""
        out = self.checks({"freshness": [
            {"label": "nightly job", "path": os.path.join(self.dir, "never"),
             "max_age_hours": 30}]})
        self.assertIn("WARN nightly job", out)
        self.assertIn("never ran", out)

    def test_a_missing_file_is_reported_rather_than_skipped(self):
        out = self.checks({"files": [
            {"label": "CLAUDE.md", "path": os.path.join(self.dir, "CLAUDE.md")}]})
        self.assertIn("WARN CLAUDE.md", out)

    def test_it_tells_the_agent_that_on_disk_is_not_loaded(self):
        out = self.checks({})
        self.assertIn("real tool call", out)


# ----------------------------------------------------------------- failing

class WhenItBreaks(Sandbox):

    def test_a_broken_config_cannot_stop_a_session_starting(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        p = self.run_hook({"source": "startup"})
        self.assertEqual(p.returncode, 0)

    def test_garbage_on_stdin_cannot_stop_a_session_starting(self):
        env = dict(os.environ)
        env.pop("TMUX", None)
        env.update(SESSION_VITALS_CONFIG=self.config_path,
                   SESSION_VITALS_LOG=self.log_path,
                   SESSION_VITALS_AGENT="main")
        p = subprocess.run([sys.executable, HOOK], input="not json at all",
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(p.returncode, 0)

    def test_an_unknown_source_produces_no_output(self):
        p = self.run_hook({"source": "something-new"})
        self.assertEqual(p.stdout.strip(), "")
        self.assertIn("unknown source", self.log_text())

    def test_it_never_fails_silently(self):
        """The one that matters. Every exit path above returns 0, which is what
        keeps a broken hook from breaking the session — and is also exactly
        what makes a hook that died three weeks ago look like a hook with
        nothing to say. The log line is the entire difference."""
        for payload, agent in (({"source": "startup"}, "not-an-agent"),
                               ({"source": "nonsense"}, "main")):
            open(self.log_path, "w").close()
            self.run_hook(payload, agent=agent)
            self.assertTrue(self.log_text().strip(),
                            "exited 0 and wrote nothing down: %r" % (payload,))


if __name__ == "__main__":
    unittest.main()
