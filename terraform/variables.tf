variable "project_id" {
  description = "GCP project ID to deploy PromptWars into."
  type        = string

  validation {
    condition     = length(var.project_id) > 0
    error_message = "project_id must not be empty."
  }
}

variable "region" {
  description = "GCP region for all resources."
  type        = string
  default     = "us-central1"
}

variable "redis_size" {
  description = "Memorystore Redis instance capacity in GB."
  type        = number
  default     = 1

  validation {
    condition     = var.redis_size >= 1
    error_message = "redis_size must be at least 1 GB."
  }
}

variable "min_instances" {
  description = "Minimum number of Cloud Run instances (0 = scale to zero)."
  type        = number
  default     = 0

  validation {
    condition     = var.min_instances >= 0
    error_message = "min_instances must be 0 or greater."
  }
}

variable "max_instances" {
  description = "Maximum number of Cloud Run instances."
  type        = number
  default     = 20

  validation {
    condition     = var.max_instances >= 1
    error_message = "max_instances must be at least 1."
  }
}

variable "cache_ttl" {
  description = "Redis cache TTL in seconds for scored results."
  type        = number
  default     = 3600

  validation {
    condition     = var.cache_ttl > 0
    error_message = "cache_ttl must be a positive integer."
  }
}
