variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "bucket_name" {
  description = "S3 bucket name for static assets"
  type        = string
  default     = "seismic-fib896-assets"
}
