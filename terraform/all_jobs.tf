# Master Cloud Run Job: runs bible/management/commands/run_all_jobs.py,
# which executes the sub-jobs listed in its JOBS table sequentially.
# This is the project's only Cloud Run Job — it replaced the standalone
# ``audio-generator`` job (Phase 2), whose runtime service account and
# env-file secret wiring moved into this file unchanged. No
# google_cloud_scheduler_job: scheduling stays disabled (see
# scheduler.tf).

# Runtime SA for all-jobs. The account keeps the ``audio-generator``
# id for stability — renaming the account_id would force a
# destroy/recreate of the service account.
resource "google_service_account" "audio_generator" {
  account_id   = "audio-generator"
  display_name = "Audio Generator (Cloud Run Job)"
  project      = var.project_id
}

# The Job needs the app's .env config at Django settings import time.
# It is injected as the DOTENV env var sourced from the consolidated
# ``env-file`` secret (see secrets.tf) — settings.py parses its value
# directly, so no per-secret env vars or API calls are needed.
resource "google_secret_manager_secret_iam_member" "audio_generator_env_file" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.env_file.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.audio_generator.email}"
}

resource "google_cloud_run_v2_job" "all_jobs" {
  name     = "all-jobs"
  location = local.app_engine_region
  project  = var.project_id

  template {
    template {
      service_account = google_service_account.audio_generator.email
      # 5400s — headroom for serial sub-job growth over the
      # single-job 3600s template this file was copied from; the
      # master runs its JOBS entries serially, so the cap must
      # cover the sum of sub-job runtimes.
      timeout     = "5400s"
      max_retries = 0

      containers {
        # Bootstrap image only. The real image is pushed by the
        # ``Deploy to App Engine`` GitHub Actions workflow (see
        # .github/workflows/deploy.yml), which then runs
        # ``gcloud run jobs update all-jobs`` to point this Job at
        # the freshly pushed immutable digest. The
        # ``ignore_changes`` lifecycle below tells Terraform not to
        # fight the CI writer, so the public ``gcr.io/cloudrun/hello``
        # placeholder keeps the very first ``terraform apply``
        # working before CI has pushed anything.
        image = "gcr.io/cloudrun/hello"

        command = [
          "python", "manage.py", "run_all_jobs",
        ]

        # Env surface carried over from the former audio-generator
        # job: every sub-job in the master JOBS list must find the
        # configuration it expects (TTS voice, audio bucket, monthly
        # budget, run-lock staleness, Django settings, DOTENV secret
        # payload).
        env {
          name  = "FILESET_ID"
          value = "LVSGLU8"
        }
        env {
          name  = "GOOGLE_TTS_LANGUAGE_CODE"
          value = "lv-LV"
        }
        env {
          name  = "GOOGLE_TTS_VOICE_NAME"
          value = "lv-LV-Chirp3-HD-Sadachbia"
        }
        env {
          name  = "GOOGLE_TTS_SAMPLE_RATE_HERTZ"
          value = "24000"
        }
        env {
          name  = "AUDIO_BUCKET_NAME"
          value = google_storage_bucket.bible_audio.name
        }
        env {
          name  = "AUDIO_SIGNED_URL_TTL_SECONDS"
          value = "3600"
        }
        env {
          name  = "MONTHLY_TTS_CHAR_LIMIT"
          value = "450000"
        }
        env {
          name  = "LOCK_STALE_HOURS"
          value = "24"
        }
        env {
          name  = "DJANGO_SETTINGS_MODULE"
          value = "bible_research.settings"
        }
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }

        env {
          name = "DOTENV"
          value_source {
            secret_key_ref {
              secret  = data.google_secret_manager_secret.env_file.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.audio_generator_env_file,
  ]

  lifecycle {
    # The image is owned by the CI pipeline (see deploy.yml). Without
    # this ignore, every ``terraform apply`` after a CI deploy would
    # revert the job to the bootstrap placeholder.
    ignore_changes = [
      template[0].template[0].containers[0].image,
      client,
      client_version,
    ]
  }
}

# github-deployer needs to act as the all-jobs runtime SA when running
# ``gcloud run jobs update all-jobs`` from the Deploy to App Engine
# workflow. Kept scoped to this specific SA rather than granted
# project-wide.
resource "google_service_account_iam_member" "github_deployer_act_as_audio_generator" {
  service_account_id = google_service_account.audio_generator.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_deployer.email}"
}
