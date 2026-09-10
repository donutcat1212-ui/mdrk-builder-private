"""A small, portable bold-only text representation for editable draft strings."""
import re


def emphasis_runs(text):
    start = 0
    for match in re.finditer(r"\*\*([^\n]+?)\*\*", text):
        if match.start() > start:
            yield text[start:match.start()], False
        yield match[1], True
        start = match.end()
    if start < len(text):
        yield text[start:], False


def emphasize(text):
    return "\n".join(f"**{line}**" if line else "" for line in text.split("\n"))
