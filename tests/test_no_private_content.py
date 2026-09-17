#!/usr/bin/env python3
"""The privacy tests. Run them before every push.

    python3 -m unittest discover -s tests -v

This repository ships no data directory, which makes it feel safer than it is.
Nothing here will leak the way a database leaks. It leaks by *quotation*: a
real message pasted into a fixture to reproduce a parsing bug, a real path left
in an example, a real name inside a comment explaining what went wrong. All of
those look like documentation right up until someone reads them.

So there are two halves, and neither is enough alone:

  `.gitignore`   stops files you never meant to add.
  this file      stops content inside files you did mean to add.

**The list of words that would identify you is not in this file.** Publishing
your own denylist publishes exactly the names you were trying to keep out of
the repo — the test would leak what the test exists to prevent. Put them in
`.private-terms.txt`, which is gitignored. `.private-terms.example.txt` shows
the shape.

The structural checks below need no list at all, because they look for kinds of
string rather than particular strings: absolute home directories, IP literals,
email addresses, and anything long enough to be a key.
"""

import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXT_SUFFIXES = (".md", ".py", ".sh", ".json", ".txt", ".yml", ".yaml",
                 ".toml", ".jsonl", ".cfg", ".example")

# Two files are exempt, and only two. This one, because describing a pattern
# means containing it. And the denylist, because it is a list of the exact
# strings the denylist forbids — scanning it always finds everything, which is
# a finding with no information in it.
EXEMPT = {
    os.path.relpath(os.path.abspath(__file__), REPO),
    ".private-terms.txt",
}


def git(*args):
    return subprocess.run(["git"] + list(args), cwd=REPO,
                          capture_output=True, text=True)


def in_a_git_repo():
    return git("rev-parse", "--git-dir").returncode == 0


def files_to_check():
    """What a push would actually publish.

    Inside a repo that is `git ls-files`, which is the honest answer: a file
    can be gitignored and still be tracked, if it was added before the rule
    existed, and then `.gitignore` says one thing while git does another.

    Before `git init` it falls back to walking the tree, so these tests are
    useful while the repo is still being built rather than one commit late.
    """
    if in_a_git_repo():
        out = git("ls-files", "-z").stdout
        names = [n for n in out.split("\0") if n]
    else:
        names = []
        for root, dirs, fs in os.walk(REPO):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for f in fs:
                names.append(os.path.relpath(os.path.join(root, f), REPO))

    keep = []
    for n in names:
        if n in EXEMPT or n.endswith(".example.txt"):
            continue
        if n.endswith(TEXT_SUFFIXES) or "." not in os.path.basename(n):
            keep.append(n)
    return keep


def read(name):
    try:
        with open(os.path.join(REPO, name), encoding="utf-8", errors="replace") as f:
            return f.read()
    except (IsADirectoryError, FileNotFoundError):
        return ""


# ------------------------------------------------------- structural findings

# An absolute home directory names the account it belongs to. `~/` says the
# same thing without saying who.
HOME_PATH = re.compile(r"(?:/Users/|/home/)(?!<|\$|username\b|you\b)[A-Za-z0-9._-]+")

# Loopback and the documentation ranges are fine. A real address is a server
# someone can go and knock on.
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV4_OK = re.compile(r"^(?:127\.|0\.0\.0\.0$|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.|"
                     r"255\.|192\.0\.2\.|198\.51\.100\.|203\.0\.113\.)")

EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
EMAIL_OK = re.compile(r"@(?:example\.(?:com|org|net)|users\.noreply\.github\.com)$")

# Known key shapes, plus a catch-all for anything long enough to be a secret or
# a hash of something real.
KEY_PREFIX = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
                        r"xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[0-9A-Z]{16})\b")
LONG_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*[0-9])(?=[A-Za-z0-9_-]*[A-Za-z])"
                        r"[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])")


def findings(text):
    hits = []
    for m in HOME_PATH.finditer(text):
        hits.append("absolute home directory: %s" % m.group(0))
    for m in IPV4.finditer(text):
        if not IPV4_OK.match(m.group(0)):
            hits.append("routable IP address: %s" % m.group(0))
    for m in EMAIL.finditer(text):
        if not EMAIL_OK.search(m.group(0)):
            hits.append("email address: %s" % m.group(0))
    for m in KEY_PREFIX.finditer(text):
        hits.append("credential: %s..." % m.group(0)[:8])
    for m in LONG_TOKEN.finditer(text):
        hits.append("long token (key? hash of something real?): %s..." % m.group(0)[:8])
    return hits


class NothingStructural(unittest.TestCase):

    def test_no_identifying_strings_in_anything_publishable(self):
        bad = []
        for name in files_to_check():
            for hit in findings(read(name)):
                bad.append("%s: %s" % (name, hit))
        self.assertEqual(bad, [], "\n" + "\n".join(bad))

    def test_the_scan_can_actually_fail(self):
        """A scanner that matches nothing passes every repository. This is the
        test for the test."""
        self.assertTrue(findings("see /Users/someone/notes"))
        self.assertTrue(findings("ssh 8.8.8.8"))
        self.assertTrue(findings("mail me at real.person@gmail.com"))
        self.assertTrue(findings("token sk-abcdefghijklmnopqrstuvwx"))
        self.assertFalse(findings("curl http://127.0.0.1:8080/health"))
        self.assertFalse(findings("open ~/agents/main/CLAUDE.md"))

        # The shape git invents when no identity is configured: your account
        # name at your laptop's hostname.
        self.assertTrue(findings("someone@their-MacBook-Pro.local"))
        self.assertFalse(findings("1234567+handle@users.noreply.github.com"))


def private_terms():
    """Your own words, from a file that is not in the repo.

    Kept inside the repo by default, and gitignored — the same trade the config
    makes. Set SESSION_VITALS_PRIVATE_TERMS if you would rather it never sat in
    the directory at all. Returns None when there is no list, which is a
    different thing from an empty one.
    """
    path = (os.environ.get("SESSION_VITALS_PRIVATE_TERMS")
            or os.path.join(REPO, ".private-terms.txt"))
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f
                if ln.strip() and not ln.lstrip().startswith("#")]


NO_LIST = (".private-terms.txt not present — structural checks only. Copy "
           ".private-terms.example.txt to .private-terms.txt and fill it in "
           "before you publish.")


class NothingPersonal(unittest.TestCase):

    def test_none_of_your_private_terms_appear(self):
        terms = private_terms()
        if terms is None:
            self.skipTest(NO_LIST)
        bad = []
        for name in files_to_check():
            low = read(name).lower()
            for t in terms:
                if t.lower() in low:
                    bad.append("%s contains a private term" % name)
        self.assertEqual(bad, [], "\n" + "\n".join(sorted(set(bad))))


class NobodysNameIsInTheHistory(unittest.TestCase):
    """Who the commits say they are from.

    This is the one the content scan above structurally cannot reach: an author
    name lives in commit metadata, not in any file, so a scanner that reads
    files finds nothing and reports clean. GitHub shows it on every commit and
    every blame line.

    It is also the easiest one to hit by accident. If neither the repo nor your
    global config sets `user.name`, git does not refuse — it *guesses*, from
    your operating system account and your machine's hostname. The guess is
    your real name and `you@your-laptop.local`. Nothing warns you; the commit
    just succeeds.
    """

    def setUp(self):
        if not in_a_git_repo():
            self.skipTest("not a git repository yet")
        out = git("log", "--format=%an%n%ae%n%cn%n%ce").stdout
        self.identities = sorted(set(x.strip() for x in out.splitlines() if x.strip()))
        if not self.identities:
            self.skipTest("no commits yet")

    def test_no_real_name_or_machine_name_on_any_commit(self):
        terms = private_terms() or []
        bad = []
        for who in self.identities:
            for hit in findings(who):
                bad.append("%s: %s" % (who, hit))
            for t in terms:
                if t.lower() in who.lower():
                    bad.append("%s: contains a private term" % who)
        self.assertEqual(sorted(set(bad)), [], "\n" + "\n".join(sorted(set(bad))) + """

Fix it before you push — history is the part you cannot quietly edit later:

    git config --local user.name  "your-github-handle"
    git config --local user.email "ID+handle@users.noreply.github.com"
    git commit --amend --reset-author --no-edit     # unpushed commits only
""")


class GitignoreIsDoingItsJob(unittest.TestCase):

    def setUp(self):
        if not in_a_git_repo():
            self.skipTest("not a git repository yet")

    def test_the_rules_that_matter_are_real_rules(self):
        """Ask git, don't read the file. A `#` anywhere but the start of a line
        makes a pattern that matches nothing while looking like it works."""
        for path in ("vitals.json", "hook.log", "logs/x.log",
                     "transcripts/a.jsonl", ".private-terms.txt"):
            r = git("check-ignore", "--no-index", "-q", path)
            self.assertEqual(r.returncode, 0, "%s is NOT ignored" % path)

    def test_the_example_config_is_still_committable(self):
        r = git("check-ignore", "--no-index", "-q", "vitals.example.json")
        self.assertNotEqual(r.returncode, 0,
                            "vitals.example.json is ignored — the negation broke")

    def test_nothing_ignored_was_ever_committed(self):
        """A rule added after the file was tracked changes nothing. Git keeps
        showing you the file and `.gitignore` keeps looking correct."""
        tracked = git("ls-files", "-z").stdout.split("\0")
        tracked = [t for t in tracked if t]
        if not tracked:
            self.skipTest("nothing committed yet")
        r = subprocess.run(["git", "check-ignore", "--stdin", "-z"],
                           cwd=REPO, input="\0".join(tracked),
                           capture_output=True, text=True)
        leaked = [x for x in r.stdout.split("\0") if x]
        self.assertEqual(leaked, [],
                         "tracked despite being gitignored: %s" % leaked)


if __name__ == "__main__":
    unittest.main()
