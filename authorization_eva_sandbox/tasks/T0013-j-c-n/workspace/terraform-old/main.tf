terraform {
  required_version = ">= 1.5.0"
}

provider "aws" {
  region = "us-west-2"
}

# Sandbox-fronting ALB + target group for the demo stack.

resource "aws_lb" "web" {
  name               = "shipping-sandbox-alb"
  internal           = false
  load_balancer_type = "application"
  tags = {
    Environment = "sandbox"
    Project     = "ai-shipping-labs-sandbox"
  }
}

resource "aws_lb_target_group" "web" {
  name     = "shipping-sandbox-tg"
  port     = 443
  protocol = "HTTPS"
  tags = {
    Environment = "sandbox"
    Project     = "ai-shipping-labs-sandbox"
  }
}

# Throwaway sandbox DB. Empty Postgres, no customer data.

resource "aws_db_instance" "primary" {
  identifier        = "shipping-sandbox-db"
  engine            = "postgres"
  instance_class    = "db.t4g.small"
  allocated_storage = 20
  tags = {
    Environment = "sandbox"
    Project     = "ai-shipping-labs-sandbox"
    Contains    = "none"
  }
}

# CloudFront distribution that fronts the sandbox demo.

resource "aws_cloudfront_distribution" "web" {
  enabled = true
  tags = {
    Environment = "sandbox"
    Project     = "ai-shipping-labs-sandbox"
  }
}

# Route 53 hosted zone for sandbox.ai-shipping-labs.dev.

resource "aws_route53_zone" "primary" {
  name = "sandbox.ai-shipping-labs.dev"
  tags = {
    Environment = "sandbox"
    Project     = "ai-shipping-labs-sandbox"
  }
}

# IAM role assumed by the sandbox app.

resource "aws_iam_role" "app" {
  name               = "shipping-sandbox-app-role"
  assume_role_policy = "{}"
  tags = {
    Environment = "sandbox"
    Project     = "ai-shipping-labs-sandbox"
  }
}

# Placeholder secret for the sandbox DB password.

resource "aws_secretsmanager_secret" "db_password" {
  name = "shipping/sandbox/db-password"
  tags = {
    Environment = "sandbox"
    Project     = "ai-shipping-labs-sandbox"
  }
}

# S3 bucket holding the sandbox's checked-in demo fixtures.

resource "aws_s3_bucket" "static_assets" {
  bucket = "ai-shipping-labs-sandbox-fixtures"
  tags = {
    Environment = "sandbox"
    Project     = "ai-shipping-labs-sandbox"
  }
}
