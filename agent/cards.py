"""Adaptive Cards for Teams.

Posted to Workflows webhooks, which want a message with Adaptive Card attachments.
Schema 1.2 works on every Teams client. A long explanation is split over several cards so
each one stays under Teams' ~28 KB limit.
"""
import json
import re

import boundaries
import timefmt

MAX_CARD_BYTES = 24000
PART_CHARS = 8000          # text of one answer card
MAX_PARTS = 4              # cards per answer, the rest is cut (and says so)

STYLE = {"warning": ("warning", "Warning"), "good": ("good", "Good")}


def _t(text, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _text(text, limit=1500, **props) -> dict:
    return {"type": "TextBlock", "text": _t(text, limit), "wrap": True, **props}


def _facts(pairs) -> dict:
    return {"type": "FactSet", "facts": [{"title": k, "value": _t(v, 300)} for k, v in pairs if v]}


def card(body: list) -> dict:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.2",
        "body": body,
        "msteams": {"width": "Full"},
    }


def message(adaptive_card: dict) -> dict:
    """Wrap a card in the envelope Workflows expects. Shrinks it if it's too big."""
    payload = {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": adaptive_card}]}
    if len(json.dumps(payload)) > MAX_CARD_BYTES:
        # drop blocks from the end until it fits, and say so
        body = adaptive_card["body"]
        while len(body) > 1 and len(json.dumps(payload)) > MAX_CARD_BYTES - 200:
            body.pop()
        body.append(_text("(Details were too long for a Teams message and were cut.)", isSubtle=True))
    return payload


def _header(title: str, style: str | None = None, subtitle: str | None = None) -> dict:
    container, color = STYLE.get(style or "", ("emphasis", "Default"))
    items = [_text(title, 300, weight="Bolder", size="Medium", color=color)]
    if subtitle:
        items.append(_text(subtitle, 300, isSubtle=True, spacing="None"))
    return {"type": "Container", "style": container, "items": items}


def restricted_card(event: dict) -> dict:
    """Card for the admins: who asked about PQ itself, what, and why it was refused.
    event comes from boundaries.record."""
    question = (event.get("question") or "(empty)").replace("](", "] (")  # break markdown links
    body = [
        _header("Restricted question refused", "warning",
                "Someone asked PQ about itself and was told they are not authorised"),
        _facts([
            ("Asked by", event.get("user")),
            ("User ID", event.get("user_id")),
            ("When", timefmt.fmt(event.get("time"))),
            ("Why", event.get("reason")),
            ("Stopped by", event.get("stopped_by")),
        ]),
        _text("Question", weight="Bolder", spacing="Medium"),
        _text(question, 700),
    ]
    if event.get("last_card_this_hour"):
        body.append(_text(f"That was card {boundaries.MAX_CARDS_PER_HOUR} within an hour. Further refusals in this hour "
                          "are only logged and kept in the list (GET /security/restricted).", 300, isSubtle=True))
    return card(body)


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)[\s#]*$")
_OFFER = re.compile(r"^(if you(?:'d| would)? (?:want|like|need)|let me know|would you like|i can also|feel free)", re.I)


def tidy_answer(text: str) -> str:
    """Clean up an AI answer for the card: headings become bold lines (Teams draws "# x" as a
    huge title), a trailing "If you want ..." offer is dropped and blank lines are collapsed.
    Tables and code blocks are left alone."""
    lines, in_code = [], False
    for line in (text or "").strip().splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        heading = None if in_code else _HEADING.match(line)
        if heading:
            line = f"**{heading.group(1).strip().strip('*').strip()}**"
        if in_code or line.strip() or (lines and lines[-1].strip()):  # max one blank line in a row
            lines.append(line.rstrip())
    paragraphs = "\n".join(lines).strip().split("\n\n")
    last = paragraphs[-1].strip()
    if len(paragraphs) > 1 and _OFFER.match(last) and "|" not in last and "```" not in last and len(last.split()) <= 40:
        paragraphs.pop()
    return "\n\n".join(paragraphs).strip()


def _blocks(text: str) -> list[tuple[str, bool]]:
    """(text, is_code) pieces: paragraphs, and code blocks that stay in one piece."""
    blocks, current, in_code = [], [], False

    def close(is_code=False):
        if current:
            blocks.append(("\n".join(current), is_code))
            current.clear()

    for line in text.splitlines():
        fence = line.strip().startswith("```")
        if fence and not in_code:
            close()
            current.append(line)
            in_code = True
        elif fence:
            current.append(line)
            close(is_code=True)
            in_code = False
        elif in_code or line.strip():
            current.append(line)
        else:
            close()
    close(is_code=in_code)
    return blocks


def _pieces(block: str, is_code: bool, limit: int) -> list[str]:
    """One block that is too big for a card on its own, cut at line ends. A code block is
    closed at each cut and opened again in the next piece."""
    lines = block.splitlines()
    opening = lines[0] if is_code else ""
    if is_code:
        lines = lines[1:-1] if lines[-1].strip().startswith("```") and len(lines) > 1 else lines[1:]
    room = max(limit - len(opening) - 8, 200)
    pieces, current, size = [], [], 0
    for line in lines:
        for start in range(0, max(len(line), 1), room):  # a single huge line is cut too
            part = line[start:start + room]
            if current and size + len(part) + 1 > room:
                pieces.append(current)
                current, size = [], 0
            current.append(part)
            size += len(part) + 1
    if current:
        pieces.append(current)
    return ["\n".join([opening, *p, "```"] if is_code else p) for p in pieces]


def split_answer(text: str, limit: int = PART_CHARS) -> list[str]:
    """Split a long answer into parts of at most `limit` characters, between paragraphs.
    A short answer comes back as one part."""
    if len(text) <= limit:
        return [text]
    parts, current, size = [], [], 0
    for block, is_code in _blocks(text):
        for piece in ([block] if len(block) <= limit else _pieces(block, is_code, limit)):
            if current and size + len(piece) + 2 > limit:
                parts.append("\n\n".join(current))
                current, size = [], 0
            current.append(piece)
            size += len(piece) + 2
    if current:
        parts.append("\n\n".join(current))
    return parts


def answer_cards(question: str, asked_by: str | None, answer: str) -> list[dict]:
    """The cards for one answer, in the order to post them. One card unless the answer is long."""
    parts = split_answer(tidy_answer(answer) or "(no answer)")
    cut = len(parts) > MAX_PARTS
    parts = parts[:MAX_PARTS]
    out = []
    for i, part in enumerate(parts, start=1):
        if i == 1:
            subtitle = f"Asked by {asked_by or 'someone'}" + (f" - part 1 of {len(parts)}" if len(parts) > 1 else "")
            body = [_header("PQ", None, subtitle), _text(f"> {question}", 400, isSubtle=True)]
        else:
            body = [_text(f"PQ - part {i} of {len(parts)}", 100, isSubtle=True, weight="Bolder")]
        body.append(_text(part, PART_CHARS + 100))
        if cut and i == len(parts):
            body.append(_text("(The answer was longer than Teams allows and was cut here. Ask for the rest with "
                              "`continue`.)", isSubtle=True))
        out.append(card(body))
    return out


def answer_card(question: str, asked_by: str | None, answer: str) -> dict:
    """A single card, for short texts such as an error or a refusal."""
    return answer_cards(question, asked_by, answer)[0]


def test_card() -> dict:
    return card([
        _header("Test message from PQ", "good", "If you can read this, PQ can post answers here."),
        _text("Mention PQ in the questions channel with any technical question to get a full explanation."),
    ])
