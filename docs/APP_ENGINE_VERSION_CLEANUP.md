# App Engine Version Cleanup

## Problem

Google Cloud App Engine accumulates old, inactive versions every time you
deploy. These versions consume storage quota and clutter the console, even
though they receive 0% traffic. With 60+ versions stored, cleanup is
necessary.

## Solution Overview

This project implements automatic cleanup of old App Engine versions through
two mechanisms:

1. **Automated CI/CD Cleanup** - GitHub Actions automatically cleans up old
   versions after each deployment
2. **Manual Script** - Standalone bash script for ad-hoc cleanup

## Implementation Details

### 1. Cleanup Script

**Location:** `scripts/cleanup_app_engine_versions.sh`

**What it does:**
- Lists all App Engine versions for a service
- Identifies versions receiving 0% traffic (non-serving versions)
- Deletes all non-serving versions
- Keeps only versions actively serving traffic (100% traffic)

**Usage:**
```bash
# Use current gcloud project and default service
./scripts/cleanup_app_engine_versions.sh

# Specify project and service
./scripts/cleanup_app_engine_versions.sh bible-research-489314 default

# With explicit arguments
./scripts/cleanup_app_engine_versions.sh <PROJECT_ID> <SERVICE_NAME>
```

**Safety Features:**
- Never deletes versions receiving traffic
- Provides detailed output of what will be deleted
- Continues on individual version deletion failures
- Shows remaining versions after cleanup

### 2. GitHub Actions Integration

**Location:** `.github/workflows/deploy.yml`

**When it runs:**
- Automatically after every successful App Engine deployment
- After the "Deploy to App Engine" step
- Before the "Recent serving versions" reporting step

**What changed:**
```yaml
- name: Cleanup old App Engine versions
  run: |
    chmod +x scripts/cleanup_app_engine_versions.sh
    ./scripts/cleanup_app_engine_versions.sh \
      "${GCP_PROJECT_ID}" "default"
```

This ensures that every CI/CD deployment automatically cleans up old
versions, preventing accumulation.

## Deployment Process Changes

### Current Deployment Flow

1. **GitHub Actions triggers** (push to main or manual workflow)
2. **Checkout code** and set up Python
3. **Authenticate to GCP** via Workload Identity Federation
4. **Run database migrations**
5. **Deploy to App Engine** with `--stop-previous-version`
6. **🆕 Cleanup old versions** (NEW STEP)
7. **Show recent serving versions**
8. **Build and push job image** (to the `audio-generator`
   Artifact Registry repo)
9. **Update `all-jobs` Cloud Run Job**

### What Changed in `gcloud app deploy`

**Before:**
```bash
gcloud app deploy app.yaml \
  --project="${GCP_PROJECT_ID}" \
  --quiet --promote --stop-previous-version
```

**After:**
Same command, but now followed by automatic cleanup script.

**Why `--stop-previous-version` isn't enough:**
- This flag only STOPS the previous version (makes it receive 0% traffic)
- It does NOT delete the version
- Stopped versions still consume storage quota
- Over time, you accumulate dozens of stopped versions

## Verification Steps

### 1. Check Current Version Count

```bash
# Before cleanup
gcloud app versions list \
  --project=bible-research-489314 \
  --format="value(version.id)" | wc -l
```

### 2. Run Manual Cleanup (One-Time)

To clean up your existing 60 versions:

```bash
cd /Users/tedis.rozenfelds/personal_data/p_projects/Bible\ Research/bible_research
./scripts/cleanup_app_engine_versions.sh bible-research-489314 default
```

### 3. Verify Cleanup

```bash
# Should show only 1-2 versions (current + maybe previous if still
# serving)
gcloud app versions list \
  --project=bible-research-489314 \
  --format="table(version.id,traffic_split,last_deployed_time.date())"
```

### 4. Test Automated Cleanup

1. Push a commit to main branch
2. Wait for GitHub Actions deployment to complete
3. Check the "Cleanup old App Engine versions" step in the workflow logs
4. Verify only the latest version remains



## Monitoring and Maintenance

### Check Version Count Regularly

```bash
# Quick count
gcloud app versions list --project=bible-research-489314 \
  --format="value(version.id)" | wc -l

# Detailed view
gcloud app versions list --project=bible-research-489314 \
  --format="table(version.id,traffic_split,last_deployed_time.date())" \
  --sort-by="~version.createTime"
```

### Expected Behavior

**After each deployment:**
- 1 version receiving 100% traffic (the latest)
- 0 old versions (all cleaned up)

**During deployment:**
- Briefly 2 versions (old + new)
- Old version stopped, then deleted
- Final state: 1 version

### Troubleshooting

**If cleanup fails in CI/CD:**
1. Check GitHub Actions logs for the cleanup step
2. Verify the service account has `appengine.versions.delete` permission
3. Run cleanup manually to identify the issue

**If versions accumulate again:**
1. Check if the cleanup step is running in CI/CD
2. Verify the script has execute permissions
3. Check for errors in the workflow logs

**Manual recovery:**
```bash
# Delete a specific version
gcloud app versions delete VERSION_ID \
  --project=bible-research-489314 \
  --service=default \
  --quiet

# Or run the cleanup script
./scripts/cleanup_app_engine_versions.sh bible-research-489314 default
```

## IAM Permissions Required

The deployment service account needs:
- `appengine.versions.list` - List versions
- `appengine.versions.delete` - Delete versions
- `appengine.versions.get` - Get version details

These are included in the `roles/appengine.appAdmin` role, which your
`github-deployer@bible-research-489314.iam.gserviceaccount.com` should
already have.

## Cost Impact

**Before cleanup:**
- 60 versions × ~100MB each = ~6GB storage
- Storage costs + version metadata overhead

**After cleanup:**
- 1 version × ~100MB = ~100MB storage
- ~98% reduction in App Engine storage usage

## Future Enhancements

Potential improvements (not implemented):

1. **Keep N recent versions** - Modify script to keep last 3 versions
   instead of only serving versions
2. **Dry-run mode** - Add `--dry-run` flag to preview deletions
3. **Slack notifications** - Alert on cleanup failures
4. **Metrics** - Track version count over time in Cloud Monitoring

## References

- [App Engine Versions Documentation](https://cloud.google.com/appengine/docs/admin-api/deploying-apps#managing_versions)
- [gcloud app versions delete](https://cloud.google.com/sdk/gcloud/reference/app/versions/delete)
- GitHub Actions Workflow: `.github/workflows/deploy.yml`
- Cleanup Script: `scripts/cleanup_app_engine_versions.sh`
