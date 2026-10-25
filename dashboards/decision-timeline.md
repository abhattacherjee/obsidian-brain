# Decision Timeline

## Active Decisions
```dataview
TABLE date, project
FROM "claude-insights"
WHERE type = "claude-decision" AND status = "active"
SORT date DESC
LIMIT 50
```

## All Decisions (Chronological)
```dataview
TABLE date, project, status
FROM "claude-insights"
WHERE type = "claude-decision" AND (!status OR !startswith(lower(string(status)), "provisional"))
SORT date DESC
LIMIT 50
```

## Superseded Decisions
```dataview
TABLE date, project
FROM "claude-insights"
WHERE type = "claude-decision" AND status = "superseded"
SORT date DESC
LIMIT 50
```

## Decisions by Project
```dataview
TABLE length(rows) AS "Decisions", min(rows.date) AS "First", max(rows.date) AS "Last"
FROM "claude-insights"
WHERE type = "claude-decision" AND (!status OR !startswith(lower(string(status)), "provisional"))
GROUP BY project
SORT length(rows) DESC
```
