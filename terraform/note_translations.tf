# ============================================================================
# NOTE TRANSLATIONS — Cloud Translation + Cloud Tasks pipeline
# ============================================================================
# Note writes enqueue a translate-note task; the OIDC-protected
# worker endpoint (``POST /api/v1/tasks/translate-note/``) accepts
# ONLY tokens minted for ``tasks-invoker`` (TASKS_INVOKER_SA_EMAIL
# in Django settings — same value as
# google_service_account.tasks_invoker.email).
# ============================================================================

# Dedicated invoker SA — Cloud Tasks mints OIDC tokens AS this
# account; the worker verifies the token's ``email`` claim. Kept
# separate from the appspot runtime SA so the endpoint's blast
# radius is exactly "run translation tasks".
resource "google_service_account" "tasks_invoker" {
  account_id   = "tasks-invoker"
  display_name = "Note Translate Task Invoker (Cloud Tasks OIDC)"
  project      = var.project_id

  depends_on = [google_project_service.enabled]
}

# Cloud Tasks mints the OIDC token for oidc_token.service_account_
# email through its own service agent, which needs TokenCreator on
# the invoker SA — without it every task fails PERMISSION_DENIED at
# enqueue (same failure shape as the scheduler trigger's OAuth
# token, seen 2026-10-31).
resource "google_service_account_iam_member" "cloudtasks_token_creator" {
  service_account_id = google_service_account.tasks_invoker.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member = join("", [
    "serviceAccount:service-",
    "${data.google_project.project.number}",
    "@gcp-sa-cloudtasks.iam.gserviceaccount.com",
  ])
}

# github-deployer creates/updates the queue from CI.
resource "google_project_iam_member" "github_deployer_tasks_admin" {
  project = var.project_id
  role    = "roles/cloudtasks.admin"
  member  = "serviceAccount:${google_service_account.github_deployer.email}"

  depends_on = [
    google_project_service.enabled,
    google_service_account.github_deployer,
  ]
}

resource "google_cloud_tasks_queue" "note_translations" {
  name     = "note-translations"
  location = local.app_engine_region
  project  = var.project_id

  rate_limits {
    # At most 5 concurrent dispatches — the worker calls Cloud
    # Translation serially per note, so a low cap also bounds
    # burst spend.
    max_concurrent_dispatches = 5
    max_dispatches_per_second = 5
  }

  retry_config {
    # Bounded retry on retryable failures: the worker returns
    # non-2xx so Cloud Tasks re-delivers; the source_hash guard
    # keeps re-runs idempotent. Deterministic failures
    # (cap breach, stale hash) return 2xx and are never retried.
    max_attempts = 5
    min_backoff  = "10s"
    max_backoff  = "300s"
  }

  depends_on = [google_project_service.enabled]
}

# The App Engine runtime SA enqueues tasks and calls Cloud
# Translation from the worker endpoint.
resource "google_project_iam_member" "appspot_tasks_enqueuer" {
  project = var.project_id
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${local.appspot_email}"

  depends_on = [google_project_service.enabled]
}

resource "google_project_iam_member" "appspot_translate_user" {
  project = var.project_id
  role    = "roles/cloudtranslate.user"
  member  = "serviceAccount:${local.appspot_email}"

  depends_on = [google_project_service.enabled]
}

# ============================================================================
# MONTHLY NOTE-TRANSLATION COVERAGE AUDIT TRIGGER — ENABLED
# ============================================================================
# Fires a plain ``jobs:run`` on days 28-31 at 03:00 UTC; the
# ``check_note_translations`` JOBS entry's ``last_day_of_month``
# predicate gates execution to the real last day (Cloud Scheduler
# cron cannot express "last day"). Deliberately NO
# containerOverrides — ``--only`` would bypass the schedule gate
# and run on the 28th. On the true last day every
# ``last_day_of_month`` JOBS entry runs — review the JOBS table
# before adding another such entry.
# ============================================================================
resource "google_cloud_scheduler_job" "note_translations_audit" {
  name             = "note-translations-audit"
  description      = "Last-day-of-month note translation coverage audit."
  schedule         = "0 3 28-31 * *"
  time_zone        = "Etc/UTC"
  project          = var.project_id
  region           = local.app_engine_region
  attempt_deadline = "320s"

  retry_config {
    # The audit is read-only; a scheduler retry would just
    # re-run it, so retries are harmless but unneeded.
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri = join("", [
      "https://${local.app_engine_region}-run.googleapis.com",
      "/v2/projects/${var.project_id}",
      "/locations/${local.app_engine_region}",
      "/jobs/${google_cloud_run_v2_job.all_jobs.name}:run",
    ])

    oauth_token {
      service_account_email = google_service_account.all_jobs_scheduler.email
    }
  }

  depends_on = [
    google_cloud_run_v2_job_iam_member.all_jobs_scheduler_invoker,
  ]
}
