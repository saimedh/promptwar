output "scoring_api_url" {
  description = "Public URL of the PromptWars Cloud Run scoring service."
  value       = google_cloud_run_v2_service.prompt_scoring_api.uri
}

output "redis_host" {
  description = "Private IP address of the Memorystore Redis instance."
  value       = google_redis_instance.prompt_cache.host
}

output "pubsub_topic_id" {
  description = "Fully-qualified Pub/Sub topic ID for score events."
  value       = google_pubsub_topic.score_events.id
}
