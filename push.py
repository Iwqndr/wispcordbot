import os
import re
import shutil
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
    """`[remote-url]`, `-m <message>`, `--force`, `-h`."""
    url = None
    message = DEFAULT_MESSAGE
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
    # "HEAD" means detached. Empty means no commits yet.
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


def clear_stuck_rebase() -> None:
    """Abort or clear any rebase left over from a previous run.

    A leftover rebase-merge directory blocks every future `git pull --rebase`
    and `git rebase`, so we clean it up before doing anything else.
    """
    if not rebase_in_progress():
        return
    info("A previous rebase is still in progress - aborting it.")
    abort = run("rebase", "--abort")
    if abort.returncode != 0:
        # git refused (already resolved, or dir is stale). Remove the state
        # directories directly so we can move on.
        info("`git rebase --abort` did not clear it - removing rebase state.")
        gd = git_dir()
        for name in ("rebase-merge", "rebase-apply"):
            target = gd / name
            if target.exists():
                try:
                    shutil.rmtree(target)
                except OSError as exc:
                    fail(f"Could not remove {target}: {exc}")
    # Clear stale MERGE_HEAD / CHERRY_PICK_HEAD etc. that block commits.
    gd = git_dir()
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"):
        target = gd / name
        if target.exists():
            try:
                target.unlink()
            except OSError:
                pass
    ok("Cleared the stuck rebase state.")


def ensure_on_branch() -> str:
    """Make sure we're on a real branch, creating/moving one if needed."""
    if not has_any_commit():
        # Fresh repo, no commits yet. Point HEAD at the deploy branch so the
        # first commit lands there.
        if current_branch() != BRANCH:
            info(f"No commits yet - pointing HEAD at '{BRANCH}'.")
            run("symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
        return BRANCH

    branch = current_branch()
    if branch:
        return branch

    # Detached HEAD with commits. Attach to the deploy branch.
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
        # Git older than 2.28 has no `-b`, so init then point HEAD at main
        # before the first commit exists.
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
    info("It is local to this repository; change it with `git config user.email ...`.")


def has_unpushed_commits(branch: str) -> bool:
    """True when local HEAD is ahead of origin/<branch>."""
    # If the remote branch doesn't exist, any local commit is unpushed.
    if run("ls-remote", "--exit-code", "--heads", "origin", branch).returncode != 0:
        # No commits at all? Nothing to push.
        if not has_any_commit():
            return False
        return True
    # Count commits on HEAD not on origin/<branch>.
    result = run("rev-list", "--count", f"origin/{branch}..HEAD")
    try:
        return int(result.stdout.strip() or "0") > 0
    except ValueError:
        return False


def main() -> None:
    remote_arg, message, force = parse_args(sys.argv[1:])

    if not git_available():
        fail("git was not found on PATH. Install Git for Windows, then reopen "
             "this terminal.")

    remote_url = (remote_arg or os.getenv("WISP_REMOTE") or DEFAULT_REMOTE).strip()

    ensure_repo(remote_url, force)
    ensure_identity()

    # A leftover rebase from a previous run blocks every future rebase, so
    # clear it before we touch anything else.
    clear_stuck_rebase()

    branch = ensure_on_branch()

    status = run("status", "--porcelain").stdout.strip()

    if status:
        info("Changes to push:")
        for line in status.splitlines():
            print(f"     {line}")

        info("Staging...")
        add = run("add", ".")
        if add.returncode != 0:
            fail(f"git add failed:\n{(add.stderr or add.stdout).strip()}")

        # Only commit when staging actually produced something.
        if run("diff", "--cached", "--quiet").returncode != 0:
            info(f"Committing as: {message!r}")
            commit = run("commit", "-m", message)
            if commit.returncode != 0:
                fail(f"git commit failed:\n{(commit.stderr or commit.stdout).strip()}\n\n"
                     "If it complained about who you are, set your identity once:\n"
                     "    git config user.name \"Your Name\"\n"
                     "    git config user.email \"you@example.com\"")
        else:
            info("Nothing staged to commit (everything may be ignored).")
    else:
        info("Working tree is clean.")

    # Re-read the branch: committing may have been what first created it.
    branch = current_branch() or branch

    # Check for unpushed commits, not just a dirty tree.
    if not has_unpushed_commits(branch):
        ok("Nothing to push. Working tree is clean and remote is up to date.")
        return

    if branch != BRANCH:
        info(f"Note: you are on '{branch}', but Wispbyte is configured for the "
             f"'{BRANCH}' branch. Run `git branch -M {BRANCH}` if that is not "
             f"intended.")

    # Pull only when the remote actually has this branch.
    if run("ls-remote", "--exit-code", "--heads", "origin", branch).returncode == 0:
        info(f"Rebasing onto origin/{branch}...")
        pull = run("pull", "--rebase", "origin", branch)
        if pull.returncode != 0:
            # The most common failure here is a conflict. Don't leave the
            # rebase half-done: tell the user, then abort so the next run is
            # clean. The user's local commit is preserved either way.
            print((pull.stderr or pull.stdout).strip())
            if rebase_in_progress():
                info("Conflicts detected - aborting the rebase so the next run "
                     "starts clean. Your local commit is preserved.")
                run("rebase", "--abort")
                fail("git pull --rebase hit conflicts. Your commit is safe on "
                     "'{0}'. Pull manually with `git pull --rebase origin {0}`, "
                     "resolve conflicts, then re-run push.py.".format(branch))
            fail("git pull failed. Your commit is saved locally; retry when "
                 "ready.")
    else:
        info(f"origin/{branch} does not exist yet - this push will create it.")

    info("Pushing to origin...")
    # Push the branch by name, not HEAD: if HEAD is detached for any reason,
    # `git push origin HEAD` fails with "not a full refname".
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