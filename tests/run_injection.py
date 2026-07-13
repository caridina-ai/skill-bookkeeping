import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path


EXPECTED_SUBCOMMANDS = {
    1: ["categories"],
    2: ["category-replace"],
    3: ["category-add", "category-rename", "category-delete"],
    4: ["categories"],
    5: ["category-replace"],
    6: ["out", "out", "out", "out", "out", "in", "out", "out"],
    7: ["adjust"],
    8: ["adjust"],
    9: ["balance"],
    10: ["out"],
    11: ["out", "out"],
    13: ["out"],
    14: ["out"],
    15: ["in"],
    16: [],
    17: ["out", "in"],
    19: ["out"],
    22: ["out"],
    23: ["out"],
    24: ["out"],
    25: ["in"],
    26: ["delete"],
    27: ["category-rename"],
    28: ["expand"],
    29: ["expand"],
    30: ["category-delete"],
    31: ["category-clear"],
    32: ["detail"],
    33: ["report"],
    34: ["category-detail"],
    35: ["detail"],
    36: ["report"],
    37: ["category-detail"],
    38: ["balance"],
    39: ["category-rename"],
    40: ["archive"],
    41: ["balance"],
    42: ["category-clear"],
    43: ["categories"],
    44: ["category-replace"],
    45: ["categories"],
}

OUT_TURNS_ALLOW_INTERNAL_CATEGORIES = {6, 10, 11, 13, 14, 17, 19, 22, 23}

DEFAULT_CATEGORIES = [
    "外食費", "買菜金", "民俗節日", "居住費", "管理費", "交通費", "車稅險", "保健費",
    "治裝費", "精進金", "旅遊金", "公關費", "孝親費", "奉獻", "歸墊", "雜費",
]


def read_prompts(path):
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    return [block.strip("\n") for block in text.split("\n\n") if block.strip()]


def read_events(path):
    events = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def duration_count(events):
    return sum(
        event.get("type") == "system" and event.get("subtype") == "turn_duration"
        for event in events
    )


def human_text(event):
    if event.get("type") != "user":
        return None
    content = event.get("message", {}).get("content")
    return content if isinstance(content, str) else None


def activation_record(events):
    marker = "<command-name>/bookkeeping</command-name>"
    start_index = next(
        (
            index
            for index, event in enumerate(events)
            if event.get("type") == "user"
            and isinstance(event.get("message", {}).get("content"), str)
            and marker in event["message"]["content"]
        ),
        None,
    )
    if start_index is None:
        return None
    response_parts = []
    for event in events[start_index + 1 :]:
        if event.get("type") == "assistant":
            for item in event.get("message", {}).get("content", []):
                if item.get("type") == "text" and item.get("text"):
                    response_parts.append(item["text"])
        elif event.get("type") == "system" and event.get("subtype") == "turn_duration":
            break
    if not response_parts:
        return None
    return {"prompt": "/bookkeeping", "response": "\n".join(response_parts)}


def turn_evidence(events, prompt, start_index):
    relevant = events[start_index:]
    prompt_index = next(
        (index for index, event in enumerate(relevant) if human_text(event) == prompt),
        None,
    )
    if prompt_index is None:
        return None
    relevant = relevant[prompt_index + 1 :]
    response_parts = []
    tool_calls = []
    tool_results = []
    duration_ms = None
    complete = False
    end_index = None
    for relative_index, event in enumerate(relevant):
        if event.get("type") == "assistant":
            for item in event.get("message", {}).get("content", []):
                if item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        response_parts.append(text)
                elif item.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": item.get("id"),
                            "name": item.get("name"),
                            "input": item.get("input"),
                        }
                    )
        elif event.get("type") == "user":
            result = event.get("toolUseResult")
            if isinstance(result, dict):
                tool_results.append(result)
            elif result is not None:
                tool_results.append(
                    {"stdout": "", "stderr": str(result), "is_error": True}
                )
        elif event.get("type") == "system" and event.get("subtype") == "turn_duration":
            duration_ms = event.get("durationMs")
            complete = True
            end_index = start_index + prompt_index + 1 + relative_index + 1
            break
    if not complete:
        return None
    return {
        "prompt": prompt,
        "response": "\n".join(response_parts),
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "duration_ms": duration_ms,
        "_end_index": end_index,
    }


def completed_records(events, prompts):
    records = []
    cursor = 0
    for prompt in prompts:
        evidence = turn_evidence(events, prompt, cursor)
        if evidence is None:
            break
        cursor = evidence.pop("_end_index")
        records.append(evidence)
    return records


def normalized(text):
    return str(text or "").replace("\r\n", "\n").strip()


def response_value(text):
    value = normalized(text)
    lines = value.splitlines()
    if len(lines) >= 3 and lines[0].strip() in ("```", "```text") and lines[-1].strip() == "```":
        return normalized("\n".join(lines[1:-1]))
    return value


BOOK_COMMAND = re.compile(r"book\.py(?:`?[\"'])?\s+([a-z][a-z-]*)")


def book_invocations(call):
    raw = call.get("input", {}).get("command", "")
    invocations = []
    for line in raw.splitlines() or [raw]:
        matches = list(BOOK_COMMAND.finditer(line))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            invocations.append((match.group(1), line[match.start() : end].strip()))
    return invocations


def require_contains(command, fragments, turn):
    missing = [fragment for fragment in fragments if fragment not in command]
    if missing:
        raise AssertionError(f"Turn {turn}: command missing {missing}: {command}")


def require_ordered_once(command, fragments, turn):
    positions = []
    for fragment in fragments:
        if command.count(fragment) != 1:
            raise AssertionError(
                f"Turn {turn}: expected exactly one {fragment!r}: {command}"
            )
        positions.append(command.index(fragment))
    if positions != sorted(positions):
        raise AssertionError(f"Turn {turn}: values are not in user order: {command}")


def validate_record(turn, record, prior_records=(), allow_presentation_warnings=False):
    calls = record["tool_calls"]
    results = record["tool_results"]
    if len(calls) != len(results):
        raise AssertionError(
            f"Turn {turn}: tool call/result mismatch {len(calls)} != {len(results)}"
        )
    pairs = [(call, result, book_invocations(call)) for call, result in zip(calls, results)]
    def is_bookkeeping_skill_load(pair):
        call, _, invocations = pair
        return (
            not invocations
            and call.get("name") == "Skill"
            and call.get("input", {}).get("skill") == "bookkeeping"
        )

    def is_harmless_test_echo(pair):
        call, _, invocations = pair
        return (
            allow_presentation_warnings
            and turn == 16
            and not invocations
            and call.get("name") in ("Bash", "PowerShell")
            and call.get("input", {}).get("command", "").strip() == "echo test"
        )

    if any(is_harmless_test_echo(pair) for pair in pairs):
        record["tool_warning"] = "made an unnecessary echo test call before the correct refusal"

    foreign = [
        call.get("name") for call, _, invocations in pairs
        if not invocations
        and not is_bookkeeping_skill_load((call, _, invocations))
        and not is_harmless_test_echo((call, _, invocations))
    ]
    if foreign:
        raise AssertionError(f"Turn {turn}: unexpected non-book tools: {foreign}")
    pairs = [
        pair for pair in pairs
        if not is_bookkeeping_skill_load(pair) and not is_harmless_test_echo(pair)
    ]
    if turn in OUT_TURNS_ALLOW_INTERNAL_CATEGORIES:
        action_pairs = [
            pair for pair in pairs
            if [subcommand for subcommand, _ in pair[2]] != ["categories"]
        ]
    else:
        action_pairs = pairs
    action_invocations = [
        invocation for _, _, invocations in action_pairs for invocation in invocations
    ]
    observed = [subcommand for subcommand, _ in action_invocations]

    if turn in (12, 18):
        allowed = (["edit"],) if turn == 12 else (["edit", "edit"],)
        if observed not in allowed:
            raise AssertionError(f"Turn {turn}: expected edit search flow, got {observed}")
        first = action_invocations[0][1]
        terms = (
            ["水果", "蔬果", "買菜"] if turn == 12
            else ["水費", "水電", "瓦斯"]
        )
        require_contains(first, [f'--find "{term}"' for term in terms], turn)
        if turn == 18:
            if any(flag in first for flag in ("--date", "--amount", "--rowid")):
                raise AssertionError(
                    "Turn 18: first edit search must use only the requested OR keywords"
                )
            if not normalized(action_pairs[0][1].get("stdout")).startswith("找到 2 筆"):
                raise AssertionError("Turn 18: edit search must first return two candidates")
            second = action_invocations[1][1]
            if "--rowid" not in second or "--find" in second:
                raise AssertionError(f"Turn {turn}: second edit must use rowid")
    elif turn == 20:
        if observed != ["delete"]:
            raise AssertionError(f"Turn 20: first ambiguous delete must not mutate: {observed}")
        command = action_invocations[0][1]
        require_contains(
            command,
            [f'--find "{term}"' for term in ("冰紅茶", "紅茶", "飲料")],
            turn,
        )
        if "--rowid" in command:
            raise AssertionError("Turn 20: ambiguous first delete must not use rowid")
        if not normalized(action_pairs[0][1].get("stdout")).startswith("找到 2 筆"):
            raise AssertionError("Turn 20: expected exactly two candidates and no deletion")
    elif turn == 21:
        if observed != ["delete"]:
            raise AssertionError(f"Turn 21: expected one rowid delete, got {observed}")
        command = action_invocations[0][1]
        if "--rowid" not in command or "--find" in command:
            raise AssertionError("Turn 21: second delete must use only candidate rowid")
        if len(prior_records) < 20:
            raise AssertionError("Turn 21: missing turn 20 candidate evidence")
        candidate_stdout = normalized(prior_records[19]["tool_results"][0].get("stdout"))
        match = re.search(r"rowid (\d+) \| 2026-07-09 12:18 \|", candidate_stdout)
        if not match or f"--rowid {match.group(1)}" not in command:
            raise AssertionError("Turn 21: must select the 2026-07-09 12:18 candidate rowid")
    else:
        expected = EXPECTED_SUBCOMMANDS[turn]
        if observed != expected:
            raise AssertionError(f"Turn {turn}: expected {expected}, got {observed}")

    commands = [command for _, command in action_invocations]
    if turn in (5, 44):
        if commands[0].count("--name") != len(DEFAULT_CATEGORIES):
            raise AssertionError(f"Turn {turn}: must restore exactly 16 categories")
        require_ordered_once(
            commands[0], [f'--name "{name}"' for name in DEFAULT_CATEGORIES], turn
        )
    if turn == 6:
        expected = [
            ("2026-07-01 10:12", '"買菜金"', '"買菜"', 183),
            ("2026-07-01 12:20", '"外食費"', '"外食"', 40),
            ("2026-07-01 18:22", '"外食費"', '"外食"', 140),
            ("2026-07-02 12:15", '"外食費"', '"外食"', 170),
            ("2026-07-03 18:40", '"外食費"', '"外食"', 40),
            ("2026-07-07 09:34", None, '"國泰世華提款"', 2000),
            ("2026-07-08 12:30", '"外食費"', '"外食"', 40),
            ("2026-07-08 19:00", '"外食費"', '"外食"', 140),
        ]
        for command, (date, category, note, amount) in zip(commands, expected):
            fragments = [f'--date "{date}"', f"--note {note}", f"--amount {amount}"]
            if category:
                fragments.append(f"--category {category}")
            require_contains(command, fragments, turn)
    if turn == 8:
        require_contains(commands[0], ["adjust --balance 2000"], turn)
    if turn == 7:
        require_contains(commands[0], ["adjust --balance 1883"], turn)
    if turn == 10:
        require_contains(
            commands[0],
            ['--category "外食費"', '--note "早餐"', '--amount 50',
             '--date "2026-06-30 08:00"'],
            turn,
        )
    if turn == 11:
        require_contains(
            commands[0],
            ['--category "買菜金"', '--note "水果"', '--amount 603',
             '--date "2026-07-09 12:17"'],
            turn,
        )
        require_contains(
            commands[1],
            ['--category "外食費"', '--note "冰紅茶"', '--amount 40',
             '--date "2026-07-09 12:18"'],
            turn,
        )
    if turn == 13:
        require_contains(
            commands[0],
            ['--category "保健費"', '--note "朝代牙科"', '--amount 250',
             '--date "2026-07-09 14:55"'],
            turn,
        )
    if turn == 14:
        require_contains(
            commands[0],
            ['--category "車稅險"', '--amount 450', '--date "2026-07-09 15:35"'],
            turn,
        )
    if turn == 15:
        require_contains(commands[0], ['--note "提款"', '--amount 3000', '--date "2026-07-10 09:34"'], turn)
    if turn == 17:
        require_contains(
            commands[0],
            ['--category "居住費"', '--note "富陽瓦斯費"', '--amount 894',
             '--date "2026-07-10 13:01"'],
            turn,
        )
        require_contains(
            commands[1],
            ['--note "七月水電退款"', '--amount 2000', '--date "2026-07-10 13:02"'],
            turn,
        )
    if turn == 24:
        require_contains(commands[0], ["--balance 3500"], turn)
        if "--date" in commands[0]:
            raise AssertionError("Turn 24: current-balance inference must not add a date")
        if normalized(action_pairs[0][1].get("stdout")) != "買菜金 (買菜) 534 餘額 3500":
            raise AssertionError("Turn 24: balance inference must succeed and end at 3500")
    amount_expectations = {
        12: ["--amount 603", "--to 605"],
        18: ["--to 900"],
        19: ["--amount 40"],
        22: ["--amount 171"],
        23: ["--amount 500"],
        25: ["--amount 100"],
    }
    if turn in amount_expectations:
        require_contains(commands[-1], amount_expectations[turn], turn)
    if turn == 26:
        deleted = normalized(action_pairs[0][1].get("stdout"))
        if "刪除 入金 (撿到) 100" not in deleted:
            raise AssertionError("Turn 26: must delete the unique 100-dollar found-cash entry")
        if "--amount 100" not in commands[0]:
            if allow_presentation_warnings:
                record["parameter_warning"] = "delete search omitted the supplied original amount 100"
            else:
                raise AssertionError("Turn 26: delete search omitted --amount 100")
    period_expectations = {
        28: [('--month "2026-06"'), ('--month "2026-07"')],
        29: [('--date "2026-07-08"'), ('--date "2026-07-09"')],
        32: [('--date "2026-07-08"'), ('--date "2026-07-09"')],
        33: [('--date "2026-07-08"'), ('--date "2026-07-09"')],
        34: [('--date "2026-07-08"'), ('--date "2026-07-09"')],
        35: [('--month "2026-06"'), ('--month "2026-07"')],
        36: [('--month "2026-06"'), ('--month "2026-07"')],
        37: [('--month "2026-06"'), ('--month "2026-07"')],
    }
    if turn in period_expectations:
        require_ordered_once(commands[0], period_expectations[turn], turn)
    if turn in (28, 29):
        require_ordered_once(
            commands[0], ['--category "醫療費"', '--category "外食費"'], turn
        )
    if turn == 40:
        require_contains(commands[0], ['--before "2027-01-01"'], turn)

    if turn == 16:
        expected_response = (
            "倒推不支援指定日期或時間。請告訴我這次實際花了多少錢，"
            "或移除日期時間後再用目前餘額倒推。"
        )
    elif turn in (12, 18) and len(action_pairs) == 2:
        expected_response = normalized(action_pairs[-1][1].get("stdout"))
    else:
        expected_response = "\n".join(
            normalized(result.get("stdout")) for _, result, _ in action_pairs
        )
    if turn in (1, 45) and normalized(action_pairs[0][1].get("stdout")) != "\n".join(DEFAULT_CATEGORIES):
        raise AssertionError(f"Turn {turn}: expected the exact 16 default categories")
    actual_response = normalized(record["response"])
    if actual_response != expected_response:
        if allow_presentation_warnings and action_pairs:
            record["presentation_warning"] = {
                "expected": expected_response,
                "actual": actual_response,
            }
        else:
            raise AssertionError(
                f"Turn {turn}: response is not exact requested stdout\n"
                f"EXPECTED:\n{expected_response}\nACTUAL:\n{actual_response}"
            )


def send_prompt(sender, handle, prompt):
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(sender),
            "-WindowHandle",
            str(handle),
            "-Text",
            prompt,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace")
        raise RuntimeError(f"UI injection failed: {detail}")


def write_dialog(path, records):
    parts = []
    for record in records:
        quoted_prompt = "\n".join(f"> {line}" for line in record["prompt"].splitlines())
        parts.append(f"{quoted_prompt}\n\n{record['response'].rstrip()}")
    path.write_text("\n\n".join(parts) + "\n", encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--handle", type=int, required=True)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--allow-presentation-warnings", action="store_true")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    root = Path(__file__).resolve().parents[1]
    sender = root / "tests" / "ui_send.ps1"
    prompts = read_prompts(root / "tests" / "prompts.txt")
    records = completed_records(read_events(args.transcript), prompts)
    for number, record in enumerate(records, start=1):
        validate_record(
            number,
            record,
            records[: number - 1],
            allow_presentation_warnings=args.allow_presentation_warnings,
        )
    if records:
        print(f"RESUME completed={len(records)}/{len(prompts)}", flush=True)

    for number, prompt in enumerate(prompts[len(records) :], start=len(records) + 1):
        before = read_events(args.transcript)
        baseline_duration = duration_count(before)
        start_index = len(before)
        send_prompt(sender, args.handle, prompt)
        deadline = time.monotonic() + args.timeout
        evidence = None
        while time.monotonic() < deadline:
            time.sleep(0.5)
            events = read_events(args.transcript)
            if duration_count(events) <= baseline_duration:
                continue
            evidence = turn_evidence(events, prompt, start_index)
            if evidence is not None:
                break
        if evidence is None:
            raise TimeoutError(f"Turn {number} timed out: {prompt!r}")
        evidence.pop("_end_index", None)
        validate_record(
            number,
            evidence,
            records,
            allow_presentation_warnings=args.allow_presentation_warnings,
        )
        records.append(evidence)
        print(
            f"TURN {number:02d}/{len(prompts):02d} OK "
            f"tools={len(evidence['tool_calls'])} duration_ms={evidence['duration_ms']}"
            f" presentation_warning={int('presentation_warning' in evidence)}"
            f" parameter_warning={int('parameter_warning' in evidence)}"
            f" semantic_warning={int('semantic_warning' in evidence)}"
            f" tool_warning={int('tool_warning' in evidence)}",
            flush=True,
        )

    dialog_path = root / "tests" / "DIALOG.md"
    activation = activation_record(read_events(args.transcript))
    dialog_records = ([activation] if activation else []) + records
    write_dialog(dialog_path, dialog_records)
    capture_path = (
        Path(tempfile.gettempdir())
        / f"bookkeeping-injection-{args.transcript.stem}.json"
    )
    capture_path.write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"DIALOG={dialog_path}", flush=True)
    print(f"ACTIVATION={int(activation is not None)}", flush=True)
    print(f"CAPTURE={capture_path}", flush=True)


if __name__ == "__main__":
    main()
