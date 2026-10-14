resource "google_service_account" "audio_generator" {
  account_id   = "audio-generator"
  display_name = "Audio Generator (Cloud Run Job)"
  project      = var.project_id
}

# The Job needs to read DATABASE_URL and DJANGO_SECRET_KEY at Django
# settings import time, even though it does not touch the DB. Reuse the
# existing Secret Manager entries via the same data sources used in
# secrets.tf.
resource "google_secret_manager_secret_iam_member" "audio_generator_db_url" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.app["DATABASE_URL"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.audio_generator.email}"
}

resource "google_secret_manager_secret_iam_member" "audio_generator_django_secret" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.app["DJANGO_SECRET_KEY"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.audio_generator.email}"
}

resource "google_secret_manager_secret_iam_member" "audio_generator_dbt_key" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.app["DBT_KEY"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.audio_generator.email}"
}

resource "google_secret_manager_secret_iam_member" "audio_generator_esv_key" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.app["ESV_KEY"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.audio_generator.email}"
}

resource "google_secret_manager_secret_iam_member" "audio_generator_db_ssl_cert" {
  project   = var.project_id
  secret_id = data.google_secret_manager_secret.app["DB_SSL_CERT"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.audio_generator.email}"
}

resource "google_cloud_run_v2_job" "audio_generator" {
  name     = "audio-generator"
  location = local.app_engine_region
  project  = var.project_id

  template {
    template {
      service_account = google_service_account.audio_generator.email
      timeout         = "3600s"
      max_retries     = 0

      containers {
        # Bootstrap image only. The real image is built and pushed by
        # the ``Deploy to App Engine`` GitHub Actions workflow (see
        # .github/workflows/deploy.yml) into the dedicated
        # ``audio-generator`` Artifact Registry repository under the
        # deterministic tag ``audio-generator:<git-sha>``. That same
        # workflow then runs ``gcloud run jobs update`` to point this
        # Cloud Run Job at the freshly pushed immutable digest. The
        # ``ignore_changes`` lifecycle below tells Terraform not to
        # fight the CI writer, which is also why we can safely keep a
        # placeholder bootstrap image here (the public
        # ``gcr.io/cloudrun/hello`` is guaranteed to exist so the very
        # first ``terraform apply`` — before CI has pushed anything —
        # still succeeds).
        image = "gcr.io/cloudrun/hello"

        command = [
          "python", "manage.py", "generate_chapter_audio",
          "--fileset-id", "LVSGLU8",
          # Run a verse-mode pass then a chapter-mode pass over the
          # same worklist; each mode has its own monthly run lock and
          # writes under its own GCS prefix (audio/ vs audio_chapter/).
          "--mode", "both",
        ]

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
          name = "DATABASE_URL"
          value_source {
            secret_key_ref {
              secret  = data.google_secret_manager_secret.app["DATABASE_URL"].secret_id
              version = "latest"
            }
          }
        }

        env {
          name = "DJANGO_SECRET_KEY"
          value_source {
            secret_key_ref {
              secret  = data.google_secret_manager_secret.app["DJANGO_SECRET_KEY"].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.audio_generator_db_url,
    google_secret_manager_secret_iam_member.audio_generator_django_secret,
    google_secret_manager_secret_iam_member.audio_generator_dbt_key,
    google_secret_manager_secret_iam_member.audio_generator_esv_key,
    google_secret_manager_secret_iam_member.audio_generator_db_ssl_cert,
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

# Required to create/manage Cloud Scheduler jobs (scheduler.tf).
resource "google_project_iam_member" "github_deployer_scheduler_admin" {
  project = var.project_id
  role    = "roles/cloudscheduler.admin"
  member  = "serviceAccount:${google_service_account.github_deployer.email}"

  depends_on = [
    google_project_service.enabled,
    google_service_account.github_deployer,
  ]
}

# github-deployer needs these additional permissions to run
# ``gcloud run jobs update audio-generator --image=...`` from the
# Deploy to App Engine workflow, and to manage the IAM policy on the
# Cloud Run Job (e.g. granting the audio-scheduler SA the Invoker
# role in scheduler.tf). ``roles/run.admin`` is required over
# ``roles/run.developer`` because only ``admin`` grants
# ``run.jobs.setIamPolicy``. The serviceAccountUser binding below
# (required to act as the runtime SA) is still scoped to the specific
# ``audio-generator`` SA rather than granted project-wide.
resource "google_project_iam_member" "github_deployer_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.github_deployer.email}"

  depends_on = [
    google_project_service.enabled,
    google_service_account.github_deployer,
  ]
}

resource "google_service_account_iam_member" "github_deployer_act_as_audio_generator" {
  service_account_id = google_service_account.audio_generator.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_deployer.email}"
}
