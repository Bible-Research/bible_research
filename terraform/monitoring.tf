# Email alert when the bible_audio bucket exceeds a size threshold.
#
# The lifecycle rules in audio_storage.tf bound *noncurrent* growth,
# but nothing reports total live size — a runaway TTS job would only
# be noticed via the bill. ``storage/total_bytes`` is a daily-sampled
# GAUGE metric, so the 86400s alignment period and 0s duration are
# intentional: the alert effectively re-evaluates once a day.
#
# Both resources are gated on ``alert_email_address`` so the real
# address stays out of the repo. Set the ALERT_EMAIL_ADDRESS repo
# variable (terraform.yml maps it to TF_VAR_alert_email_address) or
# use terraform.tfvars locally. With no address configured, nothing
# here is created and plan/apply stay green.

resource "google_monitoring_notification_channel" "email" {
  count = var.alert_email_address != "" ? 1 : 0

  display_name = "Infra alerts email"
  type         = "email"
  labels       = { email_address = var.alert_email_address }
}

resource "google_monitoring_alert_policy" "bible_audio_bucket_size" {
  count = var.alert_email_address != "" ? 1 : 0

  display_name = "bible-audio bucket size exceeds threshold"
  combiner     = "OR"

  conditions {
    display_name = "total_bytes over threshold"
    condition_threshold {
      filter = join(" AND ", [
        "resource.type = \"gcs_bucket\"",
        "resource.labels.bucket_name = " +
          "\"${google_storage_bucket.bible_audio.name}\"",
        "metric.type = " +
          "\"storage.googleapis.com/storage/total_bytes\"",
      ])
      comparison      = "COMPARISON_GT"
      threshold_value = var.audio_bucket_size_alert_bytes
      duration        = "0s"
      aggregations {
        alignment_period   = "86400s"
        per_series_aligner = "ALIGN_MAX"
      }
      trigger {
        count = 1
      }
    }
  }

  notification_channels =
    google_monitoring_notification_channel.email[*].id

  alert_strategy {
    auto_close = "604800s"
  }
}
