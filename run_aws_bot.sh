#!/bin/bash

# 1. Set environment variables from .env file (Alpaca, AWS SSO, etc.)
if [ -f ".env" ]; then
    echo "Loading configuration from .env..."
    # 'set -a' exports all variables defined in the sourced file
    set -a
    source .env
    set +a
fi

# Ensure Alpaca keys were found (from either source)
if [ -z "$ALPACA_API_KEY" ]; then
    echo "ERROR: ALPACA_API_KEY not found in .env or environment"
    exit 1
fi

# 3. Run the deployment (Build image, push to ECR, update AWS config)
echo "Deploying to AWS..."
./deploy/deploy.sh

# 4. Start the bot cycle in Fargate
echo "Triggering cloud bot execution..."
aws ecs run-task --cluster stock-screener-cluster \
  --task-definition stock-screener-task \
  --launch-type FARGATE \
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-005647977afe24ae7],securityGroups=[sg-0fa5426cf64d24526],assignPublicIp=ENABLED}' \
  --region us-east-1

echo ""
echo "Bot is starting in the cloud! Use this command to watch logs:"
echo "aws logs tail /ecs/stock-screener --region us-east-1 --follow"
