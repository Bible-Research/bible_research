variable "project_id" {
  type    = string
  default = "bible-research-489314"
}

# App Engine and Artifact Registry use europe-west3; audio
# resources follow Artifact Registry (see main.tf
# app_engine_region).
variable "region" {
  type        = string
  default     = "europe-west3"
  description = "Default provider region (see comment)."
}

# GitHub repository allowed to authenticate via WIF.
variable "github_repo" {
  type    = string
  default = "Bible-Research/bible_research"
}

# Allowed origins for GCS bucket CORS policy. The default covers
# the prod SPA, PR previews, the Android webview and local dev;
# override in tfvars or via -var for other deployments.
# https://bibleresearch.vercel.app — production SPA;
# https://*-industr-analysers-projects.vercel.app — Vercel PR
#   previews; the wildcard spans every project in the team
#   (GCS allows one * per origin, and signed URLs stay
#   bearer-token gated);
# https://localhost — Capacitor Android webview (androidScheme);
# http://localhost:5173 — vite dev server (GCS matches the port
#   literally; other dev ports fall back to streaming).
variable "cors_origins" {
  type = list(string)
  default = [
    "https://bibleresearch.vercel.app",
    "https://*-industr-analysers-projects.vercel.app",
    "https://localhost",
    "http://localhost:5173",
  ]
  description = "Origins allowed to fetch signed GCS URLs directly."
}

# Email receiving Cloud Monitoring alerts (monitoring.tf). Kept empty
# so the real address stays out of the repo: set the
# ALERT_EMAIL_ADDRESS repo variable — terraform.yml maps it to
# TF_VAR_alert_email_address — or use terraform.tfvars locally. An
# empty value disables the alert resources entirely.
variable "alert_email_address" {
  type        = string
  default     = ""
  description = "Email for monitoring alerts; empty disables them."
}

# Bytes threshold for the bible_audio size alert (monitoring.tf).
# 5 GiB: one full Bible of generated MP3s is on the order of a few
# GB, so exceeding this means something is wrong.
variable "audio_bucket_size_alert_bytes" {
  type        = number
  default     = 5368709120
  description = "Alert threshold (bytes) for bible_audio total_bytes."
}
