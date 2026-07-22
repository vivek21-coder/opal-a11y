output "backend_url" {
  description = "URL of the backend service"
  value       = google_cloud_run_v2_service.backend.uri
}

output "frontend_url" {
  description = "URL of the frontend service"
  value       = google_cloud_run_v2_service.frontend.uri
}