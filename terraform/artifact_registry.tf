# App Engine Standard created this repository; we track it to avoid drift.
resource "google_artifact_registry_repository" "gae_standard" {
  # App Engine Standard uses the regional repo in the app region (not var.region / tfvars).
  location      = "europe-west3"
  repository_id = "gae-standard"
  description   = "Repository to store images related to App Engine Standard deployments - Cost Optimized"
  format        = "DOCKER"

  # 1. DELETE Policy: Target ALL images older than 1 day
  cleanup_policies {
    id     = "delete-old-versions"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "86400s" # 24 Hours
    }
  }

  # 2. KEEP Policy: Protect the most recent 2 versions from deletion
  cleanup_policies {
    id     = "keep-latest-2"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
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
  description   = "Container image for the audio-generator Cloud Run Job - Cost Optimized"
  format        = "DOCKER"

  # 1. DELETE Policy: Target ALL images older than 1 day
  cleanup_policies {
    id     = "delete-old-versions"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "86400s" # 24 Hours
    }
  }

  # 2. KEEP Policy: Protect the most recent 2 versions from deletion
  cleanup_policies {
    id     = "keep-latest-2"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
    }
  }

  depends_on = [google_project_service.enabled]
}
