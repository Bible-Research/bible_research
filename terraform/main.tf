terraform {
  required_version = ">= 1.3"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "project" {
  project_id = var.project_id
}

locals {
  apis = [
    "appengine.googleapis.com",
    "appenginereporting.googleapis.com",
    "appoptimize.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "texttospeech.googleapis.com",
    "speech.googleapis.com",
  ]

  # Matches Artifact Registry + App Engine (artifact_registry.tf).
  # Audio bucket, Cloud Run Job, and Scheduler must use this
  # region—never derive only from var.region (tfvars can drift).
  app_engine_region = google_artifact_registry_repository.gae_standard.location
}

resource "google_project_service" "enabled" {
  for_each = toset(local.apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
