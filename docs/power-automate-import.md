# How to import the flows into Power Automate

> **NOT DONE IN THIS REPO.** None of the steps below have been carried out.
> The flow JSON files were never imported into a tenant, never run, and no
> Microsoft service was called. The files are validated only by the static
> checker in `flows/validate_flows.py`. Expect to adjust details (connector
> operation versions, parameter names) when you do this for real.

## What the files are

`flows/*.json` contain the **workflow definition** (the Logic Apps workflow
definition language that Power Automate cloud flows use) wrapped in a minimal
envelope (`name`, `properties.displayName`, `properties.definition`,
`properties.connectionReferences`). They are *not* a ready-to-import
package: a Power Automate "Import package (legacy)" zip or a Dataverse
solution needs additional manifest files and connection mappings.

## Prerequisites

* A Power Automate environment where you may create flows (ideally a dev environment).
* Licences: the **HTTP** action and the **When an HTTP request is received** trigger are *premium*
  connectors (Power Automate Premium / per-flow licence).
* Connections: Office 365 Outlook, Microsoft Teams, Approvals.
* The approval API must be reachable from the Microsoft cloud over HTTPS. `http://localhost:8000` does not
  work from Power Automate. Options: deploy the FastAPI app to an internal host behind API Management, or
  use an on-premises data gateway with a custom connector.
* Authentication on the API (the mock has none). Add OAuth (Entra ID app registration) or at least an API
  key header before exposing it.

## Option A (recommended): rebuild the flows from the definitions

The definitions are small (3 flows, 5-12 actions each). Rebuilding them in the designer is the most
reliable path and lets the designer pick the current connector operation versions.

1. Create a **solution** in the target environment (all flows and connection references go into it).
2. Create environment variables for `apiBaseUrl`, `approvalFlowUrl`, `apTeamMailbox`, `itOpsMailbox`,
   `escalationDays` (the `parameters` block of each JSON).
3. Create **Flow 2** first (instant cloud flow, trigger *When an HTTP request is received*, schema from the
   JSON). Save it and copy the generated HTTP POST URL into `approvalFlowUrl`.
4. Create **Flow 1** (same trigger type) and **Flow 3** (scheduled cloud flow, daily 07:00,
   W. Europe Standard Time). Add the actions in the order of their `runAfter` entries; action names in the
   designer must match the JSON names because expressions such as `body('Create_invoice')` refer to them.
5. Configure *Configure run after* for the failure handlers exactly as in the JSON
   (`Intake_failed`, `Classification_failed`, `Decision_not_recorded`, `Alert_IT_on_failure`
   run after **has failed / has timed out**).
6. Set the Http action timeout of `Classify_with_AI` to 30 s and the retry policy of
   `Escalate_overdue_approvals` to exponential, 3 retries.
7. Compare: export the new flows (*Export → Package (.zip)*), extract `definition.json` and run
   `python -m flows.validate_flows path/to/definition.json --check-api`. The validator accepts the
   exported envelope if `properties.definition` is present.

## Option B (untested): wrap the JSON into a legacy import package

A legacy package is a zip with roughly this layout:

```
manifest.json
Microsoft.Flow/flows/manifest.json
Microsoft.Flow/flows/<flow-guid>/definition.json      <- the JSON from this repo
Microsoft.Flow/flows/<flow-guid>/apisMap.json
Microsoft.Flow/flows/<flow-guid>/connectionsMap.json
```

The easiest way to get correct manifests is to export any small flow from your environment and replace
its `definition.json`. During *Import package (legacy)* you map each connection reference to an existing
connection. This route has not been tried with these files.

## Test checklist after import (in a dev environment)

| # | Case | Expected |
|---|---|---|
| 1 | Post a valid invoice to Flow 1 | invoice `received`, AI classification, approval mail to cost center manager |
| 2 | Post the same invoice again with a reformatted number | status `duplicate`, AP mail, run *Cancelled* |
| 3 | Post an invoice with `gross != net + tax` | status `on_hold`, AP mail |
| 4 | Ambiguous description ("Services as agreed") | Teams message to AP, no approval started |
| 5 | Manager rejects without comment | API stores "Rejected without comment" (flow default) |
| 6 | No decision for 3 days | Flow 3 escalates, finance lead gets mail + new approval |
| 7 | Manager answers after escalation | API returns 403, `Decision_not_recorded` mail |
| 8 | Stop the API, post an invoice | `Intake_failed` mail, run *Failed* |
