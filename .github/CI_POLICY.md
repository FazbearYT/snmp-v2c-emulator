# CI policy

| Event | Workflow | Checks |
| --- | --- | --- |
| Push to feature/other branch | None | No runner minutes |
| Draft PR into develop | CI / develop PR (skipped jobs) | No runner minutes |
| Ready PR into develop (including new commits) | CI / develop PR | Linux 3.11–3.13 with coverage, Windows, Ruff, config checks |
| Push to develop | CI / develop push | Python 3.13 tests and Ruff |
| Any PR into main (including Draft) | CI / main full | Full existing matrix, packaging and security audits |
| Any push to main | CI / main full | Full existing matrix |
| Manual full test via workflow_dispatch | CI / main full | Full matrix on chosen ref |

Draft PR events produce workflow entries whose jobs are skipped; no runners are
allocated. Ready PR updates cancel superseded runs, and converting back to
Draft cancels an ongoing PR run. Main runs are never automatically cancelled.

Branch protection/rulesets must be configured separately. Skipped jobs are
not evidence that Draft code passed tests; do not approve Draft PRs based on
skipped required checks. Full main checks should be required for main merges.
