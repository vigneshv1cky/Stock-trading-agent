#!/bin/bash
#
# Tear down all AWS resources created by deploy.sh (Managed Service + ALB Mode)
#

set -e

AWS_REGION="${AWS_REGION:-us-east-1}"
APP_NAME="stock-screener"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "============================================"
echo "  Tearing down: ${APP_NAME} (Production ALB Mode)"
echo "============================================"
echo ""

read -p "Are you sure? This will delete the ALB, Service, and all history. (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

# 1. ECS Service
echo ">>> Deleting ECS Service..."
aws ecs update-service --cluster "${APP_NAME}-cluster" --service "${APP_NAME}-service" --desired-count 0 --region "${AWS_REGION}" 2>/dev/null || true
aws ecs delete-service --cluster "${APP_NAME}-cluster" --service "${APP_NAME}-service" --force --region "${AWS_REGION}" 2>/dev/null || true

# 2. ALB infrastructure
echo ">>> Deleting Load Balancer and Target Group..."
ALB_ARN=$(aws elbv2 describe-load-balancers --names "${APP_NAME}-alb" --query 'LoadBalancers[0].LoadBalancerArn' --output text --region "${AWS_REGION}" 2>/dev/null || echo "None")
if [ "$ALB_ARN" != "None" ]; then
    aws elbv2 delete-load-balancer --load-balancer-arn "${ALB_ARN}" --region "${AWS_REGION}"
    echo "  Deleting ALB... (this takes a moment)"
    aws elbv2 wait load-balancers-deleted --load-balancer-arns "${ALB_ARN}" --region "${AWS_REGION}"
fi

TG_ARN=$(aws elbv2 describe-target-groups --names "${APP_NAME}-tg" --query 'TargetGroups[0].TargetGroupArn' --output text --region "${AWS_REGION}" 2>/dev/null || echo "None")
if [ "$TG_ARN" != "None" ]; then
    aws elbv2 delete-target-group --target-group-arn "${TG_ARN}" --region "${AWS_REGION}"
fi

# 3. Security Groups
echo ">>> Deleting Security Groups..."
# We have to wait for the ALB and ENIs to detach
sleep 10
aws ec2 delete-security-group --group-name "${APP_NAME}-task-sg" --region "${AWS_REGION}" 2>/dev/null || true
aws ec2 delete-security-group --group-name "${APP_NAME}-alb-sg" --region "${AWS_REGION}" 2>/dev/null || true

# 4. Other resources (Same as before)
echo ">>> Removing EventBridge rule..."
aws events remove-targets --rule "${APP_NAME}-daily-schedule" --ids "${APP_NAME}-target" --region "${AWS_REGION}" 2>/dev/null || true
aws events delete-rule --name "${APP_NAME}-daily-schedule" --region "${AWS_REGION}" 2>/dev/null || true

echo ">>> Deleting ECS cluster..."
aws ecs delete-cluster --cluster "${APP_NAME}-cluster" --region "${AWS_REGION}" 2>/dev/null || true

echo ">>> Deregistering task definitions..."
TASK_DEFS=$(aws ecs list-task-definitions --family-prefix "${APP_NAME}-task" --query 'taskDefinitionArns' --output text --region "${AWS_REGION}" 2>/dev/null || true)
for td in ${TASK_DEFS}; do
    aws ecs deregister-task-definition --task-definition "${td}" --region "${AWS_REGION}" 2>/dev/null || true
done

echo ">>> Deleting ECR repository..."
aws ecr delete-repository --repository-name "${APP_NAME}" --force --region "${AWS_REGION}" 2>/dev/null || true

echo ">>> Deleting IAM roles..."
for ROLE in "${APP_NAME}-ecs-exec-role" "${APP_NAME}-ecs-task-role" "${APP_NAME}-events-role"; do
    POLICIES=$(aws iam list-attached-role-policies --role-name "${ROLE}" --query 'AttachedPolicies[*].PolicyArn' --output text 2>/dev/null || true)
    for p in ${POLICIES}; do
        aws iam detach-role-policy --role-name "${ROLE}" --policy-arn "${p}" 2>/dev/null || true
    done
    INLINE=$(aws iam list-role-policies --role-name "${ROLE}" --query 'PolicyNames' --output text 2>/dev/null || true)
    for p in ${INLINE}; do
        aws iam delete-role-policy --role-name "${ROLE}" --policy-name "${p}" 2>/dev/null || true
    done
    aws iam delete-role --role-name "${ROLE}" 2>/dev/null || true
done

echo ">>> Deleting CloudWatch log group..."
aws logs delete-log-group --log-group-name "/ecs/${APP_NAME}" --region "${AWS_REGION}" 2>/dev/null || true

echo ""
echo "✅ Teardown complete. (S3 Bucket and DynamoDB tables kept for safety)"
