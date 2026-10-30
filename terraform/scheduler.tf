# ============================================================================
# SCHEDULER RESOURCES
# ============================================================================
# The daily ``daily-gemini-audio`` trigger below is ENABLED. It is the
# only scheduled trigger of ``all-jobs``.
#
# It must not be a plain trigger: the JOBS table in
# bible/management/commands/run_all_jobs.py also contains the
# monthly ``dom:3`` ``generate_chapter_audio`` entry (Google Cloud
# TTS spend, intentionally left unscheduled), so a bare ``jobs:run``
# call would still fire it on the 3rd of each month. The http_target
# body therefore passes ``overrides.containerOverrides`` — the job
# container's command is ``python manage.py run_all_jobs`` and the
# override supplies ``args`` = ``["--only",
# "generate_gemini_audio"]``, restricting the pass to that single
# JOBS entry. The ``generate_gemini_audio`` entry additionally
# carries schedule ``manual``, so even an override-less run cannot
# start it implicitly.
# ============================================================================

# Invoker SA for the scheduler trigger. Its email is already in the
# github-deployer scoped serviceAccountAdmin condition (iam.tf) —
# after the first apply, add the new account's uniqueId to
# ``local.managed_sa_unique_ids`` there too (see the comment above
# that local) so uniqueId-addressed ops stay covered.
resource "google_service_account" "all_jobs_scheduler" {
  account_id   = "all-jobs-scheduler"
  display_name = "All-Jobs Scheduler (invokes Cloud Run Job)"
  project      = var.project_id
}

resource "google_cloud_run_v2_job_iam_member" "all_jobs_scheduler_invoker" {
  project  = var.project_id
  location = google_cloud_run_v2_job.all_jobs.location
  name     = google_cloud_run_v2_job.all_jobs.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.all_jobs_scheduler.email}"
}

# 09:00 UTC daily: the Gemini free-tier quota resets at midnight
# America/Los_Angeles, which is 07:00–08:00 UTC depending on DST, so
# this always lands safely after the reset while still being a
# daytime run in Europe/Riga.
resource "google_cloud_scheduler_job" "daily_gemini_audio" {
  name             = "daily-gemini-audio"
  description      = "Daily Gemini free-tier audio sub-job of all-jobs."
  schedule         = "0 9 * * *"
  time_zone        = "Etc/UTC"
  project          = var.project_id
  region           = local.app_engine_region
  attempt_deadline = "320s"

  retry_config {
    # The job itself enforces quota/run-lock semantics; an
    # immediate scheduler retry would just hit the day lock.
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "https://${local.app_engine_region}-run.googleapis.com/v2/projects/${var.project_id}/locations/${local.app_engine_region}/jobs/${google_cloud_run_v2_job.all_jobs.name}:run"

    headers = {
      "Content-Type" = "application/json"
    }

    body = base64encode(jsonencode({
      overrides = {
        containerOverrides = [{
          args = ["--only", "generate_gemini_audio"]
        }]
      }
    }))

    oauth_token {
      service_account_email = google_service_account.all_jobs_scheduler.email
    }
  }

  depends_on = [
    google_cloud_run_v2_job_iam_member.all_jobs_scheduler_invoker,
  ]
}

# ============================================================================
# MONTHLY GOOGLE TTS TRIGGER — STILL DISABLED
# ============================================================================
# To re-enable: uncomment the resource below and run `terraform apply`
# Disabled on: 2026-09-17
#
# This is the paid Cloud TTS monthly pass. Do NOT resurrect it as a
# second trigger of all-jobs without checking what else a plain
# ``jobs:run`` would execute — today the dom:3 JOBS gate is the only
# thing keeping it from spending money on other days.
# ============================================================================

# resource "google_cloud_scheduler_job" "monthly_all_jobs" {
#   name             = "monthly-all-jobs"
#   description      = "Run all-jobs Cloud Run Job on the 3rd of each month."
#   schedule         = "0 3 3 * *"
#   time_zone        = "Europe/Riga"
#   project          = var.project_id
#   region           = local.app_engine_region
#   attempt_deadline = "320s"
#
#   retry_config {
#     retry_count = 0
#   }
#
#   http_target {
#     http_method = "POST"
#     uri         = "https://${local.app_engine_region}-run.googleapis.com/v2/projects/${var.project_id}/locations/${local.app_engine_region}/jobs/${google_cloud_run_v2_job.all_jobs.name}:run"
#
#     oauth_token {
#       service_account_email = google_service_account.all_jobs_scheduler.email
#     }
#   }
#
#   depends_on = [
#     google_cloud_run_v2_job_iam_member.all_jobs_scheduler_invoker,
#   ]
# }
