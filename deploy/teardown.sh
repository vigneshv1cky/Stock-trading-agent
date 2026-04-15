#!/bin/bash
#
# Tear down all AWS resources created by deploy.sh
#

set -e

AWS_REGION="${AWS_REGION:-us-east-1}"
APP_NAME="stock-screener"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

echo "============================================"
echo "  Tearing down: ${APP_NAME}"
echo "============================================"
echo ""

read -p "Are you sure? This will delete ALL resources. (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Cancelled."
    exit 0
fi

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
    # Detach managed policies
    POLICIES=$(aws iam list-attached-role-policies --role-name "${ROLE}" --query 'AttachedPolicies[*].PolicyArn' --output text 2>/dev/null || true)
    for p in ${POLICIES}; do
        aws iam detach-role-policy --role-name "${ROLE}" --policy-arn "${p}" 2>/dev/null || true
    done
    # Delete inline policies
    INLINE=$(aws iam list-role-policies --role-name "${ROLE}" --query 'PolicyNames' --output text 2>/dev/null || true)
    for p in ${INLINE}; do
        aws iam delete-role-policy --role-name "${ROLE}" --policy-name "${p}" 2>/dev/null || true
    done
    aws iam delete-role --role-name "${ROLE}" 2>/dev/null || true
done

echo ">>> Deleting CloudWatch log group..."
aws logs delete-log-group --log-group-name "/ecs/${APP_NAME}" --region "${AWS_REGION}" 2>/dev/null || true

echo ">>> S3 bucket NOT deleted (may contain reports you want to keep)"
echo "    To delete manually: aws s3 rb s3://${APP_NAME}-reports-${ACCOUNT_ID} --force"

echo ""
echo "✅ Teardown complete."
