"""Discussion threads — the conversation behind the clock.

The board can say that a reviewer is waiting on the author; it cannot say what
*for*. That answer lives in a thread on GitLab, one page-load away, and looking
it up is the most common reason to leave radar at all. The poller already
fetches every MR's discussions to derive events, so keeping what people said
costs no extra API call and puts the reason next to the obligation it explains.

Stored, not derived. Everything else radar shows is replayed from the immutable
event log, but a thread's ``resolved`` flag is mutable state that only GitLab
knows, and an edited or deleted comment leaves no note behind. So each poll
replaces an MR's threads wholesale (see ``Database.replace_threads``): resolving
a thread on GitLab clears it here on the next pass.

Only human notes are kept. System notes ("requested review from @dan") are the
audit trail events are made of, and repeating them as conversation would bury
the two sentences someone actually needs to read.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A comment is meant to be read here rather than on GitLab, so the cap is
# generous — but a pasted stack trace or a base64 blob must not be able to make
# a board row megabytes wide. Whatever is cut is still one click away.
MAX_BODY_CHARS = 4000


@dataclass(frozen=True)
class Thread:
    """One resolvable discussion (or one standalone comment) on an MR.

    ``author`` is whoever opened it — the person a reviewer chip names — even
    when later replies come from someone else. ``resolvable`` distinguishes a
    diff/discussion thread, which someone has to resolve, from a plain comment,
    which nobody can.
    """

    project_id: int
    mr_iid: int
    discussion_id: str
    author: str | None
    created_at: str | None
    updated_at: str | None
    resolvable: bool
    resolved: bool
    resolved_by: str | None
    resolved_at: str | None
    file_path: str | None
    line: int | None
    notes: list[dict] = field(default_factory=list)

    @property
    def open(self) -> bool:
        """Whether someone still owes this thread a resolution."""
        return self.resolvable and not self.resolved


def _truncate(body: object) -> tuple[str, bool]:
    text = str(body or "")
    if len(text) <= MAX_BODY_CHARS:
        return text, False
    return text[:MAX_BODY_CHARS], True


def _username(user: object) -> str | None:
    return user.get("username") if isinstance(user, dict) else None


def _position(note: dict) -> tuple[str | None, int | None]:
    """The file and line a diff note is anchored to, if it is one.

    ``new_*`` first: a comment on a changed line is nearly always about the new
    version, and falling back to ``old_*`` covers a note left on a deleted line.
    """
    pos = note.get("position") or {}
    if not isinstance(pos, dict):
        return None, None
    path = pos.get("new_path") or pos.get("old_path")
    raw_line = pos.get("new_line") or pos.get("old_line")
    try:
        line = int(raw_line) if raw_line is not None else None
    except (TypeError, ValueError):
        line = None
    return (str(path) if path else None), line


def thread_from_discussion(project_id: int, mr_iid: int, discussion: dict) -> Thread | None:
    """Build one thread from a GitLab discussion, or None if it is all system notes."""
    notes = [n for n in (discussion.get("notes") or []) if not n.get("system")]
    if not notes:
        return None

    # A thread is resolved only when every note that *can* be resolved is: GitLab
    # tracks the flag per note, and a reply added after resolution reopens it.
    resolvable_notes = [n for n in notes if n.get("resolvable")]
    resolvable = bool(resolvable_notes)
    resolved = resolvable and all(bool(n.get("resolved")) for n in resolvable_notes)

    resolved_by = resolved_at = None
    if resolved:
        for note in resolvable_notes:
            resolved_by = resolved_by or _username(note.get("resolved_by"))
            resolved_at = resolved_at or note.get("resolved_at")

    file_path = line = None
    for note in notes:
        file_path, line = _position(note)
        if file_path:
            break

    stored: list[dict] = []
    for note in notes:
        body, truncated = _truncate(note.get("body"))
        stored.append(
            {
                "id": note.get("id"),
                "author": _username(note.get("author")),
                "created_at": note.get("created_at"),
                "body": body,
                "truncated": truncated,
            }
        )

    timestamps = [n.get("updated_at") or n.get("created_at") for n in notes]
    return Thread(
        project_id=project_id,
        mr_iid=mr_iid,
        discussion_id=str(discussion.get("id") or notes[0].get("id")),
        author=stored[0]["author"],
        created_at=stored[0]["created_at"],
        updated_at=max((t for t in timestamps if t), default=None),
        resolvable=resolvable,
        resolved=resolved,
        resolved_by=resolved_by,
        resolved_at=resolved_at,
        file_path=file_path,
        line=line,
        notes=stored,
    )


def threads_from_discussions(
    project_id: int, mr_iid: int, discussions: list[dict]
) -> list[Thread]:
    """Every human conversation on an MR, in the order GitLab returned them."""
    out = []
    for discussion in discussions:
        thread = thread_from_discussion(project_id, mr_iid, discussion)
        if thread is not None:
            out.append(thread)
    return out
