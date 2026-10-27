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

  # 2. KEEP Policy: protect only the most recent version per package
  # (rollback means redeploying, which builds a fresh image anyway).
  # NOTE: ``condition`` and ``most_recent_versions`` are mutually
  # exclusive in one policy (API oneof) — keep_count cannot be
  # scoped to app/*, so build-cache/* also keeps its newest version.
  # The cleanup-artifacts workflow deletes it anyway on every run;
  # this policy is only the daily-evaluated backstop.
  cleanup_policies {
    id     = "keep-latest-1"
    action = "KEEP"
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

  # 2. KEEP Policy: protect the most recent 2 versions per package —
  # the Cloud Run Job is pinned to the newest, and the spare covers
  # the window where a job update lags one deploy behind.
  # NOTE: ``condition`` and ``most_recent_versions`` are mutually
  # exclusive in one policy (API oneof), so this also shields 2
  # versions of stray packages like ``bible-research`` PR previews —
  # the cleanup-artifacts workflow purges those on every run.
  cleanup_policies {
    id     = "keep-latest-2"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
    }
  }

  depends_on = [google_project_service.enabled]
}
