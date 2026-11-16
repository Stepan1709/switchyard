"""Deterministic checks of an answer. Pure functions: no database, no network.

An answer is either text or a list of tool calls. Tools are never run: for an agent the
decision "which tool, with which arguments" is what is compared, and it can be compared
exactly, without asking a language model.

Equality is deliberately forgiving about form and strict about content:
* text: surrounding and repeated whitespace is ignored; two JSON texts are equal when their
  parsed values are equal (key order and spacing do not matter);
* tool calls: the same tools in the same order with equal arguments (as parsed JSON).
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

TEXT_LIMIT = 20_000  # characters examined by a regex: a model answer can be arbitrarily long
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Any  # parsed JSON when possible, else the raw string


@dataclass(frozen=True)
class Answer:
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.content and self.content.strip()) and not self.tool_calls

    def as_text(self) -> str:
        """What a judge (or a person) should read."""
        parts = []
        if self.content:
            parts.append(self.content)
        for call in self.tool_calls:
            parts.append(f"[tool call] {call.name}({canonical(call.arguments)})")
        return "\n".join(parts) if parts else "(empty answer)"


def _parse_arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            return raw
    return raw


def answer_from_response(response: dict[str, Any] | None) -> Answer:
    """The first choice of a chat completion."""
    if not isinstance(response, dict):
        return Answer()
    choices = response.get("choices")
    message = choices[0].get("message") if isinstance(choices, list) and choices else None
    if not isinstance(message, dict):
        return Answer()
    calls: list[ToolCall] = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") if isinstance(call, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            calls.append(ToolCall(function["name"], _parse_arguments(function.get("arguments"))))
    content = message.get("content")
    return Answer(content if isinstance(content, str) else None, tuple(calls))


def answer_from_expected(expected: dict[str, Any] | None) -> Answer:
    """A labelled example: {"content": "..."} or {"tool_calls": [{"name", "arguments"}]}."""
    if not isinstance(expected, dict):
        return Answer()
    calls = tuple(
        ToolCall(c["name"], _parse_arguments(c.get("arguments", {})))
        for c in expected.get("tool_calls") or []
        if isinstance(c, dict) and isinstance(c.get("name"), str)
    )
    content = expected.get("content")
    return Answer(content if isinstance(content, str) else None, calls)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_text(text: str) -> str:
    return _SPACE_RE.sub(" ", text).strip()


def _json_value(text: str) -> tuple[bool, Any]:
    match = _FENCE_RE.match(text)
    candidate = match.group(1) if match else text
    try:
        return True, json.loads(candidate)
    except ValueError:
        return False, None


def _strip_args(arguments: Any, ignore: frozenset[str]) -> Any:
    if ignore and isinstance(arguments, dict):
        return {k: v for k, v in arguments.items() if k not in ignore}
    return arguments


def tool_calls_equal(
    a: tuple[ToolCall, ...], b: tuple[ToolCall, ...], *, ignore_args: frozenset[str] = frozenset()
) -> bool:
    return len(a) == len(b) and all(
        x.name == y.name
        and canonical(_strip_args(x.arguments, ignore_args))
        == canonical(_strip_args(y.arguments, ignore_args))
        for x, y in zip(a, b, strict=True)
    )


def texts_equal(a: str | None, b: str | None, *, ignore_case: bool = False) -> bool:
    if a is None or b is None:
        return a is None and b is None
    ok_a, value_a = _json_value(a)
    ok_b, value_b = _json_value(b)
    if ok_a and ok_b and isinstance(value_a, dict | list) and isinstance(value_b, dict | list):
        return canonical(value_a) == canonical(value_b)
    left, right = normalize_text(a), normalize_text(b)
    return left.casefold() == right.casefold() if ignore_case else left == right


def answers_equal(a: Answer, b: Answer, *, ignore_args: frozenset[str] = frozenset()) -> bool:
    """The same decision: same tool calls, and the same text if there is any."""
    if a.tool_calls or b.tool_calls:
        return tool_calls_equal(a.tool_calls, b.tool_calls, ignore_args=ignore_args) and (
            texts_equal(a.content, b.content) or not (a.content and b.content)
        )
    return texts_equal(a.content, b.content)


# ---- a small JSON Schema validator -----------------------------------------------------------
# Only a documented subset is supported. Keywords outside it are refused when a check is
# configured, so a schema is never silently half-checked.
_ANNOTATIONS = {"$schema", "title", "description", "default", "examples", "$comment"}
_SUPPORTED = {
    "type", "enum", "const", "properties", "required", "additionalProperties", "items",
    "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum", "pattern",
    "anyOf", "allOf", "oneOf",
}  # fmt: skip
_TYPES = {"string", "number", "integer", "boolean", "array", "object", "null"}


def validate_schema(schema: Any, path: str = "schema") -> None:
    """Raises ValueError if the schema uses something this validator does not understand."""
    if isinstance(schema, bool):
        return
    if not isinstance(schema, dict):
        raise ValueError(f"{path}: must be an object")
    unknown = set(schema) - _SUPPORTED - _ANNOTATIONS
    if unknown:
        raise ValueError(f"{path}: unsupported keyword(s) {sorted(unknown)}")
    types = schema.get("type")
    if types is not None:
        listed = types if isinstance(types, list) else [types]
        if not listed or not all(isinstance(t, str) and t in _TYPES for t in listed):
            raise ValueError(f"{path}.type: must be one of {sorted(_TYPES)}")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError) as exc:
            raise ValueError(f"{path}.pattern: not a valid regular expression") from exc
    for key in ("properties",):
        if key in schema:
            if not isinstance(schema[key], dict):
                raise ValueError(f"{path}.{key}: must be an object")
            for name, sub in schema[key].items():
                validate_schema(sub, f"{path}.{key}.{name}")
    if "required" in schema and not (
        isinstance(schema["required"], list) and all(isinstance(r, str) for r in schema["required"])
    ):
        raise ValueError(f"{path}.required: must be a list of names")
    if "additionalProperties" in schema:
        validate_schema(schema["additionalProperties"], f"{path}.additionalProperties")
    if "items" in schema:
        validate_schema(schema["items"], f"{path}.items")
    for key in ("anyOf", "allOf", "oneOf"):
        if key in schema:
            if not isinstance(schema[key], list) or not schema[key]:
                raise ValueError(f"{path}.{key}: must be a non-empty list")
            for i, sub in enumerate(schema[key]):
                validate_schema(sub, f"{path}.{key}[{i}]")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise ValueError(f"{path}.enum: must be a list")


def _type_ok(value: Any, name: str) -> bool:
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)) or (
            isinstance(value, float) and value.is_integer()
        )
    if name == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if name == "array":
        return isinstance(value, list)
    return isinstance(value, dict)


def schema_errors(value: Any, schema: Any, path: str = "$") -> list[str]:
    """Why `value` does not match `schema` (empty list = it matches)."""
    if schema is True:
        return []
    if schema is False:
        return [f"{path}: no value is allowed here"]
    errors: list[str] = []
    types = schema.get("type")
    if types is not None:
        listed = types if isinstance(types, list) else [types]
        if not any(_type_ok(value, t) for t in listed):
            return [f"{path}: expected {'/'.join(listed)}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: not one of the allowed values")
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: must equal {schema['const']!r}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value[:TEXT_LIMIT]):
            errors.append(f"{path}: does not match the pattern")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += schema_errors(item, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                errors.append(f"{path}: missing '{name}'")
        props = schema.get("properties", {})
        for name, sub in props.items():
            if name in value:
                errors += schema_errors(value[name], sub, f"{path}.{name}")
        extra = schema.get("additionalProperties", True)
        for name in value:
            if name not in props:
                if extra is False:
                    errors.append(f"{path}: unexpected '{name}'")
                elif isinstance(extra, dict):
                    errors += schema_errors(value[name], extra, f"{path}.{name}")
    if "allOf" in schema:
        for sub in schema["allOf"]:
            errors += schema_errors(value, sub, path)
    if "anyOf" in schema and not any(not schema_errors(value, s, path) for s in schema["anyOf"]):
        errors.append(f"{path}: matches none of the alternatives")
    if "oneOf" in schema:
        matching = sum(1 for s in schema["oneOf"] if not schema_errors(value, s, path))
        if matching != 1:
            errors.append(f"{path}: must match exactly one alternative (matches {matching})")
    return errors


# ---- arguments of tool calls -----------------------------------------------------------------
def tool_args_errors(answer: Answer, tools: list[dict[str, Any]] | None) -> list[str]:
    """Is every tool call of the answer a call that the request made possible?

    The tools offered in the request are the contract: a call has to name one of them and its
    arguments have to be an object that fits the tool's parameter schema. This needs no
    reference answer, so it also works where nothing recorded can be compared with.
    """
    offered: dict[str, Any] = {}
    for tool in tools or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            offered[function["name"]] = function.get("parameters")
    errors: list[str] = []
    for index, call in enumerate(answer.tool_calls):
        where = f"call {index + 1} ({call.name})"
        if offered and call.name not in offered:
            errors.append(f"{where}: no such tool in the request")
            continue
        if not isinstance(call.arguments, dict):
            errors.append(f"{where}: arguments are not a JSON object")
            continue
        schema = offered.get(call.name)
        if isinstance(schema, dict):
            try:  # a tool schema written elsewhere may use keywords this validator cannot read
                errors += [f"{where}: {e}" for e in schema_errors(call.arguments, schema)]
            except (re.error, TypeError, KeyError):
                continue
    return errors


# ---- the checks ------------------------------------------------------------------------------
@dataclass
class CheckOutcome:
    kind: str
    passed: bool | None  # None: the check could not be applied (nothing to compare with)
    required: bool = True
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def applied(self) -> bool:
        return self.passed is not None

    def as_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "passed": self.passed, "required": self.required,
                "detail": self.detail}  # fmt: skip


def validate_check_config(kind: str, config: dict[str, Any]) -> dict[str, Any]:
    """Normalise and validate the configuration of a check; raises ValueError."""
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    allowed: dict[str, set[str]] = {
        "json_schema": {"schema"},
        "exact_match": {"ignore_case"},
        "regex": {"pattern", "mode"},
        "tool_call_match": {"ignore_args", "names_only"},
        "tool_args_valid": set(),
        "llm_judge": {"min_score", "rubric"},
    }
    if kind not in allowed:
        raise ValueError(f"unknown check kind '{kind}'")
    extra = set(config) - allowed[kind]
    if extra:
        raise ValueError(f"unknown option(s) for {kind}: {sorted(extra)}")
    if kind == "json_schema":
        if "schema" not in config:
            raise ValueError("json_schema needs a 'schema'")
        validate_schema(config["schema"])
    elif kind == "exact_match":
        if not isinstance(config.get("ignore_case", False), bool):
            raise ValueError("ignore_case must be true or false")
    elif kind == "regex":
        pattern = config.get("pattern")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 500:
            raise ValueError("regex needs a 'pattern' of up to 500 characters")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError("pattern is not a valid regular expression") from exc
        if config.get("mode", "search") not in ("search", "fullmatch"):
            raise ValueError("mode must be 'search' or 'fullmatch'")
    elif kind == "tool_call_match":
        ignore = config.get("ignore_args", [])
        if not (isinstance(ignore, list) and all(isinstance(i, str) for i in ignore)):
            raise ValueError("ignore_args must be a list of argument names")
        if not isinstance(config.get("names_only", False), bool):
            raise ValueError("names_only must be true or false")
    else:  # llm_judge
        score = config.get("min_score", 4)
        if not (isinstance(score, int) and not isinstance(score, bool) and 1 <= score <= 5):
            raise ValueError("min_score must be an integer from 1 to 5")
        rubric = config.get("rubric", "")
        if not isinstance(rubric, str) or len(rubric) > 2000:
            raise ValueError("rubric must be text of up to 2000 characters")
    return config


def run_check(
    kind: str,
    config: dict[str, Any],
    answer: Answer,
    truth: Answer | None,
    *,
    required: bool = True,
    tools: list[dict[str, Any]] | None = None,
) -> CheckOutcome:
    """Apply one deterministic check. `truth` is the expected or the reference answer."""
    if kind == "json_schema":
        text = answer.content
        if not text:
            return CheckOutcome(kind, False, required, "no text to validate")
        ok, value = _json_value(text)
        if not ok:
            return CheckOutcome(kind, False, required, "the answer is not valid JSON")
        errors = schema_errors(value, config["schema"])
        return CheckOutcome(kind, not errors, required, "; ".join(errors[:3]))
    if kind == "regex":
        text = (answer.content or "")[:TEXT_LIMIT]
        matcher = re.fullmatch if config.get("mode", "search") == "fullmatch" else re.search
        found = matcher(config["pattern"], text) is not None
        return CheckOutcome(kind, found, required, "" if found else "no match")
    if kind == "exact_match":
        if truth is None or not (truth.content or truth.tool_calls):
            return CheckOutcome(kind, None, required, "nothing to compare with")
        same = texts_equal(
            answer.content, truth.content, ignore_case=bool(config.get("ignore_case"))
        )
        return CheckOutcome(kind, same, required, "" if same else "differs from the expected text")
    if kind == "tool_args_valid":
        if not answer.tool_calls:
            return CheckOutcome(kind, None, required, "the answer made no tool call")
        errors = tool_args_errors(answer, tools)
        return CheckOutcome(kind, not errors, required, "; ".join(errors[:3]))
    if kind == "tool_call_match":
        if truth is None or not truth.tool_calls:
            return CheckOutcome(kind, None, required, "no expected tool call")
        if config.get("names_only"):
            same = [c.name for c in answer.tool_calls] == [c.name for c in truth.tool_calls]
            return CheckOutcome(kind, same, required, "" if same else "different tools")
        ignore = frozenset(config.get("ignore_args", []))
        if not tool_calls_equal(answer.tool_calls, truth.tool_calls, ignore_args=ignore):
            names_ok = [c.name for c in answer.tool_calls] == [c.name for c in truth.tool_calls]
            why = "right tool, different arguments" if names_ok else "different tool calls"
            return CheckOutcome(kind, False, required, why)
        return CheckOutcome(kind, True, required, "")
    raise ValueError(f"{kind} is not a deterministic check")
