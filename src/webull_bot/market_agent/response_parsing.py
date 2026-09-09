import json
import re


def _number(value, minimum: float, maximum: float, default: float) -> float:
    try:
        return min(max(float(value), minimum), maximum)
    except (TypeError, ValueError):
        return default


def _extract_json_object(content: str) -> str | None:
    depth = 0
    start = None
    in_string = False
    escape = False
    for index, character in enumerate(content):
        if in_string:
            if escape:
                escape = False
            elif character == "\\":
                escape = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    return content[start : index + 1]
    return None


def _salvage_json_objects(text: str, required_key: str) -> list[dict]:
    """Last-resort recovery for a genuinely truncated response (cut
    off mid-string with no balanced top-level object anywhere in it,
    so _extract_json_object can't find anything): scan for every
    individually-balanced {...} object at any nesting depth, parse
    each standalone, and keep the ones carrying required_key (e.g.
    "lever" for a suggested_changes[] entry). A well-formed entry is
    self-contained - it doesn't reference anything outside itself -
    so whatever the model finished writing before the cutoff is
    still valid, parseable data; only the incomplete tail object
    (which never gets a closing brace) is correctly left out.
    """
    found: list[dict] = []
    stack: list[int] = []
    in_string = False
    escape = False
    for index, character in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif character == "\\":
                escape = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            stack.append(index)
        elif character == "}":
            if not stack:
                continue
            start = stack.pop()
            try:
                parsed = json.loads(text[start : index + 1])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and required_key in parsed:
                found.append(parsed)
    return found


def _parse_response(self, content: str) -> dict:
    text = str(content or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        candidate = self._extract_json_object(text)
        if candidate is not None:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                candidate = None
        if candidate is None:
            # Either no balanced top-level object exists at all (a
            # genuine truncation), or one does but is internally
            # malformed for some other reason (a missing comma, a
            # stray token) - the balanced-brace candidate's own
            # json.loads can raise too, and that used to propagate
            # uncaught here, skipping the salvage path entirely for
            # exactly the "balanced braces, broken inside" case.
            salvaged = self._salvage_json_objects(text, "lever")
            if not salvaged:
                raise exc
            self.log.warning(
                "AGENT  | response was truncated/malformed - salvaged "
                "%s complete suggested_changes entr(y/ies) instead of "
                "discarding the whole cycle",
                len(salvaged),
            )
            return {"suggested_changes": salvaged}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_review(self, payload) -> dict:
    if not isinstance(payload, dict):
        payload = {}
    severity = str(payload.get("severity", "none") or "none").strip().lower()
    if severity not in self._VALID_SEVERITIES:
        severity = "none"
    raw_changes = payload.get("suggested_changes", [])
    changes = []
    if isinstance(raw_changes, list):
        for raw in raw_changes:
            if not isinstance(raw, dict):
                continue
            lever = str(raw.get("lever", "") or "").strip().lower()
            if lever not in self._VALID_LEVERS:
                continue
            direction = str(raw.get("direction", "") or "").strip().lower()
            if direction not in self._VALID_DIRECTIONS:
                continue
            changes.append(
                {
                    "lever": lever,
                    "direction": direction,
                    "reasoning": str(raw.get("reasoning", "") or "").strip()[
                        :300
                    ],
                }
            )
    return {
        "assessment": str(payload.get("assessment", "") or "").strip()[:400],
        "severity": severity,
        "confidence": self._number(payload.get("confidence"), 0, 1, 0),
        "suggested_changes": changes[:10],
    }
