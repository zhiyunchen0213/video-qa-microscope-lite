#!/usr/bin/env python3
"""Lite standalone deterministic prompt linter (3 generic video physics rules).

Open-source Lite edition:
  1. closed_door_ghosting: opening a closed door and walking through without pre-open or cut
  2. silent_quoted_dialogue: silent lip-sync paired with quoted / <d> dialogue (error)
  3. english_speech_rate_high: spoken English exceeds 2.4 words/sec after 0.8s tail margin

Full 6-rule + H3 syntax validator is included in the Pro edition inside the Feishu Knowledge Base.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from sys import float_info

_DIALOGUE = re.compile(
    r'<d\s*>(?P<tag>.*?)</d\s*>|"[^"\n]+"|“[^”\n]+”|「[^」\n]+」|『[^』\n]+』'
    r'|‘[^’\n]+’|(?<!\w)\'[^\n]+?\'(?!\w)',
    re.I | re.S,
)
_BREAK = re.compile(r'[。！？；;\n]+|[.!?]+(?=\s|$)')
_QUOTE_LEAD = re.compile(r"""^\s*(?:(?:台词|dialogue|line)\s*[:：]\s*)?["“「『‘']""", re.I)
_OPEN_DOOR = re.compile(
    r'(?:推|拉|打|拽|踹)(?:开|动)[^。！？\n]{0,30}门|(?:open\w*|push\w*|pull\w*)\b[^.!?\n]{0,55}\bdoor\b',
    re.I,
)
_CLOSED_DOOR = re.compile(
    r'(?:紧闭|关紧|关着|关闭|锁着|上锁|闭合)[^。！？\n]{0,12}门'
    r'|门(?:板)?\s*(?:(?:是|仍然|仍|原本)\s*)?(?:紧闭|关紧|关着|关闭|锁着|上锁|闭合)'
    r'|\b(?:closed|shut|locked)\s+(?:(?:wooden|front|back|glass|metal)\s+)?door\b'
    r'|\bdoor\b[^.!?\n]{0,25}\b(?:closed|shut|locked)\b',
    re.I,
)
_THROUGH_DOOR = re.compile(
    r'走(?:出|进|入|过|出去|进去)|迈(?:步)?(?:过|出|进|入)|穿(?:过|越)|跨(?:过|出|进|入)'
    r'|\b(?:walk\w*|step\w*|pass\w*|go(?:es|ing)?|went|slip\w*|stride\w*)\s+'
    r'(?:(?:right|straight|quickly)\s+)?(?:through|across|out|in(?:to)?)(?![A-Za-z])'
    r'|\b(?:exits?|enters?|cross(?:es|ing)?)\b',
    re.I,
)
_PREOPEN_DOOR = re.compile(
    r'预开|预先(?:开|打开)|半开|已(?:经)?(?:开|打开|敞开)|首帧[^。！？\n]{0,14}(?:开|敞)'
    r'|\b(?:ajar|pre[- ]?open\w*|half[- ]open|already open)\b',
    re.I,
)
_CUT_AWAY = re.compile(
    r'硬切|切(?:至|到|点省略)|下一镜|\b(?:cut(?:s|ting)? (?:to|away)|hard cut|next shot)\b',
    re.I,
)
_SILENT = re.compile(
    r'无声(?:的|地)?(?:口型|对口型|开口|动唇)|只做口型|纯口型|默念|心(?:里|中)(?:嘀咕|念|说|想)'
    r'|\b(?:mouth(?:s|ing|ed)?\s+(?:the words\s+)?silently|silently\s+mouth(?:s|ing|ed)?'
    r'|silent(?:ly)? lip[- ]?sync\w*|(?:murmur\w*|say\w*) (?:to herself|to himself) silently)\b',
    re.I,
)
_WORD = re.compile(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*")


def _action_beats(prompt: str, dialogue: list[re.Match]) -> list[tuple[str, str]]:
    parts, start = [], 0
    for match in dialogue:
        parts.append(prompt[start:match.start()])
        marker = '<d>' if match['tag'] is not None else ''
        parts.append(marker.ljust(match.end() - match.start()))
        start = match.end()
    action = ''.join(parts) + prompt[start:]
    beats, start = [], 0
    for delimiter in _BREAK.finditer(action):
        beats.append((prompt[start:delimiter.start()].strip(), action[start:delimiter.start()].strip()))
        start = delimiter.end()
    beats.append((prompt[start:].strip(), action[start:].strip()))
    return [(source, visual) for source, visual in beats if source]


def lint_video_prompt(prompt: str, *, duration_sec: float | None = None) -> dict:
    report = {'warnings': [], 'errors': []}

    def add(code: str, message: str, evidence, *, severity: str = 'warning'):
        report[severity + 's'].append({
            'code': code,
            'severity': severity,
            'message': message,
            'evidence': evidence[:240] if isinstance(evidence, str) else evidence,
        })

    dialogue = list(_DIALOGUE.finditer(prompt))
    beats = _action_beats(prompt, dialogue)

    for index, (source, action) in enumerate(beats):
        previous_source, previous = beats[index - 1] if index else ('', '')
        if not (_OPEN_DOOR.search(action) and _CLOSED_DOOR.search(previous + ' ' + action)):
            continue
        next_source, next_action = beats[index + 1] if index + 1 < len(beats) else ('', '')
        if _THROUGH_DOOR.search(action):
            next_source, next_action = '', ''
        combined = action + ' ' + next_action
        traverse = _THROUGH_DOOR.search(combined)
        if (traverse and not _PREOPEN_DOOR.search(previous + ' ' + combined)
                and not _CUT_AWAY.search(combined[:traverse.start()])):
            context = '' if _CLOSED_DOOR.search(action) else previous_source
            add(
                'closed_door_ghosting',
                '紧闭的门先拍开缝后硬切，或在首帧预开门再拍跨门槛。',
                f'{context} {source} {next_source}'.strip(),
            )
            break

    for index, (source, action) in enumerate(beats):
        if not _SILENT.search(action):
            continue
        next_source = beats[index + 1][0] if index + 1 < len(beats) else ''
        quoted = any(m['tag'] is None or _DIALOGUE.search(m['tag']) for m in _DIALOGUE.finditer(source))
        if quoted or _QUOTE_LEAD.search(next_source):
            add(
                'silent_quoted_dialogue',
                '静音口型只写嘴唇和眼神变化，移除带引号的具体台词。',
                source if quoted else f'{source}\n{next_source}',
                severity='error',
            )
            break

    if isinstance(duration_sec, (int, float)) and not isinstance(duration_sec, bool) and 0 < duration_sec <= float_info.max:
        words = sum(len(_WORD.findall(m['tag'] if m['tag'] is not None else m[0][1:-1])) for m in dialogue)
        spoken_sec = max(duration_sec - 0.8, 0.5)
        rate = words / spoken_sec
        if rate > 2.4:
            add(
                'english_speech_rate_high',
                f'英文台词约 {words} 词、{rate:.2f} 词/秒，建议控制在 2.4 词/秒内。',
                {'words': words, 'words_per_sec': round(rate, 3), 'available_sec': round(spoken_sec, 3)},
            )

    return report


def lint_video_segments(segments: list[dict]) -> dict:
    result = {'warnings': [], 'errors': []}
    for index, segment in enumerate(segments, 1):
        prompt = segment.get('prompt')
        if not isinstance(prompt, str):
            continue
        report = lint_video_prompt(prompt, duration_sec=segment.get('duration_sec', segment.get('duration')))
        for severity, findings in report.items():
            result[severity].extend({**finding, 'segment': index} for finding in findings)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Lite video prompt physics linter (3 generic rules; full 6-rule + H3 validator in Pro KB)'
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--prompt', help='Inline video prompt string')
    group.add_argument('--file', type=Path, help='Plain text prompt file or JSON list of segment dicts')
    parser.add_argument('--duration', type=float, default=None, help='Segment duration in seconds (for speech rate check)')
    args = parser.parse_args()

    if args.prompt is not None:
        out = lint_video_prompt(args.prompt, duration_sec=args.duration)
    else:
        raw = args.file.read_text(encoding='utf-8')
        if args.file.suffix.lower() == '.json':
            data = json.loads(raw)
            out = lint_video_segments(data if isinstance(data, list) else data.get('segments', []))
        else:
            out = lint_video_prompt(raw, duration_sec=args.duration)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if out['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
