"""Tiny shared text helpers with no app dependencies (importable from any
module without cycles — routines/signals must never import worker or main at
module level)."""


def single_line(value, cap: int = 300) -> str:
    """Collapse untrusted free text to ONE bounded line (every whitespace run
    — newlines included — becomes a single space). Untrusted values rendered
    inside engine-voiced markdown (prompt headers, inbox bodies) could smuggle
    headings/instructions via newlines, so they are forced single-line and
    capped at the write site."""
    return " ".join(str(value or "").split())[:cap].strip()
