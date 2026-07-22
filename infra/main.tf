terraform {
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# --- Backend Service ---
resource "google_cloud_run_v2_service" "backend" {
  name     = "opal-backend"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    # Session affinity keeps WebSocket connections on the same instance
    session_affinity = true

    # Gen2 uses a full Linux kernel (not gVisor) — required for Chromium
    execution_environment = "EXECUTION_ENVIRONMENT_GEN2"

    # Limit concurrency — each session runs Chromium + browser-use
    max_instance_request_concurrency = 2

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    # Request timeout: 1 hour (browser-use agents can run long)
    timeout = "3600s"

    containers {
      image = var.backend_image

      ports {
        container_port = 8080
      }

      # Chromium + browser-use needs real resources
      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
        cpu_idle          = false  # Keep CPU active — Chromium dies when throttled
        startup_cpu_boost = true   # Extra CPU during cold start
      }

      # Startup probe — Playwright/Chromium init is slow
      startup_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 12  # up to 60s to start
        timeout_seconds       = 3
      }

      # Liveness probe
      liveness_probe {
        http_get {
          path = "/health"
          port = 8080
        }
        period_seconds    = 30
        failure_threshold = 3
        timeout_seconds   = 5
      }

      # Environment variables for your application
      env {
        name  = "GOOGLE_API_KEY"
        value = var.google_api_key
      }
      env {
        name  = "GROQ_API_KEY"
        value = var.groq_api_key
      }
      env {
        name  = "GEMINI_MODEL"
        value = var.gemini_model
      }
      env {
        name  = "STT_MODEL"
        value = var.stt_model
      }
      env {
        name  = "TTS_VOICE"
        value = var.tts_voice
      }
    }
  }
}

# Allow public access to Backend
resource "google_cloud_run_service_iam_member" "backend_public" {
  service  = google_cloud_run_v2_service.backend.name
  location = google_cloud_run_v2_service.backend.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# --- Frontend Service ---
resource "google_cloud_run_v2_service" "frontend" {
  name     = "opal-frontend"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.frontend_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "256Mi"
        }
        cpu_idle = true
      }

      # Pass the backend URL for runtime injection by entrypoint.sh
      env {
        name  = "BACKEND_URL"
        value = google_cloud_run_v2_service.backend.uri
      }
    }
  }
}

# Allow public access to Frontend
resource "google_cloud_run_service_iam_member" "frontend_public" {
  service  = google_cloud_run_v2_service.frontend.name
  location = google_cloud_run_v2_service.frontend.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}