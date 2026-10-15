variable "project_id" {
  type    = string
  default = "bible-research-489314"
}

variable "region" {
  type        = string
  default     = "europe-west3"
  description = "Default provider region. App Engine and Artifact Registry use europe-west3; audio resources follow Artifact Registry (see main.tf app_engine_region)."
}

# GitHub repository allowed to authenticate via WIF.
variable "github_repo" {
  type    = string
  default = "Bible-Research/bible_research"
}

# Allowed origins for GCS bucket CORS policy.
# Override in tfvars or via -var for production deployments.
variable "cors_origins" {
  type        = list(string)
  default     = ["https://bible-research-489314.uc.r.appspot.com"]
  description = "Origins allowed to fetch signed image URLs directly from GCS."
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
