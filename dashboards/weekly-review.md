# This Week in Claude

```dataview
TABLE date, project, type
FROM "claude-sessions" OR "claude-insights"
WHERE date >= date(today) - dur(7 days) AND (!status OR !startswith(lower(string(status)), "provisional"))
SORT date DESC
LIMIT 100
```
