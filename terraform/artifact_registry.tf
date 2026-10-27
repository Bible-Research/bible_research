# App Engine Standard created this repository; we track it to avoid drift.
resource "google_artifact_registry_repository" "gae_standard" {
  # App Engine Standard uses the regional repo in the app region
  # (not var.region / tfvars).
  location      = "europe-west3"
  repository_id = "gae-standard"
  description   = "App Engine Standard deployment images - Cost Optimized"
  format        = "DOCKER"

  # Policies must actively delete, not just log intended actions.
  cleanup_policy_dry_run = false

  # 1. DELETE Policy: every version not protected by a KEEP policy.
  # No older_than floor — stale deploy images and build-cache/*
  # packages are removed at the next (~daily) policy evaluation.
  cleanup_policies {
    id     = "delete-unkept-versions"
    action = "DELETE"
    condition {
      tag_state = "ANY"
    }
  }

  # 2. KEEP Policy: protect only the most recent deploy image, and
  # only inside app/* — keep_count is per-package, so scoping the
  # KEEP policy to app/ leaves build-cache/* (and any other stray
  # package) always deletable. Rollback means redeploying, which
  # builds a fresh image anyway.
  cleanup_policies {
    id     = "keep-latest-1"
    action = "KEEP"
    condition {
      package_name_prefixes = ["app/"]
    }
    most_recent_versions {
      keep_count = 1
    }
  }

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [google_project_service.enabled]
}

# Dedicated repository for the audio-generator Cloud Run Job image.
# The image is built and pushed by the Deploy workflow (see
# .github/workflows/deploy.yml) under a deterministic name we own —
# "audio-generator:<git-sha>" — so the Cloud Run Job no longer depends
# on App Engine Standard's auto-generated image hashes (which Terraform
# cannot predict and which forced an unusable ":latest" reference).
resource "google_artifact_registry_repository" "audio_generator" {
  location      = "europe-west3"
  repository_id = "audio-generator"
  description   = "audio-generator Cloud Run Job image - Cost Optimized"
  format        = "DOCKER"

  cleanup_policy_dry_run = false

  # 1. DELETE Policy: every version not protected by a KEEP policy.
  cleanup_policies {
    id     = "delete-unkept-versions"
    action = "DELETE"
    condition {
      tag_state = "ANY"
    }
  }

  # 2. KEEP Policy: protect the most recent 2 versions — the Cloud
  # Run Job is pinned to the newest, and the spare covers the window
  # where a job update lags one deploy behind. Scoped to the
  # audio-generator package: keep_count is per-package, and without
  # the prefix a stray package (e.g. ``bible-research`` PR-preview
  # images from deploy-pr-preview.yml) would keep 2 versions forever.
  cleanup_policies {
    id     = "keep-latest-2"
    action = "KEEP"
    condition {
      package_name_prefixes = ["audio-generator"]
    }
    most_recent_versions {
      keep_count = 2
    }
  }

  depends_on = [google_project_service.enabled]
}
