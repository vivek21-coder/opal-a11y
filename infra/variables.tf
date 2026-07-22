variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for Cloud Run services"
  type        = string
  default     = "europe-west2"
}

variable "google_api_key" {
  description = "Google AI Studio API key for Gemini"
  type        = string
  sensitive   = true
}

variable "groq_api_key" {
  description = "Groq API key for Whisper STT"
  type        = string
  sensitive   = true
}

variable "gemini_model" {
  description = "Gemini model name"
  type        = string
  default     = "gemini-2.5-flash"
}

variable "stt_model" {
  description = "Groq Whisper STT model"
  type        = string
  default     = "whisper-large-v3-turbo"
}

variable "tts_voice" {
  description = "Edge TTS voice name"
  type        = string
  default     = "en-US-AriaNeural"
}

variable "backend_image" {
  description = "Docker image for the backend (e.g. europe-west2-docker.pkg.dev/PROJECT/opal/backend:latest)"
  type        = string
}

variable "frontend_image" {
  description = "Docker image for the frontend (e.g. europe-west2-docker.pkg.dev/PROJECT/opal/frontend:latest)"
  type        = string
}
