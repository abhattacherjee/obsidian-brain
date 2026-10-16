"""Require one explicit PR impact choice and reviewable supporting evidence."""
import argparse
from pathlib import Path
import re
import sys

LABELS = (
    'Shared behavior affects Claude Code and Codex',
    'Host-specific behavior; name the host and capability',
    'No Codex impact; explain why',
)


def _visible_markdown(body):
    visible, fence = [], None
    for line in body.splitlines():
        match = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if fence is not None:
            if (match and match.group(1)[0] == fence[0]
                    and len(match.group(1)) >= fence[1]
                    and not match.group(2).strip()):
                fence = None
            continue
        if match:
            visible.append('')
            fence = (match.group(1)[0], len(match.group(1)))
            continue
        visible.append(line)
    body = '\n'.join(visible)
    visible, offset = [], 0
    while offset < len(body):
        if body[offset] == '`':
            marker = re.match(r'`+', body[offset:]).group()
            suffix = body[offset + len(marker):]
            paragraph_end = re.search(r'\n[ \t]*\n', suffix)
            paragraph = suffix[:paragraph_end.start()] if paragraph_end else suffix
            end = re.search(r'(?<!`)'+ re.escape(marker) + r'(?!`)', paragraph)
            if end:
                stop = offset + len(marker) + end.end()
                visible.append(body[offset:stop].replace('\n', ' '))
                offset = stop
                continue
        if body.startswith('<!--', offset):
            end = body.find('-->', offset + 4)
            # An unfinished HTML comment hides the rest of the body.
            offset = len(body) if end < 0 else end + 3
            continue
        visible.append(body[offset])
        offset += 1
    return ''.join(visible)


def validate(body):
    body = _visible_markdown(body)
    marked = [label for label in LABELS
              if re.search(r'^- \[[xX]\] ' + re.escape(label) + r'\s*$', body, re.M)]
    if len(marked) != 1:
        raise ValueError('Choose exactly one Codex-impact entry.')
    evidence = re.search(r'^Impact evidence:[ \t]*(.*)$', body, re.M)
    text = (evidence.group(1) if evidence else '').strip()
    if not text:
        raise ValueError('Impact evidence must identify affected contracts or explain no impact.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--body-file', required=True, type=Path)
    args = parser.parse_args()
    if args.body_file.stat().st_size > 256_000:
        parser.error('PR body exceeds the review limit.')
    try:
        validate(args.body_file.read_text())
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
