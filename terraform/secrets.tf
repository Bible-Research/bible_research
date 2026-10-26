# Secrets are managed in GCP; Terraform only reads metadata
# (matches remote state).
data "google_secret_manager_secret" "app" {
  for_each = toset([
    "DJANGO_SECRET_KEY",
    "DBT_KEY",
    "ESV_KEY",
    "DATABASE_URL",
    "DB_SSL_CERT",
    "SENTRY_DSN",
  ])

  project   = var.project_id
  secret_id = each.value
}
