"""Retained Android AskUserQuestion and request-card pick wire.

This is conversation input, never a grant to run an edited tool call.
See README.user-questions.md for the original serializer/call-site evidence.
"""
import copy
import json
from dataclasses import dataclass

QUESTION_TOOL = "AskUserQuestion"
WIRE_LIMIT = 16384


@dataclass(frozen=True)
class QuestionAnswer:
    answers: dict


def definition():
    string = {"type": "string", "minLength": 1}
    option = {"type": "object", "properties": {"label": string, "description": {"type": "string"}},
              "required": ["label"], "additionalProperties": False}
    question = {"type": "object", "properties": {"question": string, "header": string,
                "options": {"type": "array", "minItems": 1, "items": option}, "multiSelect": {"type": "boolean"}},
                "required": ["question", "header", "options"], "additionalProperties": False}
    return {"name": QUESTION_TOOL,
            "description": "Ask the user questions with single or multiple choices and an optional custom answer. "
                           "Waits for their native answer, then returns answers keyed by the exact question text. "
                           "Does not authorize tool execution. Keep the total question input under 16 KiB.",
            "parameters": {"type": "object", "properties": {
                "questions": {"type": "array", "minItems": 1, "items": question}},
                "required": ["questions"], "additionalProperties": False}}


def bounded(value):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > WIRE_LIMIT:
        raise ValueError("Question input or answer exceeds 16 KiB")


def text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Question text and answers must be nonempty strings")
    return value


def questions(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"questions"}:
        raise ValueError("AskUserQuestion requires only questions")
    bounded(arguments)
    items = arguments["questions"]
    if not isinstance(items, list) or not items:
        raise ValueError("At least one question is required")
    keys = set()
    for item in items:
        if (not isinstance(item, dict) or set(item) - {"question", "header", "options", "multiSelect"}
                or not {"question", "header", "options"} <= set(item)):
            raise ValueError("Invalid question fields")
        key = text(item["question"])
        text(item["header"])
        if key in keys:
            raise ValueError("Question texts must be distinct")
        keys.add(key)
        if "multiSelect" in item and type(item["multiSelect"]) is not bool:
            raise ValueError("multiSelect must be boolean")
        options = item["options"]
        if not isinstance(options, list) or not options:
            raise ValueError("Question options are required")
        labels = set()
        for option in options:
            if not isinstance(option, dict) or set(option) - {"label", "description"} or "label" not in option:
                raise ValueError("Invalid option fields")
            label = text(option["label"])
            if label in labels:
                raise ValueError("Option labels must be distinct")
            labels.add(label)
            if option.get("description") is not None and not isinstance(option["description"], str):
                raise ValueError("Option description must be text")
    return items


def answer_map(arguments, answers):
    items = questions(arguments)
    if not isinstance(answers, dict) or not answers or not set(answers) <= {item["question"] for item in items}:
        raise ValueError("At least one answer, only to original questions, is required")
    for value in answers.values():
        text(value)
    bounded(answers)
    return copy.deepcopy(answers)


def sdk_answers(arguments, updated):
    if (not isinstance(updated, dict) or set(updated) != {"questions", "answers"}
            or updated["questions"] != arguments.get("questions")):
        raise ValueError("Only answers to the unchanged original questions may be submitted")
    questions({"questions": updated["questions"]})
    return answer_map(arguments, updated["answers"])


def card_questions(arguments):
    return [{"header": item["header"], "question": item["question"],
             "options": [{"index": index, **option} for index, option in enumerate(item["options"])],
             "pick_several": item.get("multiSelect", False), "takes_other": True}
            for item in questions(arguments)]


def pick_answers(arguments, body):
    items = questions(arguments)
    picks = body.get("picks")
    if len(items) > 1:
        if (not isinstance(picks, list) or len(picks) != len(items)
                or any(body.get(key) is not None for key in ("option_index", "option_indexes", "other_text"))):
            raise ValueError("Each question requires its corresponding pick")
    else:
        if picks is not None:
            raise ValueError("Single questions use the top-level pick fields")
        indexes = body.get("option_indexes")
        index = body.get("option_index")
        if index is not None:
            if indexes is not None or items[0].get("multiSelect", False):
                raise ValueError("Conflicting pick fields")
            indexes = [index]
        picks = [{"option_indexes": indexes, "other_text": body.get("other_text")}]
    answers = {}
    for item, pick in zip(items, picks):
        if not isinstance(pick, dict) or set(pick) - {"option_indexes", "other_text"}:
            raise ValueError("Invalid pick")
        indexes = pick.get("option_indexes")
        indexes = [] if indexes is None else indexes
        if (not isinstance(indexes, list) or any(type(index) is not int or index < 0 or index >= len(item["options"])
                                                for index in indexes) or len(set(indexes)) != len(indexes)):
            raise ValueError("Invalid or repeated option index")
        other = pick.get("other_text")
        if other is not None:
            text(other)
        count = len(indexes) + (other is not None)
        if count == 0 and len(items) > 1:
            # v2e.N0 skips one question; rbe.i serializes its empty q330 pick.
            continue
        if count < 1 or (not item.get("multiSelect", False) and count != 1):
            raise ValueError("Invalid number of answers")
        # Android rbe.h sorts selected indexes before appending custom words.
        selected = [item["options"][index]["label"] for index in sorted(indexes)]
        if other is not None:
            selected.append(other)
        if item.get("multiSelect", False):
            # rbe.h's a3e(17) JSON-quotes items containing a separator or quote.
            selected = [json.dumps(value, ensure_ascii=False) if ", " in value or '"' in value else value
                        for value in selected]
        answers[item["question"]] = ", ".join(selected)
    return answer_map(arguments, answers)
