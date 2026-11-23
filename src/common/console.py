"""Making the three scripts printable on a Windows console.

This looks like a triviality and is not. The first live run of task 1 returned
a book whose title contains a character the Windows console's default code
page (cp437 here, cp1252 elsewhere) cannot encode. Python's reaction to that
is not a mangled character - it is `UnicodeEncodeError`, raised from `print`,
after the fetch succeeded and the rows were already committed. The data is
fine and the program dies anyway, on the last line, on a machine that is not
mine.

That is the whole failure mode of "it worked when I ran it", so it is worth
four lines to remove: ask for UTF-8 on the way out, and if even that fails,
replace what cannot be encoded rather than crash. A report that prints a
question mark is a report; a report that raises is not.
"""
from __future__ import annotations

import sys


def use_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # A redirected or wrapped stream may not support it; nothing to do.
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Some consoles refuse the switch. `errors="replace"` on its own
            # still turns a crash into a substituted character.
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
