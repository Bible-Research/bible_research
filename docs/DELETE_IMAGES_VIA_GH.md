# Delete Old GCP Artifact Registry Images

## Requirement

Create github action that would delete old GCP AF images during
deployments to not exceed the AF storage limits due to the 24 h
retention period before GCP automatically deletes old images.

## Status

✅ **COMPLETED**

## Implementation

See `ARTIFACT_REGISTRY_CLEANUP.md` for full documentation.

The cleanup workflow is implemented in:
`.github/workflows/cleanup-artifacts.yml`

### Key Features

- Runs after successful deployments
- Daily scheduled cleanup at 2 AM UTC
- Manual trigger available
- Keeps only the most recent tagged image
- Proper error handling and failure detection
- Deletes tags first, then untagged images

### Usage

The workflow runs automatically. To manually trigger:

1. Go to GitHub Actions
2. Select "Cleanup Artifact Registry" workflow
3. Click "Run workflow"