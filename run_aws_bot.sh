#!/bin/bash
set -e

echo "----------------------------------------------------"
echo "🚀 AWS WORKFLOW REMINDER:"
echo "1. aws sso login --profile vignesh-sso-profile"
echo "2. ./run_aws_bot.sh"
echo "----------------------------------------------------"
echo ""

# 1. Set environment variables from .env file (Alpaca, AWS SSO, etc.)
if [ -f ".env" ]; then
    echo "Loading configuration from .env..."
    # 'set -a' exports all variables defined in the sourced file
    set -a
    source .env
    set +a
fi

# 2. Set AWS Profile if provided in .env
if [ ! -z "$AWS_PROFILE" ]; then
    echo "Using AWS Profile: $AWS_PROFILE"
    export AWS_PROFILE=$AWS_PROFILE
fi

# Ensure Alpaca keys were found
if [ -z "$ALPACA_API_KEY" ]; then
    echo "ERROR: ALPACA_API_KEY not found in .env or environment"
    exit 1
fi

# 3. Run the deployment (Build image, push to ECR, update AWS config)
echo "Deploying to AWS..."
./deploy/deploy.sh

echo ""
echo "✅ Bot has been updated in the cloud!"
echo "The ECS Service will automatically spin up 1 healthy task (Desired Count: 1)."
echo "Use this command to watch live logs:"
if [ ! -z "$AWS_PROFILE" ]; then
    echo "aws logs tail /ecs/stock-screener --region us-east-1 --follow --profile $AWS_PROFILE"
else
    echo "aws logs tail /ecs/stock-screener --region us-east-1 --follow"
fi
