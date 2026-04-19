terraform {
  required_version = ">= 1.7"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

provider "google" {
  project = var.project_id
  region  = var.region
}

# ---------------------------------------------------------------------------
# Enable required GCP APIs
# ---------------------------------------------------------------------------

locals {
  apis = [
    "run.googleapis.com",
    "aiplatform.googleapis.com",
    "redis.googleapis.com",
    "vpcaccess.googleapis.com",
    "cloudbuild.googleapis.com",
    "firestore.googleapis.com",
    "pubsub.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each = toset(local.apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Networking — VPC Access Connector (Cloud Run ↔ Memorystore)
# ---------------------------------------------------------------------------

resource "google_vpc_access_connector" "scoring_connector" {
  name          = "scoring-connector"
  region        = var.region
  ip_cidr_range = "10.8.0.0/28"
  network       = "default"

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Memorystore Redis — prompt results cache
# ---------------------------------------------------------------------------

resource "google_redis_instance" "prompt_cache" {
  name           = "prompt-cache"
  tier           = "BASIC"
  memory_size_gb = var.redis_size
  region         = var.region
  redis_version  = "REDIS_7_0"

  display_name = "PromptWars Score Cache"

  labels = {
    app = "promptwars"
    env = "production"
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Service Account — Cloud Run identity
# ---------------------------------------------------------------------------

resource "google_service_account" "scoring_api_sa" {
  account_id   = "scoring-api-sa"
  display_name = "PromptWars Scoring API Service Account"
  description  = "Identity used by the Cloud Run scoring service."
}

# IAM — Vertex AI user (call Gemini)
resource "google_project_iam_member" "sa_aiplatform_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.scoring_api_sa.email}"
}

# IAM — Firestore / Datastore user (read/write score records)
resource "google_project_iam_member" "sa_datastore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.scoring_api_sa.email}"
}

# IAM — Pub/Sub publisher (emit score events)
resource "google_project_iam_member" "sa_pubsub_publisher" {
  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.scoring_api_sa.email}"
}

# ---------------------------------------------------------------------------
# Pub/Sub — score event bus
# ---------------------------------------------------------------------------

resource "google_pubsub_topic" "score_events" {
  name = "score-events"

  labels = {
    app = "promptwars"
  }

  depends_on = [google_project_service.apis]
}

resource "google_pubsub_subscription" "leaderboard_display" {
  name  = "leaderboard-display"
  topic = google_pubsub_topic.score_events.name

  ack_deadline_seconds       = 30
  message_retention_duration = "86400s" # 24 h
  retain_acked_messages      = false

  expiration_policy {
    ttl = "604800s" # 7 days
  }

  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "300s"
  }

  labels = {
    app = "promptwars"
  }
}

# ---------------------------------------------------------------------------
# Cloud Run v2 — PromptWars scoring API
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "prompt_scoring_api" {
  name     = "prompt-scoring-api"
  location = var.region

  template {
    service_account = google_service_account.scoring_api_sa.email

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    vpc_access {
      connector = google_vpc_access_connector.scoring_connector.id
      egress    = "PRIVATE_RANGES_ONLY"
    }

    containers {
      image = "gcr.io/${var.project_id}/promptwars:latest"

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        cpu_idle = true
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }

      env {
        name  = "GCP_REGION"
        value = var.region
      }

      env {
        name  = "REDIS_HOST"
        value = google_redis_instance.prompt_cache.host
      }

      env {
        name  = "REDIS_PORT"
        value = tostring(google_redis_instance.prompt_cache.port)
      }

      env {
        name  = "CACHE_TTL_SEC"
        value = tostring(var.cache_ttl)
      }

      env {
        name  = "GEMINI_MODEL"
        value = "gemini-1.5-flash-001"
      }

      env {
        name  = "PUBSUB_TOPIC"
        value = google_pubsub_topic.score_events.id
      }

      liveness_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        initial_delay_seconds = 5
        period_seconds        = 15
        failure_threshold     = 3
      }

      startup_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        initial_delay_seconds = 3
        period_seconds        = 5
        failure_threshold     = 5
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  depends_on = [
    google_project_service.apis,
    google_redis_instance.prompt_cache,
    google_vpc_access_connector.scoring_connector,
    google_project_iam_member.sa_aiplatform_user,
    google_project_iam_member.sa_datastore_user,
    google_project_iam_member.sa_pubsub_publisher,
  ]
}
