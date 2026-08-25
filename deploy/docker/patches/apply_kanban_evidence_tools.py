#!/usr/bin/env python3
"""Wire tools/kanban_evidence_tools.py into the Hermes source tree.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes``, after
``apply_kanban_worker_tools.py``. One edit: a registration block inserted
immediately before the first kanban tool registration, where ``registry``,
``tool_error`` and the worker-only gate are all already bound. Recording
evidence is specialist work in exactly the way closing a card is, so the
tools gate with ``_check_kanban_worker_mode`` — the same gate the worker
patch applies to ``kanban_complete`` — and this applier refuses to run
before that gate exists rather than silently registering a front-door-wide
tool surface.

Why the tools exist is documented in the module docstring of
``deploy/docker/patches/kanban_evidence_tools.py``. Usage::

    python3 apply_kanban_evidence_tools.py [HERMES_ROOT]   # default /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patchlib  # noqa: E402

RELATIVE = "tools/kanban_tools.py"

#: The first registration in the file; the block lands just above it.
ANCHOR_TOOL = "kanban_show"

#: The worker-only gate the kanban_worker_tools patch imports into the file.
WORKER_CHECK_FN = "_check_kanban_worker_mode"

REGISTER_BLOCK = (
    "# kube-agents patch: see tools/kanban_evidence_tools.py\n"
    "from tools.kanban_evidence_tools import register as _register_evidence_tools\n"
    "\n"
    f"_register_evidence_tools(registry, {WORKER_CHECK_FN}, tool_error)\n"
    "\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    if WORKER_CHECK_FN not in (root / RELATIVE).read_text(encoding="utf-8"):
        raise SystemExit(
            "kanban_evidence_tools patch: tools/kanban_tools.py does not bind "
            f"{WORKER_CHECK_FN}; run apply_kanban_worker_tools.py first."
        )
    patch = patchlib.Patch(root, RELATIVE, prefix="kanban_evidence_tools")

    site = patch.find_call(
        "registry.register",
        label=f"{ANCHOR_TOOL} registration",
        name=ANCHOR_TOOL,
    )
    site.expect(toolset="kanban", check_fn=patchlib.Ident("_check_kanban_mode"))
    patch.insert(site.start, REGISTER_BLOCK)

    patch.commit("1 registration block")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
