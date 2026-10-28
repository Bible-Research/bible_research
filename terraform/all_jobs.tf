# Master Cloud Run Job: runs bible/management/commands/run_all_jobs.py,
# which executes the sub-jobs listed in its JOBS table sequentially.
# Phase 1: manually runnable only — no google_cloud_scheduler_job.
# Reuses the audio-generator runtime service account and the same
# env-file secret wiring (see audio_generator.tf).
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
      timeout         = "5400s"
      max_retries     = 0

      containers {
        # Bootstrap image only. The real image is pushed by the
        # ``Deploy to App Engine`` GitHub Actions workflow (see
        # .github/workflows/deploy.yml), which then runs
        # ``gcloud run jobs update all-jobs`` to point this Job at the
        # freshly pushed immutable digest — the same wiring as the
        # audio-generator job (see audio_generator.tf). The
        # ``ignore_changes`` lifecycle below tells Terraform not to
        # fight the CI writer, so the public ``gcr.io/cloudrun/hello``
        # placeholder keeps the very first ``terraform apply``
        # working before CI has pushed anything.
        image = "gcr.io/cloudrun/hello"

        command = [
          "python", "manage.py", "run_all_jobs",
        ]

        # Same env surface as the audio-generator job: every sub-job
        # in the master JOBS list must find the configuration it
        # expects (TTS voice, audio bucket, monthly budget, run-lock
        # staleness, Django settings, DOTENV secret payload).
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
