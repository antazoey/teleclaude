"""Markdown replies as Telegram messages, with fenced code drawn as Unicode-styled editor boxes."""

import math
import re
from dataclasses import dataclass, field

import unicoder
from pygments.lexers import get_lexer_by_name, get_lexer_for_filename
from pygments.token import Comment, Keyword, Name, Number, Punctuation, String
from pygments.util import ClassNotFound

MAX_MESSAGE_UNITS = 3800
MAX_INTRO_UNITS = 600
MAX_RULE_LEN = 64
INDENT_WIDTH = 4
EMPTY_TEXT = "(no output)"
GUIDE = "┊"
WRAP_MARKER = "↪"
HUNK_MARKER = "⋯"
DIFF_LANGUAGES = ("diff", "patch")
OPENERS = "([{"
CLOSERS = ")]}"
FENCE_PATTERN = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^`]*)$")
LOCATION_PATTERN = re.compile(r"^(.*?)(?::|#L)(\d+)(?:-L?\d+)?$")
HUNK_PATTERN = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
ITEM_PATTERN = re.compile(
    r"^(?:(?:pub(?:\([^)]*\))?|export|default|async|unsafe|static|abstract|public|private|protected|override|extern(?:\s+\"[^\"]*\")?)\s+)*"
    r"(?:fn|def|class|impl|struct|enum|trait|mod|union|function|func|fun|interface|type|macro_rules!|const\s+fn)\b"
)
INLINE_PATTERN = re.compile(r"(`+)(.+?)\1|\*\*(.+?)\*\*")
HEADING_PATTERN = re.compile(r"^#{1,6}\s+(.+)$")
MESSAGE_COST = 10
SPLIT_COST = 6
UNDERFILL_COST = 2
ATTACHED_COST = 50
UNBREAKABLE_COST = 1000


@dataclass
class SourceLine:
    text: str
    segments: list = field(default_factory=list)
    depth: int = 0
    inside_token: bool = False
    leading: bool = False


@dataclass
class Row:
    text: str
    number: int | None = None
    blank: bool = False
    cost: float = 0

    @property
    def units(self):
        return count_units(self.text)


@dataclass
class Box:
    title: str
    label: str | None
    rows: list
    summary: str | None = None
    rule_len: int = MAX_RULE_LEN


@dataclass
class Hunk:
    new_start: int
    context: str
    lines: list = field(default_factory=list)


@dataclass
class DiffFile:
    path: str | None
    hunks: list = field(default_factory=list)


def count_units(text):
    return len(text.encode("utf-16-le")) // 2


def style_keyword(text):
    return unicoder.sans(unicoder.bold(text))


def style_function(text):
    return unicoder.sans(unicoder.ital(unicoder.bold(text)))


def style_string(text):
    return unicoder.ital(text)


def style_ghost(text):
    return unicoder.sans(unicoder.ital(text))


def style_comment(text):
    marker_len = len(text) - len(text.lstrip("/#-;* !"))
    return text[:marker_len] + style_ghost(text[marker_len:])


def pick_style(token):
    if token in Keyword.Type:
        return None

    if token in Keyword or token in Name.Builtin.Pseudo:
        return style_keyword

    if token in Name.Function or token in Name.Decorator:
        return style_function

    if token in String.Doc or token in Comment:
        return style_comment

    if token in String.Interpol or token in String.Escape:
        return None

    if token in String:
        return style_string

    if token in Number:
        return unicoder.doubled

    return None


def find_lexer(language, path):
    try:
        return get_lexer_by_name(language, stripnl=False)
    except ClassNotFound:
        pass

    try:
        return get_lexer_for_filename(path or language, stripnl=False)
    except ClassNotFound:
        return None


def analyze_source(code, language="", path=None):
    """Lines with highlight segments and the structure that cut costs are scored from."""
    raw_lines = code.split("\n")
    lexer = find_lexer(language, path)
    if lexer is None:
        return [SourceLine(line, [(None, line)]) for line in raw_lines], None

    lines = [SourceLine("")]
    newline_tokens = [None]
    depth = 0
    for token, value in lexer.get_tokens(code):
        style = pick_style(token)
        spans_lines = token in String or token in Comment
        for index, chunk in enumerate(value.split("\n")):
            if index:
                if len(lines) > 1 and not lines[-1].segments:
                    lines[-1].inside_token = spans_lines and token == newline_tokens[-1]

                lines.append(SourceLine("", depth=depth))
                newline_tokens.append(token)

            if not chunk:
                continue

            current = lines[-1]
            if not current.segments:
                current.inside_token = spans_lines and token == newline_tokens[-1]

            if not any(text.strip() for _, text in current.segments) and chunk.strip():
                current.leading = token in Comment or token in String.Doc or token in Name.Decorator

            current.segments.append((style, chunk))

        if token in Punctuation:
            depth = max(0, depth + sum(value.count(char) for char in OPENERS) - sum(value.count(char) for char in CLOSERS))

    for line, raw_line in zip(lines, raw_lines):
        line.text = raw_line

    return lines[: len(raw_lines)], lexer.name


def measure_indents(texts):
    indents = [len(text) - len(text.lstrip(" ")) if text.strip() else None for text in texts]
    for index, indent in enumerate(indents):
        if indent is None:
            prev_indent = next((val for val in reversed(indents[:index]) if val is not None), 0)
            next_indent = next((val for val in indents[index + 1:] if val is not None), 0)
            indents[index] = min(prev_indent, next_indent)

    return indents


def mark_item_starts(lines):
    """Whether each line begins a definition, counting the comments and attributes stacked on it."""
    starts = [bool(ITEM_PATTERN.match(line.text.strip())) for line in lines]
    for index in range(len(lines) - 2, -1, -1):
        if lines[index].leading and lines[index + 1].text.strip() and starts[index + 1]:
            starts[index] = True

    return starts


def score_breaks(lines, indents):
    """The cost of starting a new part at each line: cheap between definitions, dear inside a body."""
    blank = [not line.text.strip() for line in lines]
    item_starts = mark_item_starts(lines)
    next_content = [None] * len(lines)
    upcoming = None
    for index in range(len(lines) - 1, -1, -1):
        upcoming = upcoming if blank[index] else index
        next_content[index] = upcoming

    costs = [0.0] * len(lines)
    prev_content = None
    for index in range(1, len(lines)):
        if not blank[index - 1]:
            prev_content = index - 1

        target = next_content[index]
        if lines[index].inside_token or target is None:
            costs[index] = UNBREAKABLE_COST if lines[index].inside_token else 0
            continue

        at_blank = blank[index] or blank[index - 1]
        level = max(lines[target].depth, indents[target] // INDENT_WIDTH)
        if item_starts[target]:
            cost = 2 * level + (0 if at_blank else 2)
        elif level == 0:
            cost = 1 if at_blank else 3
        elif at_blank:
            cost = 30 + 10 * level
        else:
            cost = 60 + 20 * level

        if prev_content is not None and lines[prev_content].leading and item_starts[prev_content]:
            cost += ATTACHED_COST

        costs[index] = cost

    return costs


def render_code(segments, indent):
    text_len = 0
    out = []
    for style, chunk in segments or [(None, " " * indent)]:
        if text_len < indent:
            lead = min(indent - text_len, len(chunk))
            out.extend(" " if (text_len + offset) % INDENT_WIDTH else GUIDE for offset in range(lead))
            text_len += lead
            chunk = chunk[lead:]

        out.append(style(chunk) if style and chunk else chunk)
        text_len += len(chunk)

    return "".join(out)


def build_code_box(source, language="", path=None, start=1):
    lines, lexer_name = analyze_source(source.expandtabs(INDENT_WIDTH).rstrip("\n"), language, path)
    indents = measure_indents([line.text for line in lines])
    costs = score_breaks(lines, indents)
    width = len(str(start + len(lines) - 1))
    rows = [
        Row(f"│ {number:>{width}}  {render_code(line.segments, indent)}".rstrip(), number, not line.text.strip(), cost)
        for number, line, indent, cost in zip(range(start, start + len(lines)), lines, indents, costs)
    ]
    rule_len = min(max(len(line.text) for line in lines) + width + 6, MAX_RULE_LEN)
    return Box(path or lexer_name or language or "code", lexer_name if path else None, rows, None, rule_len)


def parse_diff(source):
    files = []
    old_left = new_left = 0
    old_path = None
    for line in source.split("\n"):
        if old_left > 0 or new_left > 0:
            marker = line[:1] or " "
            if marker == "\\":
                continue

            if marker in "+- ":
                files[-1].hunks[-1].lines.append((marker, line[1:]))
                old_left -= marker != "+"
                new_left -= marker != "-"
                continue

            old_left = new_left = 0

        hunk_match = HUNK_PATTERN.match(line)
        if line.startswith("diff --git "):
            files.append(DiffFile(line.rsplit(" b/", 1)[-1] if " b/" in line else None))
        elif line.startswith("--- "):
            old_path = line[4:].split("\t")[0].strip().removeprefix("a/")
        elif line.startswith("+++ "):
            new_path = line[4:].split("\t")[0].strip().removeprefix("b/")
            path = old_path if new_path == "/dev/null" else new_path
            if not files or files[-1].hunks:
                files.append(DiffFile(path))
            elif path:
                files[-1].path = path
        elif hunk_match:
            if not files:
                files.append(DiffFile(None))

            old_len, new_start, new_len, context = hunk_match.groups()
            files[-1].hunks.append(Hunk(int(new_start), context.strip()))
            old_left = int(old_len) if old_len is not None else 1
            new_left = int(new_len) if new_len is not None else 1

    return [diff_file for diff_file in files if diff_file.hunks]


def build_diff_rows(hunk, path, width):
    """One hunk's rows, each side highlighted as its own file so tokens stay in context."""
    new_lines, lexer_name = analyze_source(
        "\n".join(text.expandtabs(INDENT_WIDTH) for marker, text in hunk.lines if marker != "-"), "", path
    )
    old_lines, _ = analyze_source(
        "\n".join(text.expandtabs(INDENT_WIDTH) for marker, text in hunk.lines if marker != "+"), "", path
    )
    new_iter = iter(new_lines)
    old_iter = iter(old_lines)
    display = []
    for marker, _ in hunk.lines:
        if marker == " ":
            display.append((marker, next(new_iter)))
            next(old_iter)
        else:
            display.append((marker, next(new_iter) if marker == "+" else next(old_iter)))

    lines = [line for _, line in display]
    indents = measure_indents([line.text for line in lines])
    costs = score_breaks(lines, indents)
    rows = [Row(f"│ {HUNK_MARKER:>{width}}  {style_ghost(hunk.context)}".rstrip())]
    number = hunk.new_start
    for (marker, line), indent, cost in zip(display, indents, costs):
        shown = number if marker != "-" else None
        label = f"{shown:>{width}}" if shown is not None else " " * width
        rows.append(Row(f"│ {label} {marker} {render_code(line.segments, indent)}".rstrip(), shown, False, cost))
        number += marker != "-"

    if len(rows) > 1:
        rows[1].cost = UNBREAKABLE_COST

    return rows, lexer_name


def build_diff_boxes(source, path=None):
    boxes = []
    for diff_file in parse_diff(source):
        file_path = diff_file.path or path
        width = len(str(max(hunk.new_start + len(hunk.lines) for hunk in diff_file.hunks)))
        rows = []
        lexer_name = None
        for hunk in diff_file.hunks:
            hunk_rows, lexer_name = build_diff_rows(hunk, file_path, width)
            rows.extend(hunk_rows)

        markers = [marker for hunk in diff_file.hunks for marker, _ in hunk.lines]
        rule_len = min(max(len(text) for hunk in diff_file.hunks for _, text in hunk.lines) + width + 8, MAX_RULE_LEN)
        boxes.append(Box(file_path or "diff", lexer_name, rows, f"+{markers.count('+')} -{markers.count('-')}", rule_len))

    return boxes


def wrap_rows(box, limit):
    """Rows longer than `limit` units cut into continuation rows."""
    wrapped = []
    for row in box.rows:
        if row.units <= limit:
            wrapped.append(row)
            continue

        pieces = cut_by_units(row.text, limit)
        wrapped.append(Row(pieces[0], row.number, row.blank, row.cost))
        wrapped.extend(Row(f"│ {WRAP_MARKER} {piece}", None, False, UNBREAKABLE_COST) for piece in pieces[1:])

    box.rows = wrapped


def render_header(box, part_no, part_count):
    suffix = f" ({part_no}/{part_count})" if part_count > 1 else ""
    return f"╭─ {style_keyword(box.title)}{suffix} " + "─" * max(box.rule_len - len(box.title + suffix) - 4, 3)


def render_footer(box, rows, part_count):
    numbers = [row.number for row in rows if row.number is not None]
    if part_count == 1 and numbers and numbers[0] == 1 and box.summary is None:
        span = f"{len(numbers)} line{'s' if len(numbers) != 1 else ''}"
    elif numbers:
        span = f"lines {numbers[0]}-{numbers[-1]}" if numbers[-1] != numbers[0] else f"line {numbers[0]}"
    else:
        span = None

    footer = " · ".join(filter(None, [box.label, span, box.summary]))
    return f"╰─ {footer} " + "─" * max(box.rule_len - len(footer) - 4, 3)


def render_part(box, rows, part_no=1, part_count=1):
    return [render_header(box, part_no, part_count), "│", *(row.text for row in rows), "│", render_footer(box, rows, part_count)]


def measure_chrome(box):
    widest = [Row("", 10**9), Row("", 10**9)]
    return count_units("\n".join([render_header(box, 99, 99), "│", "", "│", render_footer(box, widest, 99)]))


def plan_parts(box, limit, first_budget=None):
    """Row ranges per part, cut where cheapest; the first may share a message with `first_budget` units left."""
    rows = box.rows
    total = len(rows)
    chrome = measure_chrome(box)
    prefix = [0]
    for row in rows:
        prefix.append(prefix[-1] + row.units + 1)

    def part_units(start, end):
        return prefix[end] - prefix[start] + chrome

    def part_cost(start, end, budget):
        underfill = 0 if end == total else UNDERFILL_COST * (1 - part_units(start, end) / budget)
        return underfill + (rows[end].cost if end < total else 0)

    best = [0.0] * (total + 1)
    choice = [total] * (total + 1)
    for start in range(total - 1, -1, -1):
        best[start] = math.inf
        for end in range(start + 1, total + 1):
            if end > start + 1 and part_units(start, end) > limit:
                break

            cost = MESSAGE_COST + part_cost(start, end, limit) + best[end]
            if cost <= best[start]:
                best[start], choice[start] = cost, end

    first_end = None
    if first_budget is not None and total:
        shared_best = best[0]
        for end in range(1, total + 1):
            if part_units(0, end) > first_budget:
                break

            cost = (SPLIT_COST if end < total else 0) + part_cost(0, end, first_budget) + best[end]
            if cost <= shared_best:
                shared_best, first_end = cost, end

    ranges = []
    start = 0
    while start < total:
        end = first_end if start == 0 and first_end else choice[start]
        ranges.append((start, end))
        start = end

    return first_end is not None, ranges


def trim_blank_rows(rows):
    start = 0
    end = len(rows)
    while end - start > 1 and rows[start].blank:
        start += 1

    while end - start > 1 and rows[end - 1].blank:
        end -= 1

    return rows[start:end]


def cut_by_units(text, limit):
    pieces = []
    current = ""
    current_units = 0
    for char in text:
        char_units = count_units(char)
        if current and current_units + char_units > limit:
            pieces.append(current)
            current = ""
            current_units = 0

        current += char
        current_units += char_units

    pieces.append(current)
    return pieces


def cut_prose(line, limit):
    """An overlong line cut at word boundaries where it can be."""
    pieces = []
    remaining = line
    while count_units(remaining) > limit:
        head = cut_by_units(remaining, limit)[0]
        space = head.rfind(" ")
        cut = space if space > len(head) // 2 else len(head)
        pieces.append(remaining[:cut])
        remaining = remaining[cut:].lstrip(" ")

    pieces.append(remaining)
    return pieces


def render_inline(line):
    """Markdown inline code, bold and headings as entity spans over the plain text."""
    heading = HEADING_PATTERN.match(line)
    if heading:
        text, entities = render_inline(heading.group(1))
        return text, [("bold", 0, count_units(text)), *entities]

    text = ""
    entities = []
    cursor = 0
    for match in INLINE_PATTERN.finditer(line):
        text += line[cursor:match.start()]
        inner = match.group(2) if match.group(1) else match.group(3)
        entities.append(("code" if match.group(1) else "bold", count_units(text), count_units(inner)))
        text += inner
        cursor = match.end()

    return text + line[cursor:], entities


def parse_info(info):
    """A fence info string as (language, path, first line number)."""
    words = info.split()
    if len(words) == 1 and any(char in words[0] for char in "/.:"):
        words = ["", words[0]]

    language = words[0] if words else ""
    location = words[1] if len(words) > 1 else ""
    location_match = LOCATION_PATTERN.match(location)
    if location_match:
        return language, location_match.group(1), int(location_match.group(2))

    return language, location or None, 1


def parse_reply(text):
    """The reply as ("prose", line) and ("code", info, source) segments."""
    segments = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        opener = FENCE_PATTERN.match(lines[index])
        if not opener:
            segments.append(("prose", lines[index]))
            index += 1
            continue

        indent, fence, info = opener.groups()
        body = []
        index += 1
        while index < len(lines):
            stripped = lines[index].strip()
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                break

            body.append(lines[index].removeprefix(indent))
            index += 1

        segments.append(("code", info.strip(), "\n".join(body)))
        index += 1

    return segments


def build_boxes(info, source):
    language, path, start = parse_info(info)
    if language.lower() in DIFF_LANGUAGES:
        boxes = build_diff_boxes(source, path)
        if boxes:
            return boxes

    return [build_code_box(source, language, path, start)]


def is_pre_line(line):
    return any(kind == "pre" for kind, _, _ in line[1])


def pack_message(lines):
    while lines and not lines[0][0].strip():
        lines = lines[1:]

    while lines and not lines[-1][0].strip():
        lines = lines[:-1]

    text = ""
    entities = []
    prev_pre = False
    for position, line in enumerate(lines):
        if position:
            text += "\n"

        offset = count_units(text)
        text += line[0]
        for kind, start, length in line[1]:
            if kind == "pre" and prev_pre:
                entities[-1]["length"] = count_units(text) - entities[-1]["offset"]
            elif length:
                entities.append({"type": kind, "offset": offset + start, "length": length})

        prev_pre = is_pre_line(line)

    return text, entities


class MessageBuilder:
    """Accumulates lines with their entity spans into messages of at most `limit` UTF-16 units."""

    def __init__(self, limit):
        self.limit = limit
        self.messages = []
        self.lines = []
        self.units = 0

    @property
    def remaining(self):
        return self.limit - self.units - (1 if self.lines else 0)

    def append(self, text, entities):
        self.units += count_units(text) + (1 if self.lines else 0)
        self.lines.append((text, entities))

    def add(self, text, entities):
        if self.lines and count_units(text) > self.remaining:
            self.flush()

        self.append(text, entities)

    def flush(self):
        if self.lines:
            self.messages.append(pack_message(self.lines))

        self.lines = []
        self.units = 0

    def add_prose(self, line):
        text, entities = render_inline(line)
        if count_units(text) <= self.limit:
            self.add(text, entities)
            return

        for piece in cut_prose(line, self.limit):
            self.add(piece, [])

    def take_intro(self):
        """The paragraph right before a box, lifted out so it can travel with the box."""
        end = len(self.lines)
        while end and not self.lines[end - 1][0].strip():
            end -= 1

        start = end
        while start and self.lines[start - 1][0].strip() and not is_pre_line(self.lines[start - 1]):
            start -= 1

        intro = self.lines[start:end]
        if not start or count_units("\n".join(text for text, _ in intro)) > MAX_INTRO_UNITS:
            return []

        self.lines = self.lines[:start]
        self.units = count_units("\n".join(text for text, _ in self.lines))
        return intro

    def add_box(self, box):
        wrap_rows(box, self.limit - measure_chrome(box) - 8)
        if self.lines and is_pre_line(self.lines[-1]):
            self.append("", [])

        shared, ranges = plan_parts(box, self.limit, self.remaining if self.lines else None)
        if self.lines and not shared:
            intro = self.take_intro()
            self.flush()
            for text, entities in intro:
                self.append(text, entities)

            if self.lines:
                shared, ranges = plan_parts(box, self.limit, self.remaining)
                if not shared:
                    self.flush()

        for part_no, (start, end) in enumerate(ranges, start=1):
            if part_no > 1:
                self.flush()

            rows = trim_blank_rows(box.rows[start:end]) if len(ranges) > 1 else box.rows
            for text in render_part(box, rows, part_no, len(ranges)):
                self.append(text, [("pre", 0, count_units(text))])


def build_messages(text, limit=MAX_MESSAGE_UNITS):
    """sendMessage payloads as (text, entities), each within `limit` UTF-16 units."""
    builder = MessageBuilder(limit)
    for segment in parse_reply(text):
        if segment[0] == "prose":
            builder.add_prose(segment[1])
            continue

        for box in build_boxes(segment[1], segment[2]):
            builder.add_box(box)

    builder.flush()
    messages = [message for message in builder.messages if message[0].strip()]
    return messages or [(EMPTY_TEXT, [])]
