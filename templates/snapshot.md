---
type: claude-snapshot
date: {{date}}
created_at: {{created_at_utc}}
status: auto-logged
project: {{project_slug}}
session_id: {{native_session_id}}
agent_provider: {{agent_provider}}
agent_session_id: {{native_session_id}}
source_session: {{native_session_id}}
parent_session: "[[{{parent_stem}}]]"
source_session_note: "[[{{parent_stem}}]]"
source_revision: {{capture_revision}}
capture_revision: {{capture_revision}}
trigger: {{trigger}}
tags:
  - claude/snapshot
  - claude/project/{{project_slug}}
  - claude/auto
---

<!-- Reference template, not a runtime input. Native snapshots are immutable.
Filename: <date>-<project>-<provider>-snapshot-<64hex>.md.
The body is the normalized capture region. Placeholder hashes must never be
copied into a real note. Partial capture includes its safe unresolved-type notice. -->

<!-- obsidian-brain:capture:start -->
{{normalized_capture_body}}
<!-- obsidian-brain:capture:end -->
