# ============================================================================
# SCHEDULER RESOURCES - CURRENTLY DISABLED
# ============================================================================
# To re-enable: uncomment all resources below and run `terraform apply`
# Disabled on: 2026-09-17
#
# Note: this example now targets ``all-jobs``, which replaced the
# standalone ``audio-generator`` Cloud Run Job (Phase 2). Sub-jobs and
# their per-day gating live in the JOBS table of
# bible/management/commands/run_all_jobs.py — the scheduler below would
# simply trigger the master run.
# ============================================================================

# resource "google_service_account" "all_jobs_scheduler" {
#   account_id   = "all-jobs-scheduler"
#   display_name = "All-Jobs Scheduler (invokes Cloud Run Job)"
#   project      = var.project_id
# }
#
# resource "google_cloud_run_v2_job_iam_member" "all_jobs_scheduler_invoker" {
#   project  = var.project_id
#   location = google_cloud_run_v2_job.all_jobs.location
#   name     = google_cloud_run_v2_job.all_jobs.name
#   role     = "roles/run.invoker"
#   member   = "serviceAccount:${google_service_account.all_jobs_scheduler.email}"
# }
#
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
