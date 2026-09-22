import re

import unicoder

import codebox

RUST_SOURCE = """use std::collections::BTreeMap;

impl Book {
    pub fn depth(&self, levels: usize) -> u64 {
        debug_assert!(levels > 0, "empty depth");

        self.asks.values().take(levels).sum()
    }
}
"""
DIFF_SOURCE = """diff --git a/src/book.rs b/src/book.rs
index 1111111..2222222 100644
--- a/src/book.rs
+++ b/src/book.rs
@@ -12,4 +12,4 @@ impl Book {
     pub fn depth(&self, levels: usize) -> u64 {
-        let total = self.asks.values().take(levels).sum();
-        total
+        self.asks.values().take(levels).sum()
     }
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 # book
+Order book depth.
"""
ROW_NUMBER = re.compile(r"^│ +(\d+)(?:  | [+ ] )")


def slice_units(text, entity):
    encoded = text.encode("utf-16-le")
    return encoded[entity["offset"] * 2:(entity["offset"] + entity["length"]) * 2].decode("utf-16-le")


def entity_texts(messages, kind):
    return [slice_units(text, entity) for text, entities in messages for entity in entities if entity["type"] == kind]


def build_function(name, body_lines, doc=True):
    doc_line = [f"/// Returns the {name} total."] if doc else []
    body = [f"    let {name}_{index} = compute_{name}({index});" for index in range(body_lines)]
    return "\n".join([*doc_line, f"pub fn {name}() -> u64 {{", *body, "    0", "}"])


def test_build_code_box_styles_rust():
    box = codebox.build_code_box(RUST_SOURCE, "rust", "src/book.rs", start=40)
    rows = codebox.render_part(box, box.rows)

    assert rows[0].startswith(f"╭─ {unicoder.sans(unicoder.bold('src/book.rs'))} ─")
    assert rows[-1].startswith("╰─ Rust · lines 40-48 ─")
    assert f"│ 40  {unicoder.sans(unicoder.bold('use'))} std::collections::BTreeMap;" in rows
    assert "│ 45  ┊   ┊" in rows
    joined = "\n".join(rows)
    assert unicoder.sans(unicoder.ital(unicoder.bold("depth"))) + "(&" + unicoder.sans(unicoder.bold("self")) in joined
    assert '"' + unicoder.ital("empty depth") + '"' in joined
    assert "levels: usize) -> u64 {" in joined


def test_build_messages_renders_markdown_as_entities():
    box = codebox.build_code_box(RUST_SOURCE, "rust")
    boxed = "\n".join(codebox.render_part(box, box.rows))
    reply = f"## Review\n\nThe `depth` helper is **fine**:\n\n```rust\n{RUST_SOURCE}```\n```rust\n{RUST_SOURCE}```"

    messages = codebox.build_messages(reply)

    assert entity_texts(messages, "pre") == [boxed, boxed]
    assert entity_texts(messages, "code") == ["depth"]
    assert entity_texts(messages, "bold") == ["Review", "fine"]
    assert messages[0][0].startswith("Review\n\nThe depth helper is fine:\n\n╭─")


def test_plan_parts_cuts_between_functions_before_inside_them():
    short_functions = [build_function(name, 6) for name in ("alpha", "beta", "gamma", "delta")]
    long_body = build_function("omega", 30, doc=False).split("\n")
    long_body.insert(16, "")
    source = "\n\n".join([*short_functions, "\n".join(long_body)])
    box = codebox.build_code_box(source, "rust")
    lines = source.split("\n")

    _, ranges = codebox.plan_parts(box, limit=1500)

    cut_lines = [next(line for line in lines[start:] if line.strip()) for start, _ in ranges[1:]]
    assert len(ranges) > 2
    assert [line for line in cut_lines if not line.startswith("/// Returns")] == ["    let omega_15 = compute_omega(15);"]

    python_lines, _ = codebox.analyze_source('def build():\n    return """\nfirst\n\nsecond\n"""\n', "python")
    costs = codebox.score_breaks(python_lines, codebox.measure_indents([line.text for line in python_lines]))
    assert costs[3] == costs[4] == codebox.UNBREAKABLE_COST


def test_build_diff_boxes_numbers_rows_by_the_new_file():
    boxes = codebox.build_diff_boxes(DIFF_SOURCE)

    assert [box.title for box in boxes] == ["src/book.rs", "README.md"]
    assert [box.summary for box in boxes] == ["+1 -2", "+1 -0"]
    assert [row.number for row in boxes[0].rows[1:]] == [12, None, None, 13, 14]
    assert codebox.render_footer(boxes[0], boxes[0].rows, 1).startswith("╰─ Rust · lines 12-14 · +1 -2 ─")


def test_build_messages_splits_within_the_limit_without_losing_lines():
    limit = 900
    source = "\n\n".join(build_function(f"item_{index}", index % 9) for index in range(40))
    prose = " ".join(["word"] * 400)
    reply = f"{prose}\n\nIntro line.\n```rust src/lib.rs\n{source}\n```\n```diff\n{DIFF_SOURCE}```\ndone"
    lines = source.split("\n")

    messages = codebox.build_messages(reply, limit)

    assert all(0 < codebox.count_units(text) <= limit for text, _ in messages)
    boxes = entity_texts(messages, "pre")
    assert all(box.startswith("╭─") and box.split("\n")[-1].startswith("╰─") for box in boxes)
    code_numbers = [int(match.group(1)) for box in boxes if codebox.style_keyword("src/lib.rs") in box.split("\n")[0] for row in box.split("\n") if (match := ROW_NUMBER.match(row))]
    assert code_numbers == [number for number in range(1, len(lines) + 1) if lines[number - 1].strip()]
    assert all("Intro line.\n╭─" in text for text, _ in messages if "Intro line." in text)
    assert messages[-1][0].endswith("done")
    assert codebox.build_messages("  \n") == [(codebox.EMPTY_TEXT, [])]
