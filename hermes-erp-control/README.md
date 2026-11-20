# Prabhat Hermes ERP control companion

This companion fills the local file-processing gap in the existing PrabhatJobs + Hermes deployment. The live ERP remains the authority for business data, user roles, delegated tokens and write approvals.

## Routing

| Work | Route |
| --- | --- |
| Candidate, vacancy, interview, tasks, finance, website and messaging actions | Existing PrabhatJobs MCP with the requesting user's delegated identity |
| An action missing from that MCP | Browser session explicitly authorized for that requester, respecting the same UI permissions and approvals |
| PDF split/merge, Excel exports and PDF/DOCX documents | The durable file worker in this directory |
| Poster and video work | Existing media MCP; fixed video functions can also run through the durable worker |
| Recurring business automation | ERP scheduler or an explicitly registered n8n workflow with a suitable scoped grant |

A missing permission is an authorization issue, not a reason to switch to a more privileged browser or use another account's token.

## What this adds

The existing document MCP is extended with five tools: `workbench_capabilities`, `workbench_submit`, `workbench_status`, `workbench_cancel` and `workbench_verify_file`. A systemd worker runs independently of the browser and chat. Jobs use SQLite, opaque random IDs, idempotency keys, leases, bounded retries, cancellation and output hashes.

Operations: `spreadsheet_create`, `document_create`, `pdf_split`, `pdf_merge`, plus the existing `video_edit`, `video_merge`, `images_to_video`, `video_thumbnail` and `extract_audio` functions. Excel text is never treated as a formula. Input files must resolve inside the existing file workspace. PDF output supports the configured Latin font; Hindi/Gujarati content must use DOCX until a renderer with verified Indic shaping is added.

This worker runs fixed file operations only. It does not execute arbitrary commands, send messages, update ERP records, grant permissions or store ERP credentials. It creates no public sharing link. Deliver output through the existing authenticated attachment flow or approved ERP upload tools.

## Install on the existing VPS

Run `sudo python3 deploy.py` from this directory. This creates an isolated virtual environment, installs the requirements, backs up the existing document MCP and relevant Hermes configuration, extends the document MCP, installs the worker service and adds routing guidance to the existing Hermes instruction file. Existing MCP credentials and ERP approvals are preserved.

After testing the new document MCP, reload MCP tools in Hermes or restart the existing gateway during an idle period. A new chat may be needed to see refreshed tools.

The installer writes a rollback manifest under `/opt/prabhat-workbench/backups/`. Restore the listed original files and stop/disable `prabhat-workbench.service` to roll back. Do not remove unrelated files or restore an old Hermes configuration over newer changes without review.

## Example

```json
{"operation":"spreadsheet_create","arguments":{"columns":["S.No","Candidate","Phone"],"rows":[[1,"Test Candidate","09040841969"]]},"idempotency_key":"conversation-request-unique-key"}
```

The return value is a queued job. Poll its `job_id`. Report completion only after `state` is `done`, verify the returned file hash, and deliver the attachment. Record approval-pending ERP work as pending, not done.

## Validation

Run `python -m unittest -v test_workbench.py`. Coverage includes actual PDF split/merge, literal spreadsheet values, path/symlink confinement, durable execution in a child process, idempotency, cancellation, crashed-worker recovery, MCP framing and Unicode-preserving DOCX output. Live installation should additionally verify systemd restart and a synthetic job submitted through the document MCP.

## Remaining ERP integration work

The connected repository initially contained an older staff-bot ZIP, not the deployed ERP application source. This companion does not claim complete coverage of every ERP screen. Missing APIs, role-specific workflow tests, in-app job progress/callbacks and durable scoped ERP grants require changes in the actual ERP source. In particular, adding this worker does not grant a missing `erp.messaging` permission or add a Meta-template creation endpoint.
