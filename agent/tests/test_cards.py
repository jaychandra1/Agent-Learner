import json

import cards


def test_message_envelope_for_workflows():
    msg = cards.message(cards.answer_card("what is DNS?", "Asha", "**DNS turns names into addresses.**"))
    assert msg["type"] == "message"
    att = msg["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    assert att["content"]["type"] == "AdaptiveCard" and att["content"]["version"] == "1.2"
    shown = json.dumps(msg)
    assert "Asked by Asha" in shown and "> what is DNS?" in shown and "DNS turns names" in shown


def test_oversized_card_is_cut(monkeypatch):
    monkeypatch.setattr(cards, "MAX_CARD_BYTES", 1500)
    msg = cards.message(cards.answer_card("q", "Asha", "a" * 5000))
    assert "too long" in json.dumps(msg) and len(json.dumps(msg)) <= 1500


def test_test_card():
    assert "PQ can post answers here" in json.dumps(cards.test_card())


# --- Clean-up for the Teams card ---

def test_headings_become_bold_lines():
    assert cards.tidy_answer("# How it works\ntext") == "**How it works**\ntext"
    assert cards.tidy_answer("## **Common mistakes** ##") == "**Common mistakes**"


def test_closing_offer_dropped():
    assert cards.tidy_answer("**A pod is one or more containers.**\n\nLet me know if you need more.") == \
        "**A pod is one or more containers.**"
    assert cards.tidy_answer("**Use an index.**\n\nIf you want, I can show the query plan.") == "**Use an index.**"
    only = "If you want fewer restarts, raise the memory limit of api to 512Mi."
    assert cards.tidy_answer(only) == only  # the whole answer: never dropped
    keep = "**Use an index.**\n\n**Learn next:** if you want speed, covering indexes."
    assert cards.tidy_answer(keep) == keep


def test_tables_and_code_untouched():
    table = "| Queue | Order |\n|---|---|\n| Kafka | per partition |"
    code = "```python\n# a comment, not a heading\n\n\nprint('hi')\n```"
    assert cards.tidy_answer(f"**Result.**\n\n\n\n{table}\n\n\n{code}\n") == f"**Result.**\n\n{table}\n\n{code}"


# --- Long explanations: several cards ---

def lesson(paragraphs=40, words=60):
    return "\n\n".join(f"**Part {i}.** " + " ".join(["word"] * words) for i in range(paragraphs))


def test_short_answer_is_one_card():
    (card,) = cards.answer_cards("q", "Asha", "**Short.**")
    assert card["body"][0]["items"][1]["text"] == "Asked by Asha" and card["body"][-1]["text"] == "**Short.**"


def test_long_answer_is_split_between_paragraphs():
    text = lesson()
    assert len(text) > cards.PART_CHARS
    parts = cards.split_answer(text)
    assert len(parts) > 1 and all(len(p) <= cards.PART_CHARS for p in parts)
    assert "\n\n".join(parts) == text  # nothing lost, nothing cut inside a paragraph
    answer = cards.answer_cards("teach me DNS", "Asha", text)
    assert len(answer) == len(parts)
    assert f"part 1 of {len(parts)}" in json.dumps(answer[0]) and f"part 2 of {len(parts)}" in json.dumps(answer[1])
    assert "> teach me DNS" not in json.dumps(answer[1])  # the question is only on the first card
    for card in answer:
        assert len(json.dumps(cards.message(card))) < 28_000 and "too long" not in json.dumps(cards.message(card))


def test_code_block_is_never_split_from_its_fence():
    code = "```python\n" + "\n".join(f"print({i})  # line {i}" for i in range(900)) + "\n```"
    parts = cards.split_answer(f"**Example**\n\n{code}\n\n**Remember:** done.", limit=3000)
    assert len(parts) > 3
    for part in parts:
        assert part.count("```") % 2 == 0, "a part with an open code block"
        assert len(part) <= 3000
    assert parts[1].startswith("```python\n") and parts[-1].endswith("**Remember:** done.")
    joined = "\n".join(parts)
    assert all(f"print({i})  # line {i}" in joined for i in (0, 450, 899))


def test_one_huge_line_is_cut_too():
    parts = cards.split_answer("x" * 20_000, limit=3000)
    assert all(len(p) <= 3000 for p in parts) and "".join(parts) == "x" * 20_000


def test_very_long_answer_is_capped_and_says_so():
    answer = cards.answer_cards("q", "Asha", lesson(paragraphs=200))
    assert len(answer) == cards.MAX_PARTS
    assert "was cut here" in json.dumps(answer[-1]) and "was cut here" not in json.dumps(answer[0])
