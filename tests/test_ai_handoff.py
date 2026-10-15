"""Check the intentionally published, message-only AI handoff snapshot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re


HANDOFF = Path(__file__).resolve().parents[1] / "docs/ai_handoff"


def test_handoff_snapshot_hash_counts_and_readme():
    manifest = json.loads((HANDOFF / "MANIFEST.json").read_text())
    content = (HANDOFF / "CHAT_HISTORY.md").read_bytes()
    entry = manifest["artifacts"]["CHAT_HISTORY.md"]
    assert entry["bytes"] == len(content)
    assert entry["sha256"] == hashlib.sha256(content).hexdigest()
    text = content.decode()
    assert len(re.findall(r"^## \d+\. 用户 ·", text, re.M)) == manifest["user_messages"]
    assert len(re.findall(r"^## \d+\. 助手 ·", text, re.M)) == manifest["assistant_messages"]
    assert manifest["messages"] == manifest["user_messages"] + manifest["assistant_messages"]
    assert manifest["messages"] > 0
    for name in ("README.md", "PROJECT_CONTEXT.md"):
        assert (HANDOFF / name).is_file()


def test_handoff_contains_no_native_or_tool_export_files():
    manifest = json.loads((HANDOFF / "MANIFEST.json").read_text())
    for name in ("tool_logs_included", "native_session_files_included", "credentials_or_config_files_included",
                 "binary_attachments_included", "native_session_import_supported"):
        assert manifest[name] is False
    assert {path.name for path in HANDOFF.iterdir()} == {
        "README.md", "PROJECT_CONTEXT.md", "CHAT_HISTORY.md", "MANIFEST.json"
    }


def test_handoff_has_no_unmasked_common_token_formats():
    text = (HANDOFF / "CHAT_HISTORY.md").read_text()
    patterns = (
        r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b",
        r"\b(?:gh[pousr]_|github_pat_|glpat-)[A-Za-z0-9_-]{16,}\b",
        r"\bhf_[A-Za-z0-9]{16,}\b",
        r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
        r"data:[A-Za-z0-9.+/-]+;base64,[A-Za-z0-9+/=]+",
        r"(?i)https?://[^\s/@]+:[^\s/@]+@",
    )
    for pattern in patterns:
        assert not re.search(pattern, text), "Review required: possible credential/binary data in handoff"
