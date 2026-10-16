---
type: claude-session
date: {{date}}
session_id: {{native_session_id}}
agent_provider: {{agent_provider}}
agent_session_id: {{native_session_id}}
project: {{project_slug}}
project_path: {{canonical_project_root}}
git_branch: {{git_branch}}
duration_minutes: {{duration_minutes}}
tags:
  - claude/session
  - claude/project/{{project_slug}}
  - claude/auto
status: auto-logged
capture_state: {{capture_state}}
capture_completeness: {{capture_completeness}}
capture_revision: {{capture_revision}}
---

# Session

<!-- Reference template, not a runtime input. Placeholders describe fields the
runtime generates; do not copy placeholder revisions into a real note.
Duration is elapsed minutes and may have a fractional part.
The native writer starts with
empty Summary, Key Decisions and Changes Made sections. It replaces only the
managed capture region and permitted metadata. Keep manual prose outside the
managed region. Summary upgrades add a separate managed summary region and
summary_revision after validating the captured source revision. -->

## Summary

## Key Decisions

## Changes Made

## Capture continuation

<!-- obsidian-brain:capture:start -->
{{scrubbed_owned_dialogue}}
{{safe_partial_status_if_any}}
<!-- obsidian-brain:capture:end -->

<!-- Optional manual sections below are preserved by later capture updates. -->

## Errors Encountered
{{manual_errors_encountered}}

## Open Questions / Next Steps
{{manual_open_questions}}

## Session Metadata
- **Commits:** {{manual_commits}}
- **Files touched:** {{manual_files_touched}}
