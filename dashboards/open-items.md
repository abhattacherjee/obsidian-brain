# Open Items — All Projects

Cross-project view of all unchecked `- [ ]` items from session notes' `## Open Questions / Next Steps` sections, scoped to the last 90 days. Items older than 90 days fall off this view — use `/check-items` (unbounded) or `/vault-search` to find them.

## By Project

```dataviewjs
let cutoff = dv.date("today").minus(dv.duration("90 days"));

let pages = dv.pages('"claude-sessions"')
    .where(p => p.type === "claude-session" && p.date && p.date >= cutoff);

let allItems = [];
for (let p of pages) {
    let content = await dv.io.load(p.file.path);
    if (!content) continue;
    let match = content.match(/## Open Questions[^\n]*\n([\s\S]*?)(?=\n## |\n# |$)/);
    if (!match) continue;
    let lines = match[1].split("\n");
    for (let line of lines) {
        let m = line.match(/^- \[ \] (.+)$/);
        if (m) {
            allItems.push({
                project: p.project || "unknown",
                item: m[1],
                date: p.date,
                file: p.file.link
            });
        }
    }
}

let byProject = {};
for (let i of allItems) {
    if (!byProject[i.project]) byProject[i.project] = [];
    byProject[i.project].push(i);
}

let projectNames = Object.keys(byProject).sort();
for (let project of projectNames) {
    let items = byProject[project];
    dv.header(3, project + " (" + items.length + ")");
    dv.list(items.map(i => i.item + " — " + i.file));
}

if (allItems.length === 0) {
    dv.paragraph("No open items in the last 90 days.");
}
```

## Recent (last 7 days)

```dataviewjs
let cutoff = dv.date("today").minus(dv.duration("7 days"));

let pages = dv.pages('"claude-sessions"')
    .where(p => p.type === "claude-session" && p.date && p.date >= cutoff);

let items = [];
for (let p of pages) {
    let content = await dv.io.load(p.file.path);
    if (!content) continue;
    let match = content.match(/## Open Questions[^\n]*\n([\s\S]*?)(?=\n## |\n# |$)/);
    if (!match) continue;
    let lines = match[1].split("\n");
    for (let line of lines) {
        let m = line.match(/^- \[ \] (.+)$/);
        if (m) items.push({ project: p.project || "unknown", item: m[1], file: p.file.link });
    }
}

if (items.length === 0) {
    dv.paragraph("No open items from sessions in the last 7 days.");
} else {
    dv.list(items.map(i => "**[" + i.project + "]** " + i.item + " — " + i.file));
}
```

## Stale (>30 days, still open, within 90-day window)

```dataviewjs
let recentCutoff = dv.date("today").minus(dv.duration("30 days"));
let oldCutoff = dv.date("today").minus(dv.duration("90 days"));

let pages = dv.pages('"claude-sessions"')
    .where(p => p.type === "claude-session" && p.date && p.date < recentCutoff && p.date >= oldCutoff);

let items = [];
for (let p of pages) {
    let content = await dv.io.load(p.file.path);
    if (!content) continue;
    let match = content.match(/## Open Questions[^\n]*\n([\s\S]*?)(?=\n## |\n# |$)/);
    if (!match) continue;
    let lines = match[1].split("\n");
    for (let line of lines) {
        let m = line.match(/^- \[ \] (.+)$/);
        if (m) items.push({ project: p.project || "unknown", item: m[1], date: p.date, file: p.file.link });
    }
}

if (items.length === 0) {
    dv.paragraph("No stale open items.");
} else {
    items.sort((a, b) => a.date - b.date);
    dv.list(items.map(i => "**[" + i.project + "]** " + i.item + " (from " + i.date.toFormat("yyyy-MM-dd") + ") — " + i.file));
}
```

## Stats

```dataviewjs
let cutoff = dv.date("today").minus(dv.duration("90 days"));

let pages = dv.pages('"claude-sessions"')
    .where(p => p.type === "claude-session" && p.date && p.date >= cutoff);

let total = 0;
let byProject = {};
let oldestDate = null;

for (let p of pages) {
    let content = await dv.io.load(p.file.path);
    if (!content) continue;
    let match = content.match(/## Open Questions[^\n]*\n([\s\S]*?)(?=\n## |\n# |$)/);
    if (!match) continue;
    let lines = match[1].split("\n");
    for (let line of lines) {
        if (/^- \[ \] /.test(line)) {
            total++;
            let project = p.project || "unknown";
            byProject[project] = (byProject[project] || 0) + 1;
            if (!oldestDate || p.date < oldestDate) oldestDate = p.date;
        }
    }
}

dv.paragraph("**Total open items (last 90 days):** " + total);
dv.paragraph("**Projects with open items:** " + Object.keys(byProject).length);
if (oldestDate) {
    dv.paragraph("**Oldest open item from:** " + oldestDate.toFormat("yyyy-MM-dd"));
}

if (Object.keys(byProject).length > 0) {
    dv.table(
        ["Project", "Open Items"],
        Object.entries(byProject).sort((a, b) => b[1] - a[1])
    );
}
```
