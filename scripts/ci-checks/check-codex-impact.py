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


def validate(body):
    marked = [label for label in LABELS
              if re.search(r'^- \[[xX]\] ' + re.escape(label) + r'\s*$', body, re.M)]
    if len(marked) != 1:
        raise ValueError('Choose exactly one Codex-impact entry.')
    evidence = re.search(r'^Impact evidence:\s*(.*)$', body, re.M)
    text = re.sub(r'<!--.*?-->', '', evidence.group(1) if evidence else '').strip()
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
