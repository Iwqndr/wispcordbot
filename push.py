"""Push this folder to its own GitHub repository, setting it up if needed.

Usage:
    python push.py                        set up if needed, then push
    python push.py <remote-url>           use a different GitHub repo
    python push.py -m "commit message"    override the commit message
    python push.py --force                allow setting up inside another repo

Run it from inside the member bot folder. If this is not a repository yet it
will do the whole first-time setup for you:

    1. `git init` on branch `main`
    2. add the `origin` remote
    3. set a commit identity for this repository only (never your global git
       config), derived from the repository owner

Then it stages everything (respecting .gitignore), commits, rebases onto
whatever is already on the remote, and pushes. The first push creates the
upstream, so a brand-new repository needs no extra commands.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Change this if your member bot repository is somewhere else. It can also be
# passed as the first argument, or set as the WISP_REMOTE environment variable.
DEFAULT_REMOTE = "https://github.com/Iwqndr/wispcordbot.git"
DEFAULT_MESSAGE = "new update"

# Wispbyte is configured to deploy the `main` branch.
BRANCH = "main"


def run(*args, check=False):
    """Run git in this folder.

    `encoding="utf-8", errors="replace"` matters on Windows: git writes UTF-8
    while the console code page is usually cp1252, so a non-ASCII filename would
    otherwise raise UnicodeDecodeError instead of being printed.
    """
    return subprocess.run(
        ["git", *args],
        cwd=HERE,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=check,
    )


def fail(message: str) -> None:
    print(f"[X] {message}")
    sys.exit(1)


def ok(message: str) -> None:
    print(f"[OK] {message}")


def info(message: str) -> None:
    print(f"[..] {message}")


def git_available() -> bool:
    try:
        run("--version")
        return True
    except FileNotFoundError:
        return False


def parse_args(argv):
    """`[remote-url]`, `-m <message>`, `--force`, `-h`."""
    url = None
    message = DEFAULT_MESSAGE
    force = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        elif arg in ("-m", "--message"):
            index += 1
            if index < len(argv):
                message = argv[index]
        elif arg.startswith("--message="):
            message = arg.split("=", 1)[1]
        elif arg in ("-f", "--force"):
            force = True
        elif arg.startswith("-"):
            fail(f"Unknown option {arg!r}. Try: python push.py --help")
        elif url is None:
            url = arg
        index += 1
    return url, message, force


def in_a_repo() -> bool:
    """`.exists()`, not `.is_dir()`: in a worktree `.git` is a file."""
    return (HERE / ".git").exists()


def owning_repo() -> str:
    """The work tree this folder sits inside, when that is not this folder.

    Used to catch the nested-repo case: `git init` inside another repository
    leaves the outer one holding a broken embedded-repo reference.
    """
    result = run("rev-parse", "--show-toplevel")
    if result.returncode != 0:
        return ""
    top = result.stdout.strip()
    if not top:
        return ""
    try:
        if Path(top).resolve() == HERE:
            return ""
    except OSError:
        return ""
    return top


def owner_from_remote(url: str) -> str:
    """The account in a remote URL, used to build the commit identity."""
    match = re.search(r"github\.com[/:]([^/]+)/", url or "")
    if match:
        return match.group(1)
    return HERE.name


def current_branch() -> str:
    """The branch we are on, so nothing here hardcodes `main` after setup."""
    return run("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or BRANCH


def ensure_repo(remote_url: str, force: bool) -> None:
    """Create the repository, branch and remote when they are missing."""

    if in_a_repo():
        if not run("remote", "get-url", "origin").stdout.strip():
            run("remote", "add", "origin", remote_url)
            ok(f"Added remote origin -> {remote_url}")
        return

    outer = owning_repo()
    if outer and not force:
        fail("This folder is inside another git repository:\n"
             f"    {outer}\n\n"
             "Setting one up here would nest a repo inside a repo and leave the "
             "outer one holding a broken embedded-repo reference.\n\n"
             "Move this folder somewhere else and run push.py again, or pass "
             "--force if you really want it here.")

    info("Not a git repository yet - setting one up.")
    if run("init", "-b", BRANCH).returncode != 0:
        # Git older than 2.28 has no `-b`, so init then point HEAD at main
        # before the first commit exists.
        run("init")
        run("symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
    ok(f"Initialised a repository on branch '{BRANCH}'.")

    run("remote", "add", "origin", remote_url)
    ok(f"Added remote origin -> {remote_url}")


def ensure_identity() -> None:
    """Give the repository a commit identity when it has none.

    Written to this repository only - never your global git config - and derived
    from the remote owner so commits are attributed to that GitHub account. If
    you have already configured git, nothing is touched.
    """
    if run("config", "user.email").stdout.strip() and run("config", "user.name").stdout.strip():
        return
    remote = run("remote", "get-url", "origin").stdout.strip() or DEFAULT_REMOTE
    owner = owner_from_remote(remote)
    email = f"{owner}@users.noreply.github.com"
    if not run("config", "user.name").stdout.strip():
        run("config", "user.name", owner)
    if not run("config", "user.email").stdout.strip():
        run("config", "user.email", email)
    info(f"Set this repository's commit identity to {owner} <{email}>.")
    info("It is local to this repository; change it with `git config user.email ...`.")


def main() -> None:
    remote_arg, message, force = parse_args(sys.argv[1:])

    if not git_available():
        fail("git was not found on PATH. Install Git for Windows, then reopen "
             "this terminal.")

    remote_url = (remote_arg or os.getenv("WISP_REMOTE") or DEFAULT_REMOTE).strip()

    ensure_repo(remote_url, force)
    ensure_identity()

    status = run("status", "--porcelain").stdout.strip()
    if not status:
        ok("Nothing to push. Working tree is clean.")
        return

    info("Changes to push:")
    for line in status.splitlines():
        print(f"     {line}")

    info("Staging...")
    add = run("add", ".")
    if add.returncode != 0:
        fail(f"git add failed:\n{(add.stderr or add.stdout).strip()}")

    # Bail before committing when staging produced nothing (everything matched
    # .gitignore, say) rather than making an empty commit.
    if run("diff", "--cached", "--quiet").returncode == 0:
        ok("Nothing to commit. Working tree is clean.")
        return

    info(f"Committing as: {message!r}")
    commit = run("commit", "-m", message)
    if commit.returncode != 0:
        fail(f"git commit failed:\n{(commit.stderr or commit.stdout).strip()}\n\n"
             "If it complained about who you are, set your identity once:\n"
             "    git config user.name \"Your Name\"\n"
             "    git config user.email \"you@example.com\"")

    branch = current_branch()
    if branch != BRANCH:
        info(f"Note: you are on '{branch}', but Wispbyte is configured for the "
             f"'{BRANCH}' branch. Run `git branch -M {BRANCH}` if that is not "
             f"intended.")

    # Pull only when the remote actually has this branch. On the very first push
    # the remote is empty, and `git pull` would fail with
    # "couldn't find remote ref".
    if run("ls-remote", "--exit-code", "--heads", "origin", branch).returncode == 0:
        info(f"Rebasing onto origin/{branch}...")
        pull = run("pull", "--rebase", "origin", branch)
        if pull.returncode != 0:
            fail("git pull failed. Your commit is saved locally - resolve the "
                 "conflicts, then run `git rebase --continue` and push again:\n"
                 f"{(pull.stderr or pull.stdout).strip()}")
    else:
        info(f"origin/{branch} does not exist yet - this push will create it.")

    info("Pushing to origin...")
    # `-u origin HEAD` sets the upstream on the first push and behaves like a
    # plain push afterwards, so a brand-new repo needs no separate command.
    push = run("push", "-u", "origin", "HEAD")
    if push.returncode != 0:
        fail("git push failed. Your commit is saved locally; retry when ready:\n"
             f"{(push.stderr or push.stdout).strip()}")

    ok(f"Pushed to origin/{branch}.")


if __name__ == "__main__":
    main()
