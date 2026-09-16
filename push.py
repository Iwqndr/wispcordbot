import os
import random
import re
import shutil
import string
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Change this if your member bot repository is somewhere else. It can also be
# passed as the first argument, or set as the WISP_REMOTE environment variable.
DEFAULT_REMOTE = "https://github.com/Iwqndr/wispcordbot.git"

# Wispbyte is configured to deploy the `main` branch.
BRANCH = "main"

# Commit message alphabet and length for the random message generated per run.
MESSAGE_ALPHABET = string.ascii_lowercase + string.digits
MESSAGE_LENGTH = 8


def random_message() -> str:
    """A fresh 8-character [a-z0-9] commit message."""
    return "".join(random.choice(MESSAGE_ALPHABET) for _ in range(MESSAGE_LENGTH))


def run(*args, check=False):
    """Run git in this folder."""
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
    pause()
    sys.exit(1)


def ok(message: str) -> None:
    print(f"[OK] {message}")


def info(message: str) -> None:
    print(f"[..] {message}")


def pause() -> None:
    """Keep the window open so double-clicking the script shows the output."""
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass


def git_available() -> bool:
    try:
        run("--version")
        return True
    except FileNotFoundError:
        return False


def parse_args(argv):
    """`[remote-url]`, `-m <message>`, `-f`, `-h`."""
    url = None
    message = None  # None means: generate a random one
    force = False
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-h", "--help"):
            print(__doc__)
            pause()
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
    """The work tree this folder sits inside, when that is not this folder."""
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


def has_any_commit() -> bool:
    """True when HEAD points at a real commit."""
    return run("rev-parse", "--verify", "HEAD").returncode == 0


def current_branch() -> str:
    """The branch we are on, or '' if detached / no commits yet."""
    result = run("rev-parse", "--abbrev-ref", "HEAD")
    name = result.stdout.strip()
    if not name or name == "HEAD":
        return ""
    return name


def git_dir() -> Path:
    """Absolute path to the .git directory (handles worktrees)."""
    result = run("rev-parse", "--git-dir")
    p = Path(result.stdout.strip() or ".git")
    if not p.is_absolute():
        p = (HERE / p).resolve()
    return p


def rebase_in_progress() -> bool:
    """True when a rebase is mid-flight and git is waiting on us."""
    gd = git_dir()
    return (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists()


def clear_stuck_git_state() -> None:
    """Clear any leftover rebase/merge/cherry-pick state."""
    changed = False

    if rebase_in_progress():
        info("A previous rebase is still in progress - aborting it.")
        abort = run("rebase", "--abort")
        if abort.returncode != 0:
            info("`git rebase --abort` did not clear it - removing rebase state.")
            gd = git_dir()
            for name in ("rebase-merge", "rebase-apply"):
                target = gd / name
                if target.exists():
                    try:
                        shutil.rmtree(target)
                    except OSError as exc:
                        fail(f"Could not remove {target}: {exc}")
        changed = True

    gd = git_dir()
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        target = gd / name
        if target.exists():
            try:
                target.unlink()
                changed = True
            except OSError:
                pass

    if changed:
        ok("Cleared leftover git state.")


def ensure_on_branch() -> str:
    """Make sure we're on a real branch, creating/moving one if needed."""
    if not has_any_commit():
        if current_branch() != BRANCH:
            info(f"No commits yet - pointing HEAD at '{BRANCH}'.")
            run("symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
        return BRANCH

    branch = current_branch()
    if branch:
        return branch

    info(f"HEAD is detached - attaching to branch '{BRANCH}'.")
    move = run("checkout", "-B", BRANCH)
    if move.returncode != 0:
        fail(f"Could not attach HEAD to '{BRANCH}':\n"
             f"{(move.stderr or move.stdout).strip()}")
    ok(f"Now on branch '{BRANCH}'.")
    return BRANCH


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
        run("init")
        run("symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
    ok(f"Initialised a repository on branch '{BRANCH}'.")

    run("remote", "add", "origin", remote_url)
    ok(f"Added remote origin -> {remote_url}")


def ensure_identity() -> None:
    """Give the repository a commit identity when it has none."""
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


def remote_has_branch(branch: str) -> bool:
    """True when origin/<branch> already exists on the remote."""
    return run("ls-remote", "--exit-code", "--heads", "origin", branch).returncode == 0


def count_local_changes() -> int:
    """How many files differ from HEAD (staged, unstaged, or untracked)."""
    result = run("status", "--porcelain")
    return len([ln for ln in result.stdout.splitlines() if ln.strip()])


def main() -> None:
    remote_arg, message_opt, force = parse_args(sys.argv[1:])

    if not git_available():
        fail("git was not found on PATH. Install Git for Windows, then reopen "
             "this terminal.")

    remote_url = (remote_arg or os.getenv("WISP_REMOTE") or DEFAULT_REMOTE).strip()

    ensure_repo(remote_url, force)
    ensure_identity()
    clear_stuck_git_state()

    branch = ensure_on_branch()

    info("Fetching origin...")
    fetch = run("fetch", "origin")
    if fetch.returncode != 0:
        fail("git fetch failed:\n"
             f"{(fetch.stderr or fetch.stdout).strip()}")

    has_remote = remote_has_branch(branch)
    if not has_remote:
        info(f"origin/{branch} does not exist yet - this push will create it.")

    dirty = count_local_changes() > 0

    # If local is behind the remote but the tree is clean, fast-forward so we
    # don't accidentally try to push a stale branch.
    if has_remote and not dirty and has_any_commit():
        head = run("rev-parse", "HEAD").stdout.strip()
        origin_head = run("rev-parse", f"origin/{branch}").stdout.strip()
        if head != origin_head:
            # Try a fast-forward-only pull. If it can't (diverged), just leave
            # it; the rebase below will handle it.
            run("merge", "--ff-only", f"origin/{branch}")

    # Commit whatever changed locally. This is the normal path: no reset, no
    # squash, real history preserved.
    if dirty:
        info("Changes to push:")
        for line in run("status", "--porcelain").stdout.splitlines():
            print(f"     {line}")

        info("Staging...")
        add = run("add", "-A")
        if add.returncode != 0:
            fail(f"git add failed:\n{(add.stderr or add.stdout).strip()}")

        if run("diff", "--cached", "--quiet").returncode != 0:
            message = message_opt if message_opt is not None else random_message()
            info(f"Committing as: {message!r}")
            commit = run("commit", "-m", message)
            if commit.returncode != 0:
                fail(f"git commit failed:\n{(commit.stderr or commit.stdout).strip()}")
        else:
            info("Nothing staged to commit (everything may be ignored).")
    else:
        info("Working tree is clean.")

    # Are we ahead of the remote, or identical? If identical, done.
    if has_remote and has_any_commit():
        head = run("rev-parse", "HEAD").stdout.strip()
        origin_head = run("rev-parse", f"origin/{branch}").stdout.strip()
        if head == origin_head:
            ok("Nothing to push. Working tree is clean and remote is up to date.")
            return

    # If the remote moved while we were away, rebase our commits on top of it.
    # -X ours means: for conflicting hunks, keep our side. This is what stops
    # the "rebase conflict" hanging state you hit before.
    if has_remote:
        info(f"Rebasing onto origin/{branch} (-X ours)...")
        pull = run("pull", "--rebase", "-X", "ours", "origin", branch)
        if pull.returncode != 0:
            # Something unexpected - abort the rebase so the next run is clean.
            print((pull.stderr or pull.stdout).strip())
            if rebase_in_progress():
                info("Rebase could not complete - aborting so the next run is clean.")
                run("rebase", "--abort")
            fail("git pull --rebase failed. Your commit is safe locally; retry "
                 "when ready.")

    info("Pushing to origin...")
    push = run("push", "-u", "origin", branch)
    if push.returncode != 0:
        fail("git push failed. Your commit is saved locally; retry when ready:\n"
             f"{(push.stderr or push.stdout).strip()}")

    ok(f"Pushed to origin/{branch}.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[X] Unexpected error: {exc}")
        pause()
        sys.exit(1)
    else:
        pause()