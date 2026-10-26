# Secrets are managed in GCP; Terraform only reads metadata (matches
# remote state). All app secrets are consolidated into a single
# ``env-file`` secret holding the full .env contents — settings.py
# loads it via DOTENV injection (Cloud Run) or one get_secret() call
# (App Engine). The legacy per-key secrets can be deleted once every
# consumer has migrated.
data "google_secret_manager_secret" "env_file" {
  project   = var.project_id
  secret_id = "env-file"
}
