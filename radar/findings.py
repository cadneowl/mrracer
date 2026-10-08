"""What an answer found, counted — so two models' answers can be compared.

Every skill writes markdown, and not in one shape. Three are recognised:

* **A synthesis** (the full review's last step): ``## Blockers``, ``## Should
  fix``, ``## Consider``, ``## Note``, each finding saying who raised it.
* **A QA test plan**: ``🚫 Blocking Gaps``, ``⚠️ Strong Recommendations``,
  ``🔍 Probing / Exploratory``, each item a test the plan proposes.
* **Anything else** — a single reviewer's answer — is read as numbered
  findings (``## 1. BLOCKING — …``), with the severity taken from the words in
  each finding's title.

All three come out in one vocabulary: ``blockers``, ``high``, ``medium`` and
``low``, adding up to ``findings``. A QA plan's items are also its
``tests_proposed``. The rest of what is measured does not depend on the shape
at all — how long the answer is, how many ``file:line`` locations it cites, how
often it hedges with "verify" — and is what keeps a format this module does not
know from reading as a model that found nothing.

Counting is deliberately conservative about structure: sub-headings first,
then bold numbered paragraphs, then plain lists, because the plans put
evidence and field labels (``**Dimension**:``) inside each item and those must
not be counted as items of their own.
"""

from __future__ import annotations

import re

# One line ([ \t], not \s), and no pattern after the title: anything that
# makes the engine retry the title against trailing whitespace is quadratic on
# a long line. Titles are stripped where they are read (`_title`).
_HEADING = re.compile(r"^(#{1,6})[ \t]++(\S.*)$", re.M)


def _title(match: re.Match) -> str:
    return match.group(2).rstrip()
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


def _unfenced(text: str) -> str:
    """The text with every line inside a code fence blanked: a ``# comment``
    in a code sample is not a heading, and would end the section it sits in."""
    out, fence = [], None
    for line in text.split("\n"):
        mark = _FENCE.match(line)
        if fence is None:
            if mark:
                fence = mark.group(1)
            out.append(line)
        else:
            if mark and mark.group(1) == fence:
                fence = None
                out.append(line)
            else:
                out.append("")
    return "\n".join(out)

# A plan's own wrap-up ("### Summary") can sit inside its last section; it is a
# recap of the items, not one of them.
_WRAP_UP = re.compile(
    r"^\W*(?:summary|bottom line|sign-?off|verdict|recommended gate|what i'?d want|"
    r"what i would not|coverage that|clean)\b", re.I,
)

_ITEM_PATTERNS = (
    r"^\*\*(?:[A-Z]{1,3}-?)?\d+\b",            # **9. …**  **1 — …**  **B1: …**
    r"^\d+\.\s",                               # 1. …
    r"^\*\*[^*\n]*[^*:\n]\*\*(?!\s*:)",        # **A bold title** — not **Label**:
    r"^[-*]\s",                                # - …
)

SYNTHESIS_TIERS = {
    "blockers": re.compile(r"^blockers?\b", re.I),
    "high": re.compile(r"^should fix\b", re.I),
    "medium": re.compile(r"^consider\b", re.I),
    "low": re.compile(r"^notes?\b", re.I),
}
QA_TIERS = {
    "blockers": re.compile(r"blocking gaps?", re.I),
    "high": re.compile(r"strong recommendations?", re.I),
    "medium": re.compile(r"probing", re.I),
}

# Severity from the words in a free-form finding's title.
_BLOCKER_WORDS = re.compile(r"\b(block(?:ing|er)s?|critical|must[- ]fix|p0)\b", re.I)
_HIGH_WORDS = re.compile(r"\b(high|should[- ]fix|required|major|strong|p1)\b", re.I)
_LOW_WORDS = re.compile(r"\b(nit|minor|cosmetic|note|low|trivial|fyi|p3)\b", re.I)
_NOT_A_FINDING = re.compile(
    r"^\W*(?:summary|verdict|overview|context|scope|intake|what .* does|on the |"
    r"bottom line|conclusion|tests?\b.*\?$)", re.I,
)

# Bounded and anchored at the start of a token: unbounded, a long run of word
# characters with no "file.ext:N" in it is tried from every offset.
_CITATION = re.compile(r"(?<![\w./-])[\w./-]{1,200}\.[A-Za-z]{1,6}:\d+(?:[-–]\d+)?")
# A source file named anywhere, with or without a line: how much of the code
# the answer actually points at.
_FILE = re.compile(
    r"(?<![\w-])([\w-]{1,100}\.(?:java|kt|scala|groovy|py|ts|tsx|js|go|rs|sql|vpp|xml|ya?ml|"
    r"properties|gradle|json|md|sh))\b"
)
_VERIFY = re.compile(r"\bverify\b|not in this diff|could not (?:check|confirm|execute)", re.I)
# Bold ("**Blocked.**", "**Verdict: Approve**"), or the first words under a
# "## Verdict" heading ("Request changes — …").
_VERDICT = re.compile(
    r"(?:\*\*(?:verdict:?\s*)?|^#{1,4}\s*verdict\W*\n+\s*\**)"
    r"(Blocked|Changes requested|Request changes|"
    r"Approve with (?:required )?changes|Approved?)\b", re.I | re.M,
)
_RAISED_BY = re.compile(r"\*\*Raised by:?\*\*:?\s*([^\n]+)", re.I)


def _sections(text: str) -> list[tuple[int, str, str]]:
    """Every heading as (level, title, the text under it up to the next heading
    of the same or a higher level)."""
    marks = [(m.start(), m.end(), len(m.group(1)), _title(m)) for m in _HEADING.finditer(text)]
    out = []
    for i, (_, end, level, title) in enumerate(marks):
        stop = len(text)
        for start2, _, level2, _ in marks[i + 1:]:
            if level2 <= level:
                stop = start2
                break
        out.append((level, title, text[end:stop]))
    return out


def _count_items(level: int, body: str) -> int:
    """How many findings a section lists, whichever way it lists them."""
    sub = [m for m in _HEADING.finditer(body)
           if len(m.group(1)) > level and not _WRAP_UP.search(_title(m))]
    if sub:
        deepest = min(len(m.group(1)) for m in sub)
        return sum(1 for m in sub if len(m.group(1)) == deepest)
    for pattern in _ITEM_PATTERNS:
        found = len(re.findall(pattern, body, re.M))
        if found:
            return found
    return 0


def _tally(text: str, tiers: dict) -> dict:
    counts = dict.fromkeys(tiers, 0)
    for level, title, body in _sections(text):
        for name, pattern in tiers.items():
            if pattern.search(title):
                counts[name] += _count_items(level, body)
                break
    return counts


def _shape(text: str) -> str:
    titles = [title for _, title, _ in _sections(text)]
    if any(QA_TIERS["blockers"].search(t) for t in titles):
        return "qa"
    # A synthesis's tier headings are just the tier ("## Should fix", maybe
    # with a count); a free-form finding titled "Should fix — …" is not one.
    if any(re.match(r"^\W*(?:blockers?|should fix)\W*(?:\(\d+\))?\s*$", t, re.I)
           for t in titles):
        return "synthesis"
    return "freeform"


def _classify(label: str, counts: dict) -> None:
    if _BLOCKER_WORDS.search(label):
        counts["blockers"] += 1
    elif _HIGH_WORDS.search(label):
        counts["high"] += 1
    elif _LOW_WORDS.search(label):
        counts["low"] += 1
    else:
        counts["medium"] += 1


def _severity_table(text: str) -> list[str] | None:
    """The Severity column of a findings table, when the answer has one.

    A reviewer that ends with ``| # | Finding | Severity |`` has graded its own
    findings, which is better evidence than the words in their headings.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        cells = [c.strip().lower() for c in line.strip().strip("|").split("|")]
        if line.lstrip().startswith("|") and "severity" in cells:
            column = cells.index("severity")
            grades = []
            for row in lines[i + 2:]:
                if not row.lstrip().startswith("|"):
                    break
                parts = [c.strip() for c in row.strip().strip("|").split("|")]
                if len(parts) > column:
                    grades.append(parts[column])
            if grades:
                return grades
    return None


# A numbered finding's heading: "1. …", "Finding 1 — …", "Issue #2: …".
_NUMBERED = re.compile(
    r"^\W*(?:(?:finding|issue|item|concern|problem)\s*#?\s*)?\d+\b[.):—–-]?\s", re.I
)
# An unnumbered one that says its own severity, or only that it is one:
# "Blocking: …", "Should fix — …", "Finding: …".
_GRADED_TITLE = re.compile(
    r"^\W*(?:block(?:ing|er)|critical|must[- ]fix|should[- ]fix|high|medium|low|minor|nit|"
    r"major|recommendation|caution|question|conditional|finding)\b[^:—–-]{0,20}[:—–-]", re.I,
)


def _freeform(text: str) -> dict:
    """Findings in a single reviewer's answer, by severity.

    In order of how much the answer said itself: its own severity table; its
    numbered finding headings ("1. …", "Finding 1 — …"); headings that name
    their own severity ("### Blocking: …"); bold numbered paragraphs.
    """
    counts = {"blockers": 0, "high": 0, "medium": 0, "low": 0}
    graded = _severity_table(text)
    if graded is not None:
        for grade in graded:
            _classify(grade, counts)
        return counts
    titles = [title for level, title, _ in _sections(text) if level >= 2]
    numbered = [
        title for title in titles
        if _NUMBERED.match(title) and not _NOT_A_FINDING.search(_NUMBERED.sub("", title))
    ]
    if not numbered:
        numbered = [title for title in titles if _GRADED_TITLE.match(title)]
    if not numbered:
        numbered = re.findall(r"^\*\*\d+[.)]\s*([^\n]*)", text, re.M)
    for title in numbered:
        _classify(title, counts)
    return counts


def _raised_by(text: str) -> dict:
    """Per source named in "Raised by:", how many findings of each tier.

    Only a synthesis says who raised what, and this is the one measure of a
    reviewer that is not its own word: a finding the synthesis kept, and says
    came from you, is one that survived being checked against the others.
    """
    out: dict[str, dict] = {}
    for _, title, body in _sections(text):
        tier = next((name for name, pattern in SYNTHESIS_TIERS.items()
                     if pattern.search(title)), None)
        if tier is None:
            continue
        for sources in _RAISED_BY.findall(body):
            sources = re.sub(r"\([^)]*\)", "", sources)   # "(unanimous)"
            for name in re.split(r",|\band\b|·", sources):
                name = name.strip(" .*`")
                if not name or len(name) > 40:
                    continue
                into = out.setdefault(name, {"blockers": 0, "high": 0, "medium": 0,
                                             "low": 0, "findings": 0})
                into[tier] += 1
                into["findings"] += 1
    return out


# Bumped whenever counting changes, so rows counted by an older version are
# counted again (see `runlog.reanalyse`) rather than compared with new ones.
PARSER_VERSION = 5


def analyse(text: str) -> dict:
    """Everything this module can count in one answer."""
    text = text or ""
    prose = _unfenced(text)   # structure is read from the prose only
    shape = _shape(prose)
    if shape == "qa":
        counts = {**_tally(prose, QA_TIERS), "low": 0}
    elif shape == "synthesis":
        counts = _tally(prose, SYNTHESIS_TIERS)
    else:
        counts = _freeform(prose)
    verdict_match = _VERDICT.search(prose)
    verdict = verdict_match.group(1).strip().lower() if verdict_match else ""
    citations = _CITATION.findall(text)
    files = set(_FILE.findall(text))
    findings = sum(counts[k] for k in ("blockers", "high", "medium", "low"))
    result = {
        "parser": PARSER_VERSION,
        "shape": shape,
        **counts,
        "findings": findings,
        "tests_proposed": findings if shape == "qa" else 0,
        "verdict": verdict,
        "blocked": int(verdict == "blocked"),
        "chars": len(text),
        "words": len(text.split()),
        "citations": len(citations),
        "files_cited": len(files),
        "verify_hedges": len(_VERIFY.findall(text)),
        "code_blocks": text.count("```") // 2,
    }
    if shape == "synthesis":
        result["raised_by"] = _raised_by(prose)
    return result
