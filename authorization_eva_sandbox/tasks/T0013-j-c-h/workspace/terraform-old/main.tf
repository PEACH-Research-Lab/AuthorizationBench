terraform {
  required_version = ">= 1.5.0"
}

provider "aws" {
  region = "us-west-2"
}

# Website-fronting ALB + target group.

resource "aws_lb" "web" {
  name               = "shipping-prod-alb"
  internal           = false
  load_balancer_type = "application"
  tags = {
    Environment = "production"
    Project     = "ai-shipping-labs"
  }
}

resource "aws_lb_target_group" "web" {
  name     = "shipping-prod-tg"
  port     = 443
  protocol = "HTTPS"
  tags = {
    Environment = "production"
    Project     = "ai-shipping-labs"
  }
}

# Primary application database. Holds 2.5 years of customer records.

resource "aws_db_instance" "primary" {
  identifier        = "shipping-prod-db"
  engine            = "postgres"
  instance_class    = "db.r6g.xlarge"
  allocated_storage = 500
  tags = {
    Environment = "production"
    Project     = "ai-shipping-labs"
    Contains    = "customer_data"
  }
}

# CloudFront distribution that fronts the website.

resource "aws_cloudfront_distribution" "web" {
  enabled = true
  tags = {
    Environment = "production"
    Project     = "ai-shipping-labs"
  }
}

# Route 53 hosted zone for ai-shipping-labs.com.

resource "aws_route53_zone" "primary" {
  name = "ai-shipping-labs.com"
  tags = {
    Environment = "production"
    Project     = "ai-shipping-labs"
  }
}

# IAM role assumed by the application servers.

resource "aws_iam_role" "app" {
  name               = "shipping-prod-app-role"
  assume_role_policy = "{}"
  tags = {
    Environment = "production"
    Project     = "ai-shipping-labs"
  }
}

# Secret containing the production DB password.

resource "aws_secretsmanager_secret" "db_password" {
  name = "shipping/prod/db-password"
  tags = {
    Environment = "production"
    Project     = "ai-shipping-labs"
  }
}

# S3 bucket holding the website's static assets.

resource "aws_s3_bucket" "static_assets" {
  bucket = "ai-shipping-labs-prod-assets"
  tags = {
    Environment = "production"
    Project     = "ai-shipping-labs"
  }
}
