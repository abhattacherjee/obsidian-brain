# Retro Lessons

Lessons that came back across sessions, from the retro notes' `## Process Improvements` items. Each row is one lesson note in `claude-insights/lessons/`, rebuilt by `retro_rollup.py`. Rows are ranked by sessions in the last 90 days (the `lessons_recent_days` setting), then all sessions. Snoozed lessons are hidden until their date.

## 1. Raise priority

An open issue exists, and the lesson kept coming back after it was opened.

```dataview
TABLE WITHOUT ID link(file.link, name) AS "Lesson", recent_sessions AS "Recent", sessions AS "Sessions", sessions_after_tracking AS "Since issue", tracking_ref AS "Issue", issue_milestone AS "Milestone", issue_priority AS "Priority"
FROM "claude-insights/lessons"
WHERE type = "claude-lesson" AND bucket = "raise" AND !dormant AND (!snoozed_until OR date(snoozed_until) < date(today)) AND (!status OR !startswith(lower(string(status)), "provisional"))
SORT recent_sessions DESC, sessions DESC
LIMIT 25
```

## 2. File an issue

No issue, memory entry or rule tracks the lesson, or its tracking is gone.

```dataview
TABLE WITHOUT ID link(file.link, name) AS "Lesson", recent_sessions AS "Recent", sessions AS "Sessions", length(projects) AS "Projects", first_seen AS "First seen", last_seen AS "Last seen", tracking AS "Tracking"
FROM "claude-insights/lessons"
WHERE type = "claude-lesson" AND bucket = "file" AND !dormant AND (!snoozed_until OR date(snoozed_until) < date(today)) AND (!status OR !startswith(lower(string(status)), "provisional"))
SORT recent_sessions DESC, sessions DESC
LIMIT 25
```

## 3. Fix didn't hold

The issue closed, then the lesson came back.

```dataview
TABLE WITHOUT ID link(file.link, name) AS "Lesson", recent_sessions AS "Recent", sessions AS "Sessions", tracking_ref AS "Issue", tracking_since AS "Opened"
FROM "claude-insights/lessons"
WHERE type = "claude-lesson" AND bucket = "didnt_hold" AND !dormant AND (!snoozed_until OR date(snoozed_until) < date(today)) AND (!status OR !startswith(lower(string(status)), "provisional"))
SORT recent_sessions DESC, sessions DESC
LIMIT 25
```

## 4. Rule not followed

A memory entry or a skill or CLAUDE.md rule exists, and the lesson still comes back.

```dataview
TABLE WITHOUT ID link(file.link, name) AS "Lesson", recent_sessions AS "Recent", sessions AS "Sessions", sessions_after_tracking AS "Since rule", tracking_ref AS "Tracked by"
FROM "claude-insights/lessons"
WHERE type = "claude-lesson" AND bucket = "rule_not_followed" AND !dormant AND (!snoozed_until OR date(snoozed_until) < date(today)) AND (!status OR !startswith(lower(string(status)), "provisional"))
SORT recent_sessions DESC, sessions DESC
LIMIT 25
```

## 5. Tracked and quiet

Tracking exists and nothing came back since.

```dataview
TABLE WITHOUT ID link(file.link, name) AS "Lesson", sessions AS "Sessions", tracking AS "Tracking", tracking_ref AS "Tracked by", last_seen AS "Last seen"
FROM "claude-insights/lessons"
WHERE type = "claude-lesson" AND bucket = "quiet" AND !dormant AND (!status OR !startswith(lower(string(status)), "provisional"))
SORT sessions DESC
LIMIT 25
```

## Dormant

No session in the last 90 days. Probably fixed, or no longer relevant.

```dataview
TABLE WITHOUT ID link(file.link, name) AS "Lesson", sessions AS "Sessions", bucket AS "Bucket", last_seen AS "Last seen"
FROM "claude-insights/lessons"
WHERE type = "claude-lesson" AND dormant AND (!status OR !startswith(lower(string(status)), "provisional"))
SORT last_seen DESC
LIMIT 25
```
