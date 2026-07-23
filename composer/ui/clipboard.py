"""Copy text to the system clipboard by shelling out to a platform tool.

OSC 52 is deliberately avoided: it doesn't work under VTE-based terminals, so
instead we invoke whatever clipboard CLI is on ``PATH``. Preference order is
Wayland (``wl-copy``), then X11 (``xclip`` / ``xsel``), then macOS (``pbcopy``).
"""

import shutil
import subprocess

# Each entry is the argv used to write stdin to the clipboard; the first whose
# binary is on PATH and exits cleanly wins.
_CANDIDATES: list[list[str]] = [
    ["wl-copy"],
    ["xclip", "-selection", "clipboard"],
    ["xsel", "--clipboard", "--input"],
    ["pbcopy"],
]


def copy_to_clipboard(text: str) -> bool:
    """Best-effort copy of *text* to the system clipboard.

    Returns ``True`` once a clipboard tool accepts the text, ``False`` if none
    is available or all of them fail.
    """
    for cmd in _CANDIDATES:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            subprocess.run(cmd, input=text.encode(), check=True)
            return True
        except Exception:
            continue
    return False
