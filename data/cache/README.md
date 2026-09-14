# API response caches

Not committed. Regenerable. The point of them is that a re-run does not
re-request.

This project has a documented reason to care: on 2026-09-05 it
re-downloaded 13,517 Space-Track `gp_history` records for data it already
held, against a source whose own guidance is **1 request per object per
lifetime** and which had previously suspended the account. Every cache
here exists so that a survey re-run costs nothing but disk.

| Folder | Holds | TTL | Source rule |
|---|---|---|---|
| `techport/` | One JSON file per TechPort project detail, named `<projectId>.json` | 7 days | api.data.gov publishes a rate limit, not a caching rule. 7 days is chosen because a TechPort record's `lastUpdated` moves on the order of months, so a shorter TTL would spend requests to observe nothing |

**Deleting any of these is always safe.** The next run refetches.
