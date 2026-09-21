# GCP Artifact Registry Cleanup

## Overview

This document explains the automated cleanup workflow for GCP Artifact
Registry images used by the audio-generator Cloud Run Job.

## Problem

GCP's free tier has storage quotas for Artifact Registry. While GCP has
built-in cleanup policies, they can take up to 24 hours to delete old
images. During active development with multiple deployments per day,
this delay can cause storage quota issues.

## Solution

The `.github/workflows/cleanup-artifacts.yml` workflow automatically
deletes old Docker images immediately after deployments, keeping only
the most recent tagged image.

## How It Works

### Triggers

The workflow runs in three scenarios:

1. **After successful deployments** - Triggered by the "Deploy to App
   Engine" workflow completion
2. **Daily schedule** - Runs at 2 AM UTC via cron
3. **Manual trigger** - Can be run manually via GitHub Actions UI

### Cleanup Logic

The workflow follows this critical sequence:

1. **List all tagged images** sorted by update time (newest first)
2. **Keep only the most recent tag** (the first one)
3. **Delete old tags first** using `gcloud artifacts docker tags
   delete`
4. **Delete untagged images** using `gcloud artifacts docker images
   delete`

### Why Delete Tags First?

**CRITICAL**: GCP will reject deletion attempts on images that have
tags with error:
```
ERROR: Cannot delete image [...] because it is tagged.
Existing tags are: [...]
```

The workflow must always delete tags before attempting to delete the
underlying images.

## Implementation Details

### Authentication

Uses the same Workload Identity Federation setup as the deployment
workflow:
- Provider: `github-actions` workload identity pool
- Service Account: `github-deployer@bible-research-489314.iam.gserviceaccount.com`

### Required Permissions

The service account needs:
- `roles/artifactregistry.writer` or equivalent
- Permission to delete images and tags in Artifact Registry

### Error Handling

The workflow uses strict error handling:

- `set -e` - Exit on any error
- **Tag deletion failures** → Workflow fails (RED status)
- **Untagged image deletion failures** → Warning only (GCP may
  auto-clean)
- Failure counter tracks issues
- Exit code 1 if any critical operations fail

### Output

**Success (Green):**
```
Found 8 tagged image(s) in registry
Deleting 7 old tagged image(s)...
Deleting tag: europe-west3-docker.pkg.dev/.../audio-generator:abc123
Successfully deleted tag
...
Deleting untagged images...
Successfully deleted untagged image
Cleanup complete! All old images deleted successfully.

Remaining tagged images:
TAG        VERSION            UPDATE_TIME
xyz789     sha256:def456     2026-09-21T08:00:00
```

**Failure (Red):**
```
ERROR: Failed to delete tag abc123
ERROR: 3 tag deletion(s) failed
```

## Benefits

1. **Cost Savings** - Stay within GCP free tier quotas
2. **Immediate Cleanup** - Don't wait 24h for GCP's auto-cleanup
3. **Automated** - Runs after every deployment and on schedule
4. **Reliable** - Proper error handling and failure detection
5. **Transparent** - Clear logging of what's being deleted

## Monitoring

Check the workflow status in GitHub Actions:
- Green = All old images deleted successfully
- Red = Tag deletion failed (needs investigation)
- Yellow = Workflow skipped (deployment failed)

## Manual Cleanup

To manually trigger cleanup:

1. Go to GitHub Actions
2. Select "Cleanup Artifact Registry" workflow
3. Click "Run workflow"
4. Select branch (usually `main`)
5. Click "Run workflow"

## Troubleshooting

### "Cannot delete image because it is tagged"

This means the workflow tried to delete an image before deleting its
tags. This should not happen with the current implementation, but if
it does:

1. Check the workflow logs
2. Verify tags are being deleted first
3. Check for race conditions with concurrent deployments

### "NOT_FOUND" errors

This usually means:
- Image was already deleted (auto-cleanup)
- Incorrect image reference format
- Image digest changed

These are treated as warnings for untagged images.

### Permission denied

The service account needs `roles/artifactregistry.writer`. Check IAM
permissions:

```bash
gcloud projects get-iam-policy bible-research-489314 \
  --flatten="bindings[].members" \
  --filter="bindings.members:github-deployer@*"
```

## Related Files

- `.github/workflows/cleanup-artifacts.yml` - The cleanup workflow
- `.github/workflows/deploy.yml` - Main deployment workflow
- `docs/DELETE_IMAGES_VIA_GH.md` - Original requirement

## Future Improvements

Potential enhancements:

1. **Keep N images** - Instead of just 1, keep the last N deployments
2. **Size-based cleanup** - Delete based on total storage size
3. **Slack notifications** - Alert on cleanup failures
4. **Metrics** - Track storage savings over time
