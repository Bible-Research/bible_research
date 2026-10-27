# GCP Artifact Registry Cleanup

## Overview

This document explains the automated cleanup of GCP Artifact Registry
images — both the `gae-standard` repository (App Engine deploy
images) and the `audio-generator` repository (Cloud Run Job image).

## Problem

GCP's free tier has storage quotas for Artifact Registry. Every App
Engine deploy leaves a permanent `operation-*` image under
`gae-standard/app/default`, every `deploy.yml` run pushes an
`audio-generator:<git-sha>` image, and Cloud Build cache images
collect under `gae-standard/build-cache/*`. Without cleanup the repos
grow without bound (~1.7 GB observed within weeks).

## Solution — two layers

1. **`.github/workflows/cleanup-artifacts.yml`** deletes stale images
   immediately — after every successful deploy, daily at 2 AM UTC,
   and on manual trigger.
2. **Terraform `cleanup_policies`** (`terraform/artifact_registry.tf`)
   are the daily-evaluated backstop: `DELETE` every version not
   matched by a `KEEP` policy (no `older_than` floor), keeping the
   newest version per package in `gae-standard` and the newest 2 in
   `audio-generator`. `cleanup_policy_dry_run = false` is set
   explicitly — dry-run policies only log and never delete.

## What the workflow keeps

- `gae-standard`: the **single newest `app/default` image** by
  `createTime`. Everything else — older deploy images,
  `build-cache/*`, images of any other package — is deleted. GAE
  does not re-pull old images at runtime; rollback means redeploying.
- `audio-generator`: the newest `audio-generator` image **plus the
  digest the Cloud Run Job is currently pinned to**. Deleting the
  referenced digest makes `terraform apply` 404 on the job update and
  can break the monthly TTS run. PR-preview images
  (`bible-research` package) older than 2h are deleted outright —
  Cloud Run materializes the image at deploy time; the age floor
  only guards the push → `gcloud run deploy` window.

## Critical implementation details

### Never sort tag listings

`gcloud artifacts docker tags list` returns **NULL**
`UPDATE_TIME`/`CREATE_TIME` for tag resources, so a
`--sort-by=~UPDATE_TIME` flag silently does nothing and the API
returns tags oldest-first. The previous workflow kept the *first*
row — i.e. the OLDEST image — and deleted everything newer,
including the digest the Cloud Run Job had been pinned to seconds
earlier (which then broke `terraform apply`). "Newest" must be
computed from `gcloud artifacts docker images list` (`createTime` +
`version` fields), sorted locally.

### Delete by digest with --delete-tags

Deleting `image@digest --delete-tags` removes the digest and all of
its tags in one call; the old tag-then-image dance is unnecessary.

### Terraform gotchas encountered

- `most_recent_versions.keep_count` is **per package**, and a
  policy's `condition` and `most_recent_versions` are **mutually
  exclusive** (API `oneof`) — keep counts cannot be scoped to
  specific packages. The workflow's per-package deletion is what
  stops `build-cache/*` and stray packages from accumulating.
- `older_than` floors leave up to ~2 days of accumulation (age +
  daily eval cadence). Omit it to delete any unkept version.
- `iam.serviceAccounts.delete` addresses SAs by **numeric unique
  ID** (`projects/-/serviceAccounts/<uid>`), so an
  `endsWith("…@…gserviceaccount.com")` IAM Condition never matches
  — the scoped `serviceAccountAdmin` grant in `iam.tf` must include
  `managed_sa_unique_ids` clauses or apply 403s.
- The Cloud Run Job's `ignore_changes` on `image` does not protect
  `terraform apply` from a stale referenced digest: the provider
  still sends the (refreshed) live image value in update payloads,
  and the API rejects missing images. Fix by re-pinning the job
  (`gcloud run jobs update --image=<existing-digest>`).

## Manual cleanup

To run immediately: GitHub Actions → "Cleanup Artifact Registry" →
Run workflow. Or ad-hoc:

```bash
REGISTRY=europe-west3-docker.pkg.dev/bible-research-489314
gcloud artifacts docker images list "${REGISTRY}/gae-standard" \
  --include-tags
gcloud artifacts docker images delete \
  "${REGISTRY}/gae-standard/app/default@sha256:<digest>" \
  --delete-tags --quiet
```

## Monitoring

- Green = stale images deleted (or none existed)
- Red = auth/API failure (needs investigation)
- Skipped = triggering deployment failed

## Related Files

- `.github/workflows/cleanup-artifacts.yml` — the cleanup workflow
- `terraform/artifact_registry.tf` — repo + cleanup policies
- `terraform/iam.tf` — scoped SA-admin condition
- `.github/workflows/deploy.yml` — image producer + job pinner
- `docs/APP_ENGINE_VERSION_CLEANUP.md` — GAE *version* cleanup
  (serving versions, not images)
