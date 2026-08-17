output "cloudfront_url" {
  description = "CloudFront distribution URL for the fault GeoJSON"
  value       = "https://${aws_cloudfront_distribution.assets.domain_name}/gem_active_faults.geojson"
}

output "cloudfront_domain" {
  description = "CloudFront domain name"
  value       = aws_cloudfront_distribution.assets.domain_name
}

output "s3_bucket" {
  description = "S3 bucket name"
  value       = aws_s3_bucket.assets.bucket
}
