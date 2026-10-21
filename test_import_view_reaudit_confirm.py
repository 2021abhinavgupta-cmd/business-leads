"""Fix wave 2 item 5: re-auditing from the drawer never silently discards unsaved edits.
No JS test runner in this repo, so this reads the component source (same as test_audit_recovery.py)."""
from pathlib import Path

SRC = Path(__file__).parent / "frontend" / "src" / "ImportView.jsx"


def _re_audit_body():
    src = SRC.read_text(encoding="utf-8")
    start = src.index("const reAudit = async (lead) => {")
    return src[start:src.index("const cancelAudit", start)]


def test_re_audit_asks_before_discarding_unsaved_edits():
    body = _re_audit_body()
    assert "inDrawer && dirty && !window.confirm(" in body
    # asked before anything is started or the drawer is closed
    assert body.index("window.confirm(") < body.index("axios.post(")
    assert body.index("window.confirm(") < body.index("closeReview()")
    assert body.index("window.confirm(") < body.index("setAuditStarting(true)")


def test_dirty_covers_both_designed_and_text_edits():
    assert "const dirty = designDirty || textDirty;" in SRC.read_text(encoding="utf-8")
