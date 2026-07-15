# Issue Tracker

## Type

Local markdown — issues live as files under `.scratch/` in this repo.

## Layout

```
.scratch/
  <feature-or-area>/
    <YYYY-MM-DD>-<short-slug>.md
```

Each file is a markdown issue with frontmatter:

```yaml
---
status: needs-triage | needs-info | ready-for-agent | ready-for-human | wontfix
created: YYYY-MM-DD
---
```

## Consumer skills

Skills that read/write issues: `triage`, `to-prd`, `qa`, `diagnosing-bugs`.
