resource "google_storage_bucket" "bible_audio" {
  name                        = "${var.project_id}-bible-audio"
  location                    = local.app_engine_region
  project                     = var.project_id
  force_destroy               = false
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  # Versioning without a lifecycle rule would let noncurrent MP3
  # revisions accumulate forever. 30 days is long enough to recover
  # from an accidental overwrite or a bad TTS generation, short
  # enough to keep storage cost bounded.
  lifecycle_rule {
    condition {
      with_state = "ARCHIVED"
      age        = 30
    }
    action {
      type = "Delete"
    }
  }

  # STT LRO input staging: the job deletes stt_staging/ objects
  # after recognition finishes; this rule is the belt-and-braces
  # backstop when it dies first. NOTE: with versioning enabled a
  # Delete action only makes blobs noncurrent — bytes are freed
  # by the noncurrent-after-30-days rule above (~37-day
  # effective retention). Harmless at these sizes.
  lifecycle_rule {
    condition {
      age            = 7
      matches_prefix = ["stt_staging/"]
    }
    action {
      type = "Delete"
    }
  }

  # Belt-and-braces: orphan multipart uploads should also be cleaned
  # up rather than lingering as billable fragments.
  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }
}

resource "google_storage_bucket_iam_member" "appspot_audio_reader" {
  bucket = google_storage_bucket.bible_audio.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${local.appspot_email}"
}

resource "google_storage_bucket_iam_member" "audio_generator_writer" {
  bucket = google_storage_bucket.bible_audio.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.audio_generator.email}"
}
