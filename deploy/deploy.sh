#!/bin/bash
#
# AWS Deployment Script for Stock Screener
# ==========================================
# Sets up: ECR, ECS Fargate, EventBridge daily schedule, S3, IAM
#
# Prerequisites:
#   - AWS CLI installed and configured (aws configure)
#   - Docker installed and running
#   - An SES-verified email address
#
# Usage:
#   chmod +x deploy/deploy.sh
#   ./deploy/deploy.sh
#

set -e

# ============================================================
# CONFIGURATION — Edit these before running
# ============================================================
AWS_REGION="${AWS_REGION:-us-east-1}"
APP_NAME="stock-screener"
ECR_REPO="${APP_NAME}"
ECS_CLUSTER="${APP_NAME}-cluster"
TASK_FAMILY="${APP_NAME}-task"
S3_BUCKET="${APP_NAME}-reports-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo 'ACCOUNT_ID')"
SES_FROM_EMAIL="${SES_FROM_EMAIL:-}"     # Set this: your verified SES email
SES_TO_EMAIL="${SES_TO_EMAIL:-}"         # Set this: where to receive reports
SCHEDULE_EXPRESSION="rate(1 day)"        # Daily. Use "rate(12 hours)" for twice daily
TASK_CPU="1024"                          # 1 vCPU
TASK_MEMORY="4096"                       # 4 GB (FinBERT needs ~2GB)

echo "============================================"
echo "  Stock Screener — AWS Deployment"
echo "============================================"
echo ""
echo "  Region:    $AWS_REGION"
echo "  App:       $APP_NAME"
echo "  S3 Bucket: $S3_BUCKET"
echo "  Schedule:  $SCHEDULE_EXPRESSION"
echo ""

# Check prerequisites
if ! command -v aws &> /dev/null; then
    echo "ERROR: AWS CLI not found. Install: https://aws.amazon.com/cli/"
    exit 1
fi

if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker not found. Install: https://docs.docker.com/get-docker/"
    exit 1
fi

if ! aws sts get-caller-identity &> /dev/null; then
    echo "ERROR: AWS CLI not configured. Run: aws configure"
    exit 1
fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "AWS Account: $ACCOUNT_ID"
echo ""

# ============================================================
# Step 1: Create S3 bucket for reports
# ============================================================
echo ">>> Step 1: Creating S3 bucket..."
if aws s3 ls "s3://${S3_BUCKET}" 2>/dev/null; then
    echo "  Bucket already exists: ${S3_BUCKET}"
else
    aws s3 mb "s3://${S3_BUCKET}" --region "${AWS_REGION}"
    echo "  Created bucket: ${S3_BUCKET}"
fi

# ============================================================
# Step 2: Create ECR repository
# ============================================================
echo ""
echo ">>> Step 2: Creating ECR repository..."
if aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" &>/dev/null; then
    echo "  Repository already exists: ${ECR_REPO}"
else
    aws ecr create-repository \
        --repository-name "${ECR_REPO}" \
        --region "${AWS_REGION}" \
        --image-scanning-configuration scanOnPush=true
    echo "  Created repository: ${ECR_REPO}"
fi

# ============================================================
# Step 3: Build and push Docker image
# ============================================================
echo ""
echo ">>> Step 3: Building Docker image..."
cd "$(dirname "$0")/.."

docker build -t "${APP_NAME}:latest" .

echo "  Logging into ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "  Tagging and pushing image..."
docker tag "${APP_NAME}:latest" "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"
echo "  Image pushed: ${ECR_URI}:latest"

# ============================================================
# Step 4: Create IAM execution role for ECS
# ============================================================
echo ""
echo ">>> Step 4: Creating IAM roles..."

EXEC_ROLE_NAME="${APP_NAME}-ecs-exec-role"
TASK_ROLE_NAME="${APP_NAME}-ecs-task-role"

# ECS execution role (pull images + send logs)
if aws iam get-role --role-name "${EXEC_ROLE_NAME}" &>/dev/null; then
    echo "  Execution role exists: ${EXEC_ROLE_NAME}"
else
    aws iam create-role \
        --role-name "${EXEC_ROLE_NAME}" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }'
    aws iam attach-role-policy \
        --role-name "${EXEC_ROLE_NAME}" \
        --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
    echo "  Created execution role: ${EXEC_ROLE_NAME}"
fi

EXEC_ROLE_ARN=$(aws iam get-role --role-name "${EXEC_ROLE_NAME}" --query 'Role.Arn' --output text)

# ECS task role (S3 + SES access)
if aws iam get-role --role-name "${TASK_ROLE_NAME}" &>/dev/null; then
    echo "  Task role exists: ${TASK_ROLE_NAME}"
else
    aws iam create-role \
        --role-name "${TASK_ROLE_NAME}" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }'
    echo "  Created task role: ${TASK_ROLE_NAME}"
fi

TASK_ROLE_ARN=$(aws iam get-role --role-name "${TASK_ROLE_NAME}" --query 'Role.Arn' --output text)

# Attach S3 and SES policies to task role
POLICY_NAME="${APP_NAME}-task-policy"
POLICY_DOC=$(cat <<POLICY
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:PutObject", "s3:GetObject"],
            "Resource": "arn:aws:s3:::${S3_BUCKET}/*"
        },
        {
            "Effect": "Allow",
            "Action": ["ses:SendEmail", "ses:SendRawEmail"],
            "Resource": "*"
        }
    ]
}
POLICY
)

aws iam put-role-policy \
    --role-name "${TASK_ROLE_NAME}" \
    --policy-name "${POLICY_NAME}" \
    --policy-document "${POLICY_DOC}"
echo "  Attached S3 + SES policy to task role"

# ============================================================
# Step 5: Create ECS cluster
# ============================================================
echo ""
echo ">>> Step 5: Creating ECS cluster..."
if aws ecs describe-clusters --clusters "${ECS_CLUSTER}" --region "${AWS_REGION}" \
    --query 'clusters[?status==`ACTIVE`].clusterName' --output text | grep -q "${ECS_CLUSTER}"; then
    echo "  Cluster already exists: ${ECS_CLUSTER}"
else
    aws ecs create-cluster --cluster-name "${ECS_CLUSTER}" --region "${AWS_REGION}"
    echo "  Created cluster: ${ECS_CLUSTER}"
fi

# ============================================================
# Step 6: Create CloudWatch log group
# ============================================================
echo ""
echo ">>> Step 6: Creating CloudWatch log group..."
LOG_GROUP="/ecs/${APP_NAME}"
aws logs create-log-group --log-group-name "${LOG_GROUP}" --region "${AWS_REGION}" 2>/dev/null || true
echo "  Log group: ${LOG_GROUP}"

# ============================================================
# Step 7: Register ECS task definition
# ============================================================
echo ""
echo ">>> Step 7: Registering task definition..."

TASK_DEF=$(cat <<TASKDEF
{
    "family": "${TASK_FAMILY}",
    "networkMode": "awsvpc",
    "requiresCompatibilities": ["FARGATE"],
    "cpu": "${TASK_CPU}",
    "memory": "${TASK_MEMORY}",
    "executionRoleArn": "${EXEC_ROLE_ARN}",
    "taskRoleArn": "${TASK_ROLE_ARN}",
    "containerDefinitions": [
        {
            "name": "${APP_NAME}",
            "image": "${ECR_URI}:latest",
            "essential": true,
            "environment": [
                {"name": "S3_BUCKET", "value": "${S3_BUCKET}"},
                {"name": "SES_FROM_EMAIL", "value": "${SES_FROM_EMAIL}"},
                {"name": "SES_TO_EMAIL", "value": "${SES_TO_EMAIL}"},
                {"name": "AWS_REGION", "value": "${AWS_REGION}"}
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": "${LOG_GROUP}",
                    "awslogs-region": "${AWS_REGION}",
                    "awslogs-stream-prefix": "screener"
                }
            }
        }
    ]
}
TASKDEF
)

echo "${TASK_DEF}" > /tmp/task-def.json
aws ecs register-task-definition --cli-input-json file:///tmp/task-def.json --region "${AWS_REGION}" > /dev/null
echo "  Registered: ${TASK_FAMILY}"

# ============================================================
# Step 8: Get default VPC and subnets
# ============================================================
echo ""
echo ">>> Step 8: Getting VPC configuration..."
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" \
    --query 'Vpcs[0].VpcId' --output text --region "${AWS_REGION}")
echo "  VPC: ${VPC_ID}"

SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" \
    --query 'Subnets[*].SubnetId' --output text --region "${AWS_REGION}" | tr '\t' ',')
FIRST_SUBNET=$(echo "${SUBNETS}" | cut -d',' -f1)
echo "  Subnets: ${SUBNETS}"

SG_ID=$(aws ec2 describe-security-groups \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=default" \
    --query 'SecurityGroups[0].GroupId' --output text --region "${AWS_REGION}")
echo "  Security Group: ${SG_ID}"

# ============================================================
# Step 9: Create EventBridge rule for daily schedule
# ============================================================
echo ""
echo ">>> Step 9: Creating EventBridge schedule..."
RULE_NAME="${APP_NAME}-daily-schedule"

aws events put-rule \
    --name "${RULE_NAME}" \
    --schedule-expression "${SCHEDULE_EXPRESSION}" \
    --state ENABLED \
    --region "${AWS_REGION}" > /dev/null

# Create role for EventBridge to run ECS tasks
EVENTS_ROLE_NAME="${APP_NAME}-events-role"
if ! aws iam get-role --role-name "${EVENTS_ROLE_NAME}" &>/dev/null; then
    aws iam create-role \
        --role-name "${EVENTS_ROLE_NAME}" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Principal": {"Service": "events.amazonaws.com"},
                "Action": "sts:AssumeRole"
            }]
        }'
    aws iam put-role-policy \
        --role-name "${EVENTS_ROLE_NAME}" \
        --policy-name "ecs-run-task" \
        --policy-document '{
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": ["ecs:RunTask", "iam:PassRole"],
                "Resource": "*"
            }]
        }'
fi
EVENTS_ROLE_ARN=$(aws iam get-role --role-name "${EVENTS_ROLE_NAME}" --query 'Role.Arn' --output text)

# Get the latest task definition ARN
TASK_DEF_ARN=$(aws ecs describe-task-definition --task-definition "${TASK_FAMILY}" \
    --query 'taskDefinition.taskDefinitionArn' --output text --region "${AWS_REGION}")

# Set the ECS target
aws events put-targets \
    --rule "${RULE_NAME}" \
    --targets "[{
        \"Id\": \"${APP_NAME}-target\",
        \"Arn\": \"arn:aws:ecs:${AWS_REGION}:${ACCOUNT_ID}:cluster/${ECS_CLUSTER}\",
        \"RoleArn\": \"${EVENTS_ROLE_ARN}\",
        \"EcsParameters\": {
            \"TaskDefinitionArn\": \"${TASK_DEF_ARN}\",
            \"TaskCount\": 1,
            \"LaunchType\": \"FARGATE\",
            \"NetworkConfiguration\": {
                \"awsvpcConfiguration\": {
                    \"Subnets\": [\"${FIRST_SUBNET}\"],
                    \"SecurityGroups\": [\"${SG_ID}\"],
                    \"AssignPublicIp\": \"ENABLED\"
                }
            }
        }
    }]" \
    --region "${AWS_REGION}" > /dev/null

echo "  Schedule created: ${RULE_NAME} (${SCHEDULE_EXPRESSION})"

# ============================================================
# DONE
# ============================================================
echo ""
echo "============================================"
echo "  ✅ Deployment Complete!"
echo "============================================"
echo ""
echo "  Resources created:"
echo "    ECR:        ${ECR_URI}"
echo "    ECS:        ${ECS_CLUSTER} / ${TASK_FAMILY}"
echo "    S3:         s3://${S3_BUCKET}"
echo "    Schedule:   ${RULE_NAME} (${SCHEDULE_EXPRESSION})"
echo "    Logs:       ${LOG_GROUP}"
echo ""
echo "  Reports will be saved to:"
echo "    s3://${S3_BUCKET}/reports/YYYY/MM/DD/"
echo ""
if [ -n "${SES_FROM_EMAIL}" ]; then
    echo "  Emails will be sent from: ${SES_FROM_EMAIL}"
    echo "  Emails will be sent to:   ${SES_TO_EMAIL}"
else
    echo "  ⚠  Email not configured. Set SES_FROM_EMAIL and SES_TO_EMAIL"
    echo "     and re-run deploy to enable email reports."
fi
echo ""
echo "  To run manually:"
echo "    aws ecs run-task --cluster ${ECS_CLUSTER} \\"
echo "      --task-definition ${TASK_FAMILY} \\"
echo "      --launch-type FARGATE \\"
echo "      --network-configuration 'awsvpcConfiguration={subnets=[${FIRST_SUBNET}],securityGroups=[${SG_ID}],assignPublicIp=ENABLED}'"
echo ""
echo "  To check logs:"
echo "    aws logs tail ${LOG_GROUP} --follow"
echo ""
echo "  To tear down:"
echo "    ./deploy/teardown.sh"
echo ""
