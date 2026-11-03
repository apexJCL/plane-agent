"""Push events to the operator through the user's `notify_command` (user file only, like token_command).

The command runs without a shell (`shlex.split`), gets one JSON object on stdin, and has NOTIFY_TIMEOUT seconds.
A failure only warns on stderr: the tracker write it follows stands, and so does the exit code.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Callable

NOTIFY_TIMEOUT = 10.0
EVENTS = ("needs_human", "deploy_review", "wait_expired")


def notifier(command: str, timeout: float = NOTIFY_TIMEOUT) -> Callable[[dict], None]:
    argv = shlex.split(command)

    def push(event: dict) -> None:
        try:
            res = subprocess.run(argv, input=json.dumps(event), text=True, capture_output=True, timeout=timeout,
                                 check=False)  # fmt: skip
        except (OSError, subprocess.SubprocessError) as e:
            _warn(f"notify_command failed for {event.get('event')} on {event.get('ref')}: {e}")
            return
        if res.returncode != 0:
            tail = (res.stderr or "").strip().splitlines()[-1:] or [""]
            _warn(f"notify_command exited {res.returncode} for {event.get('event')} on {event.get('ref')}"
                  + (f": {tail[0][:200]}" if tail[0] else ""))  # fmt: skip

    return push


def _warn(message: str) -> None:
    print(f"plane-agent: warning: {message}", file=sys.stderr)
