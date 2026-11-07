# Carbon progression persistence

The generated ACCOUNT_DATA path is now data/carbon/progression.sqlite3.
CarbonProgressionStore retains the same API and still supports explicit JSON
paths for compatibility. Skill/Rep formulas, FESL owner IDs and aliases, viral
awards, and role protections remain unchanged.

On first opening a writable SQLite store, the sibling progression.json is
imported in one transaction if the SQL store is empty. A durable import marker
records its path and SHA-256. Subsequent opens never reimport stale JSON.
The JSON source is retained. Duplicate profile IDs, conflicting account keys,
nonfinite statistics and malformed input abort migration.

The database contains progression accounts keyed by the existing normalized
account name, numeric/text statistics, roles, and committed race results.
It does not create authentication accounts or new persona IDs. Account policy
continues to come from the shared account database.

Each operation reads a consistent SQLite snapshot; writers use BEGIN IMMEDIATE
to avoid lost increments across server/admin processes. Race awards, Skill,
Rep, counters and the race journal commit together. A server-owned race key
makes repeating the same committed race return its original result, including
after reopening the database; different input under that key is rejected.
Coordinator keys are generated for each race tracker, not inferred from
reusable room IDs. This does not restore in-flight races after server restart.

Web readers must open the SQLite store with read_only=True and close it after
leaderboard_snapshot(). This reads the same data without importing JSON or
changing schema. Recorded snapshots omit implicit Skill defaults, preserving
the website's distinction between new profiles and saved race results.

Deployment: back up source, configuration and JSON; run server/web tests; stop
the Carbon writer; migrate and compare every account, role, statistic and text
against the JSON snapshot; restart Carbon and the web reader. Verify profile
IDs, both public boards and FESL replies. Keep the original JSON only as a
rollback snapshot: once SQL receives new races, copying it back would lose
those results. A later rollback requires exporting current SQL values.
