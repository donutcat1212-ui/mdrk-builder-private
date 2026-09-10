from mdrk_builder.domain.formatted_text import emphasis_runs
import re


def add_formatted_text(paragraph, text):
    for value, bold in emphasis_runs(text):
        run = paragraph.add_run(value)
        if bold:
            run.bold = True


def clinical_paragraphs(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    label = re.compile(r"^(Основное заболевание|Сопутствующ(?:ее|ие) заболевани[ея]|"
                       r"Дополнительные сведения(?: о заболевании)?)(:?)\s*(.*)$", re.I)
    index = 0
    while index < len(lines):
        line = lines[index]
        match = label.match(line)
        if match:
            tail = match[3]
            if not tail and index + 1 < len(lines) and not label.match(lines[index + 1]):
                index += 1
                tail = lines[index]
            line = f"**{match[1]}:**" + (f" {tail}" if tail else "")
        yield line
        index += 1
