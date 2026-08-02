"""Per-MR checkouts, so a skill reads the code the merge request actually proposes.

Pointing a skill at ``source:`` alone hands it whatever the checkout happens to
be sitting on — usually the default branch, sometimes a colleague's half-finished
rebase, and *never* reliably the MR under review. Worse, two jobs started from
the board share that one working tree: they would review each other's checkout.

With ``checkout: worktree`` radar gives each job its own detached ``git
worktree`` at the merge request's head commit, and removes it when the job ends.
Jobs stay independent, the tree matches the diff the skill was handed, and the
operator's own working copy is never touched — a worktree adds no branch, moves
no HEAD, and leaves nothing behind but its entry in ``.git/worktrees`` until it
is removed.

The commit comes from GitLab's per-MR ref (``refs/merge-requests/<iid>/head``),
which every GitLab server exposes and which resolves even for MRs from forks —
where the source branch does not exist on the target repository at all. The
fetch uses the operator's normal git credentials for that remote; radar's own
GitLab token is not involved and is not passed on.

Nothing here is best-effort: if the fetch or the worktree fails, the job fails.
A skill that quietly reviewed the default branch instead of the merge request
would produce findings that look entirely plausible and are about other code.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("radar.worktree")

# GitLab publishes every MR's head commit here, including MRs from forks.
MR_REF = "refs/merge-requests/{iid}/head"

# Where a job's fetched commit is parked locally. Its own namespace, so sweeping
# radar's leftovers can never touch a ref the operator created.
REF_NS = "refs/radar"

_FETCH_TIMEOUT_S = 300  # a cold fetch of a large repo
_GIT_TIMEOUT_S = 60     # every other plumbing call

# How long a ref with no worktree yet is left alone. A job holds one from the
# start of its fetch until `worktree add` registers the directory, so anything
# younger than the longest possible fetch may still be on its way. The age is
# carried in the ref's own name rather than in this process's memory, because a
# second radar (or a second run) sweeping the same repository must reach the
# same conclusion about a ref it did not create.
_REF_GRACE_S = _FETCH_TIMEOUT_S + 60


class WorktreeError(RuntimeError):
    """Preparing the merge request's checkout failed; the job must not run."""


def _git(args: list[str], cwd: str | Path, timeout: int = _GIT_TIMEOUT_S) -> str:
    """Run one git command, returning stdout. Raises WorktreeError on failure."""
    try:
        done = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git is not on PATH, so radar cannot prepare a worktree") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git {args[0]} timed out after {timeout}s") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exited with code {done.returncode}"
        raise WorktreeError(f"git {args[0]} failed: {tail}")
    return done.stdout.strip()


def is_git_repo(root: str | Path) -> bool:
    try:
        return _git(["rev-parse", "--is-inside-work-tree"], root) == "true"
    except WorktreeError:
        return False


def _resolves(root: str | Path, rev: str) -> bool:
    """Whether `rev` names a commit this repository already has."""
    try:
        _git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], root)
        return True
    except WorktreeError:
        return False


@dataclass
class Worktree:
    """A checkout that belongs to one job and is removed with it."""

    path: Path
    root: Path
    ref: str
    _parent: Path

    def cleanup(self) -> None:
        """Remove the worktree. Never raises: the job is already over, and a
        failure to tidy up must not turn a finished review into an error."""
        try:
            _git(["worktree", "remove", "--force", str(self.path)], self.root)
        except WorktreeError as exc:
            log.warning("could not remove worktree %s: %s", self.path, exc)
        shutil.rmtree(self._parent, ignore_errors=True)
        # The ref pins the MR's commit against gc for as long as it exists, so
        # it goes with the worktree it was fetched for.
        with suppress(WorktreeError):
            _git(["update-ref", "-d", self.ref], self.root)
        prune(self.root)


def prune(root: str | Path) -> None:
    """Drop worktrees and refs left behind by a job that never got to clean up.

    Job threads are daemons, so a radar that is killed mid-review leaves its
    worktree registered and its fetch ref in place — and the ref keeps the whole
    commit alive. Neither is self-correcting, so both are swept here (on every
    cleanup, and before every create) rather than accumulating in the operator's
    repository forever. Never raises: tidying is not worth failing a job over.

    A ref is *not* judged dead by its worktree alone. Between a job's fetch and
    its ``worktree add`` the ref exists with nothing registered against it, so a
    sweep running just then would delete a live job's commit out from under it
    and fail a review that was about to work. The grace period covers that
    window — and covers it for a *second* radar sharing the checkout, which
    in-process bookkeeping could not.
    """
    with suppress(WorktreeError):
        _git(["worktree", "prune"], root)
    with suppress(WorktreeError):
        live = _git(["worktree", "list", "--porcelain"], root)
        now = time.time()
        for ref in _git(["for-each-ref", "--format=%(refname)", REF_NS], root).splitlines():
            stamp, _, token = ref.rsplit("/", 1)[-1].partition("-")
            try:
                age = now - float(stamp)
            except ValueError:
                continue  # not a name this wrote; leave it for whoever owns it
            # Registered against a worktree, or possibly still being fetched.
            if not token or token in live or age < _REF_GRACE_S:
                continue
            with suppress(WorktreeError):
                _git(["update-ref", "-d", ref], root)


def create_mr_worktree(
    root: str | Path,
    mr_iid: int,
    head_sha: str | None = None,
    remote: str = "origin",
    timeout_s: float | None = None,
) -> Worktree:
    """Check out merge request ``mr_iid`` into a throwaway worktree of ``root``.

    The fetch lands in a ref of this job's own, never ``FETCH_HEAD``: that file
    is one per repository, so two jobs fetching at once overwrite each other's
    and a review is served the *other* merge request's code — a plausible,
    confident report about a change nobody asked about. Measured before this
    was fixed, half of all concurrent pairs came back swapped.

    The freshly fetched commit wins over ``head_sha``, which is only as new as
    the last poll: the diff a skill is handed comes from GitLab live, so pinning
    the tree to a stale snapshot would show it a diff of code it cannot see.
    ``head_sha`` is the fallback for when the fetch fails.

    ``timeout_s`` bounds the fetch to what is left of the job's own budget, so
    preparing a checkout cannot outlast the review it is for.
    """
    root = Path(root)
    if not is_git_repo(root):
        raise WorktreeError(
            f"{root} is not a git repository, so 'checkout: worktree' cannot prepare "
            "this merge request — point 'source:' at a checkout, or drop 'checkout'"
        )
    prune(root)
    fetch_timeout = _FETCH_TIMEOUT_S
    if timeout_s is not None:
        fetch_timeout = max(1.0, min(_FETCH_TIMEOUT_S, timeout_s))

    # `git worktree add` wants a path that does not exist yet, so the temp
    # directory is the parent and the worktree is created inside it. The
    # directory's *name* matters beyond that: git registers a worktree under
    # `.git/worktrees/<basename>` and the fetch ref is named after it, so a
    # fixed name would leave concurrent jobs relying on git's numeric
    # de-duplication and make both registries unreadable.
    parent = Path(tempfile.mkdtemp(prefix=f"radar-mr-{mr_iid}-"))
    path = parent / parent.name
    # The creation time leads the name so any sweeper, in any process, can tell
    # a ref that is still being fetched from one a dead job left behind.
    local_ref = f"{REF_NS}/{int(time.time())}-{parent.name}"

    mr_ref = MR_REF.format(iid=mr_iid)
    try:
        _git(
            ["fetch", "--no-tags", remote, f"{mr_ref}:{local_ref}"],
            root,
            timeout=fetch_timeout,
        )
        target = local_ref
    except WorktreeError as exc:
        # A repo that already has the commit (someone fetched it, or the remote
        # is named differently) can still be used; anything else is fatal.
        log.warning("fetching %s from %s failed: %s", mr_ref, remote, exc)
        target = head_sha if head_sha and _resolves(root, head_sha) else ""
        if not target:
            shutil.rmtree(parent, ignore_errors=True)
            raise WorktreeError(
                f"could not get merge request {mr_iid}'s commit: fetching {mr_ref} from "
                f"'{remote}' failed and {head_sha or 'its head commit'} is not in {root}. "
                "Check the remote name (skills take 'remote:') and that this checkout is "
                "of the same project."
            ) from exc

    try:
        _git(["worktree", "add", "--detach", str(path), target], root)
    except WorktreeError:
        shutil.rmtree(parent, ignore_errors=True)
        with suppress(WorktreeError):
            _git(["update-ref", "-d", local_ref], root)
        raise
    return Worktree(path=path, root=root, ref=local_ref, _parent=parent)
