# GCP Artifact Registry Cleanup - Quick Implementation Guide

## For Future Reference

This guide documents the key learnings from implementing the Artifact
Registry cleanup workflow. Use this when implementing similar cleanup
workflows in other projects.

## Critical Lessons Learned

### 1. Delete Tags First, Then Images

**Problem:** GCP rejects deletion of tagged images with error:
```
ERROR: Cannot delete image [...] because it is tagged.
```

**Solution:** Always delete tags first:
```bash
# 1. Delete tag
gcloud artifacts docker tags delete TAG_PATH --quiet

# 2. Then delete untagged image
gcloud artifacts docker images delete IMAGE@DIGEST --quiet
```

### 2. Use Tags, Not Images for Listing

**Problem:** Using `gcloud artifacts docker images list` with
`--format="value(digest,createTime)"` returns timestamps instead of
proper SHA256 digests, causing "NOT_FOUND" errors.

**Solution:** Use tags instead:
```bash
gcloud artifacts docker tags list \
  $REGISTRY/$SERVICE \
  --format="value(tag,version)" \
  --sort-by=~UPDATE_TIME
```

### 3. Proper Error Handling

**Problem:** Initial implementations used `|| { echo "Warning"; }`
which allowed failures to pass silently, showing green even when
nothing was deleted.

**Solution:**
- Use `set -e` to exit on errors
- Track failures with a counter
- Exit with code 1 if critical operations fail
- Only treat untagged image deletion as warnings (GCP may auto-clean)

```bash
set -e
FAILED=0

if ! gcloud artifacts docker tags delete "$TAG" --quiet; then
  echo "ERROR: Failed to delete tag"
  FAILED=$((FAILED + 1))
fi

if [ $FAILED -gt 0 ]; then
  exit 1
fi
```

### 4. Sort by UPDATE_TIME, Not CREATE_TIME

**Problem:** Sorting by `createTime` may not reflect the actual
"latest" image if images are updated.

**Solution:** Sort by `UPDATE_TIME` descending:
```bash
--sort-by=~UPDATE_TIME
```

## Complete Working Script Template

```bash
set -e  # Exit on any error

REGISTRY="region-docker.pkg.dev/project/repo"
SERVICE="service-name"

# List all tags sorted by update time (newest first)
TAGS=$(gcloud artifacts docker tags list \
  "${REGISTRY}/${SERVICE}" \
  --format="value(tag,version)" \
  --sort-by=~UPDATE_TIME \
  --project="$PROJECT_ID")

if [ -z "$TAGS" ]; then
  echo "No tagged images found."
  exit 0
fi

TAG_COUNT=$(echo "$TAGS" | wc -l)
echo "Found ${TAG_COUNT} tagged image(s)"

if [ "$TAG_COUNT" -le 1 ]; then
  echo "Only 1 or fewer images. Nothing to delete."
  exit 0
fi

# Keep first tag, delete rest
TAGS_TO_DELETE=$(echo "$TAGS" | tail -n +2)
DELETE_COUNT=$((TAG_COUNT - 1))
echo "Deleting ${DELETE_COUNT} old tagged image(s)..."

# Track failures and deleted digests
FAILED=0
declare -a DELETED_DIGESTS

# Delete old tags
while IFS=$'\t' read -r tag version; do
  if [ -n "$tag" ]; then
    echo "Deleting tag: ${REGISTRY}/${SERVICE}:${tag}"
    if ! gcloud artifacts docker tags delete \
      "${REGISTRY}/${SERVICE}:${tag}" \
      --quiet \
      --project="$PROJECT_ID" 2>&1; then
      echo "ERROR: Failed to delete tag ${tag}"
      FAILED=$((FAILED + 1))
    else
      echo "Successfully deleted tag"
      DELETED_DIGESTS+=("$version")
    fi
  fi
done <<< "$TAGS_TO_DELETE"

# Delete untagged images (best effort)
echo "Deleting untagged images..."
for digest in "${DELETED_DIGESTS[@]}"; do
  if gcloud artifacts docker images delete \
    "${REGISTRY}/${SERVICE}@${digest}" \
    --quiet \
    --project="$PROJECT_ID" 2>&1; then
    echo "Successfully deleted untagged image"
  else
    echo "WARNING: Could not delete ${digest} (may be auto-cleaned)"
  fi
done

# Fail if tag deletions failed
if [ $FAILED -gt 0 ]; then
  echo "ERROR: ${FAILED} tag deletion(s) failed"
  exit 1
fi

echo "Cleanup complete!"
```

## GitHub Actions Workflow Template

```yaml
name: Cleanup Artifact Registry

on:
  workflow_run:
    workflows: ["Deploy Workflow Name"]
    types: [completed]
    branches: [main]
  schedule:
    - cron: '0 2 * * *'
  workflow_dispatch:

permissions:
  contents: read
  id-token: write

env:
  GCP_PROJECT_ID: your-project-id
  GCP_REGION: your-region
  IMAGE_REPO: your-repo
  IMAGE_NAME: your-image

jobs:
  cleanup:
    runs-on: ubuntu-latest
    if: >
      github.event_name == 'workflow_dispatch' ||
      github.event_name == 'schedule' ||
      github.event.workflow_run.conclusion == 'success'
    
    steps:
      - name: Authenticate to Google Cloud
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ env.WIF_PROVIDER }}
          service_account: ${{ env.DEPLOY_SA }}

      - name: Set up gcloud
        uses: google-github-actions/setup-gcloud@v2
        with:
          project_id: ${{ env.GCP_PROJECT_ID }}

      - name: Cleanup old images
        run: |
          # Insert script from above template
          
      - name: Show remaining images
        if: always()
        run: |
          gcloud artifacts docker tags list \
            "${REGISTRY}/${SERVICE}" \
            --format="table(tag,version,UPDATE_TIME)" \
            --sort-by=~UPDATE_TIME \
            --project="${GCP_PROJECT_ID}"
```

## Required Permissions

Service account needs:
- `roles/artifactregistry.writer` or equivalent
- Permission to delete images and tags

## Testing Checklist

- [ ] Run manually via `workflow_dispatch`
- [ ] Verify only 1 tagged image remains after cleanup
- [ ] Check that storage usage decreases in GCP Console
- [ ] Verify workflow shows RED when deletions fail
- [ ] Test with 0 images (should exit gracefully)
- [ ] Test with 1 image (should skip deletion)
- [ ] Test with multiple images (should delete all but newest)

## Common Pitfalls to Avoid

1. ❌ Don't delete images before tags
2. ❌ Don't use `gcloud artifacts docker images list` for getting
   digests
3. ❌ Don't ignore errors with `|| true` or `|| echo "Warning"`
4. ❌ Don't sort by `createTime` - use `UPDATE_TIME`
5. ❌ Don't assume failures will be visible - track them explicitly
6. ❌ Don't use `--format="value(digest)"` - use `--format="value(tag,version)"`

## Expected Behavior

**Success (Green):**
- All old tags deleted successfully
- Untagged images deleted or auto-cleaned
- Only 1 tagged image remains
- Exit code 0

**Failure (Red):**
- Any tag deletion fails
- Shows error count
- Exit code 1

## Benefits

1. **Cost Savings** - Stay within GCP free tier quotas
2. **Immediate Cleanup** - Don't wait 24h for GCP auto-cleanup
3. **Automated** - Runs after deployments and on schedule
4. **Reliable** - Proper error handling
5. **Transparent** - Clear logging

## Reference Implementation

See `.github/workflows/cleanup-artifacts.yml` in the bible_research
repository for the complete working implementation.
