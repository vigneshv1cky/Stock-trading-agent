#!/bin/bash
#
# AWS Deployment Script for Stock Screener (Production ALB Mode)
# ==========================================
# Sets up: ECR, ECS Fargate, S3, IAM, Load Balancer (ALB)
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
SERVICE_NAME="${APP_NAME}-service"
S3_BUCKET="${APP_NAME}-reports-$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo 'ACCOUNT_ID')"
TASK_CPU="1024"                          # 1 vCPU
TASK_MEMORY="4096"                       # 4 GB

echo "============================================"
echo "  Stock Screener — Production Deployment (ALB)"
echo "============================================"
echo ""
echo "  Region:    $AWS_REGION"
echo "  App:       $APP_NAME"
echo "  S3 Bucket: $S3_BUCKET"
echo ""

# Check prerequisites
if ! command -v aws &> /dev/null; then echo "ERROR: AWS CLI not found."; exit 1; fi
if ! command -v docker &> /dev/null; then echo "ERROR: Docker not found."; exit 1; fi

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

# ============================================================
# Step 1-2: S3 and ECR (Fast Checks)
# ============================================================
if ! aws s3 ls "s3://${S3_BUCKET}" 2>/dev/null; then aws s3 mb "s3://${S3_BUCKET}" --region "${AWS_REGION}"; fi
if ! aws ecr describe-repositories --repository-names "${ECR_REPO}" --region "${AWS_REGION}" &>/dev/null; then
    aws ecr create-repository --repository-name "${ECR_REPO}" --region "${AWS_REGION}"
fi

# ============================================================
# Step 3: Build and push Docker image
# ============================================================
echo ">>> Building and Pushing Docker Image..."
cd "$(dirname "$0")/.."
docker build --platform linux/amd64 -t "${APP_NAME}:latest" .
aws ecr get-login-password --region "${AWS_REGION}" | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
docker tag "${APP_NAME}:latest" "${ECR_URI}:latest"
docker push "${ECR_URI}:latest"

# ============================================================
# Step 4: Networking & Security Groups
# ============================================================
echo ">>> Configuring VPC and Security Groups..."
VPC_ID=$(aws ec2 describe-vpcs --filters "Name=isDefault,Values=true" --query 'Vpcs[0].VpcId' --output text --region "${AWS_REGION}")
SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" --query 'Subnets[*].SubnetId' --output text --region "${AWS_REGION}" | xargs | tr ' ' ',')
# Convert commas to space-separated list for AWS CLI arrays if needed, but for create-lb it takes space-separated
SUBNET_LIST=$(echo $SUBNETS | tr ',' ' ')

# ALB Security Group (Allows 80 from everywhere)
ALB_SG_NAME="${APP_NAME}-alb-sg"
if ! ALB_SG_ID=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=${ALB_SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" --query 'SecurityGroups[0].GroupId' --output text --region "${AWS_REGION}" 2>/dev/null) || [ "$ALB_SG_ID" == "None" ]; then
    ALB_SG_ID=$(aws ec2 create-security-group --group-name "${ALB_SG_NAME}" --description "ALB Inbound" --vpc-id "${VPC_ID}" --query 'GroupId' --output text --region "${AWS_REGION}")
    aws ec2 authorize-security-group-ingress --group-id "${ALB_SG_ID}" --protocol tcp --port 80 --cidr 0.0.0.0/0 --region "${AWS_REGION}"
fi

# Task Security Group (Allows 8080 from ALB only)
TASK_SG_NAME="${APP_NAME}-task-sg"
if ! TASK_SG_ID=$(aws ec2 describe-security-groups --filters "Name=group-name,Values=${TASK_SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" --query 'SecurityGroups[0].GroupId' --output text --region "${AWS_REGION}" 2>/dev/null) || [ "$TASK_SG_ID" == "None" ]; then
    TASK_SG_ID=$(aws ec2 create-security-group --group-name "${TASK_SG_NAME}" --description "Task Inbound from ALB" --vpc-id "${VPC_ID}" --query 'GroupId' --output text --region "${AWS_REGION}")
    aws ec2 authorize-security-group-ingress --group-id "${TASK_SG_ID}" --protocol tcp --port 8080 --source-group "${ALB_SG_ID}" --region "${AWS_REGION}"
fi

# ============================================================
# Step 5: Load Balancer & Target Group
# ============================================================
echo ">>> Managing Application Load Balancer..."
TG_NAME="${APP_NAME}-tg"
if ! TG_ARN=$(aws elbv2 describe-target-groups --names "${TG_NAME}" --query 'TargetGroups[0].TargetGroupArn' --output text --region "${AWS_REGION}" 2>/dev/null); then
    TG_ARN=$(aws elbv2 create-target-group --name "${TG_NAME}" --protocol HTTP --port 8080 --vpc-id "${VPC_ID}" --target-type ip --health-check-path "/health" --query 'TargetGroups[0].TargetGroupArn' --output text --region "${AWS_REGION}")
fi

ALB_NAME="${APP_NAME}-alb"
if ! ALB_ARN=$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].LoadBalancerArn' --output text --region "${AWS_REGION}" 2>/dev/null); then
    ALB_ARN=$(aws elbv2 create-load-balancer --name "${ALB_NAME}" --subnets $SUBNET_LIST --security-groups "${ALB_SG_ID}" --query 'LoadBalancers[0].LoadBalancerArn' --output text --region "${AWS_REGION}")
    aws elbv2 create-listener --load-balancer-arn "${ALB_ARN}" --protocol HTTP --port 80 --default-actions Type=forward,TargetGroupArn="${TG_ARN}" --region "${AWS_REGION}" > /dev/null
fi
ALB_DNS=$(aws elbv2 describe-load-balancers --names "${ALB_NAME}" --query 'LoadBalancers[0].DNSName' --output text --region "${AWS_REGION}")

# ============================================================
# Step 6: IAM Roles
# ============================================================
EXEC_ROLE_NAME="${APP_NAME}-ecs-exec-role"
TASK_ROLE_NAME="${APP_NAME}-ecs-task-role"

if ! aws iam get-role --role-name "${EXEC_ROLE_NAME}" &>/dev/null; then
    aws iam create-role --role-name "${EXEC_ROLE_NAME}" --assume-role-policy-document '{"Version": "2012-10-17","Statement": [{"Effect": "Allow","Principal": {"Service": "ecs-tasks.amazonaws.com"},"Action": "sts:AssumeRole"}]}'
    aws iam attach-role-policy --role-name "${EXEC_ROLE_NAME}" --policy-arn "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
fi
EXEC_ROLE_ARN=$(aws iam get-role --role-name "${EXEC_ROLE_NAME}" --query 'Role.Arn' --output text)

if ! aws iam get-role --role-name "${TASK_ROLE_NAME}" &>/dev/null; then
    aws iam create-role --role-name "${TASK_ROLE_NAME}" --assume-role-policy-document '{"Version": "2012-10-17","Statement": [{"Effect": "Allow","Principal": {"Service": "ecs-tasks.amazonaws.com"},"Action": "sts:AssumeRole"}]}'
fi
TASK_ROLE_ARN=$(aws iam get-role --role-name "${TASK_ROLE_NAME}" --query 'Role.Arn' --output text)

POLICY_DOC=$(cat <<POLICY
{
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"], "Resource": "arn:aws:s3:::${S3_BUCKET}/*"},
        {"Effect": "Allow", "Action": ["ses:SendEmail", "ses:SendRawEmail"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["dynamodb:ListTables", "dynamodb:DescribeTable"], "Resource": "*"},
        {"Effect": "Allow", "Action": ["dynamodb:CreateTable","dynamodb:DescribeTable","dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:DeleteItem","dynamodb:Query","dynamodb:Scan","dynamodb:BatchWriteItem"], "Resource": "arn:aws:dynamodb:${AWS_REGION}:*:table/PROD_*"}
    ]
}
POLICY
)
aws iam put-role-policy --role-name "${TASK_ROLE_NAME}" --policy-name "${APP_NAME}-task-policy" --policy-document "${POLICY_DOC}"

# ============================================================
# Step 7: Cluster and Task Definition
# ============================================================
aws ecs create-cluster --cluster-name "${ECS_CLUSTER}" --region "${AWS_REGION}" > /dev/null || true
aws logs create-log-group --log-group-name "/ecs/${APP_NAME}" --region "${AWS_REGION}" 2>/dev/null || true

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
            "portMappings": [{"containerPort": 8080, "hostPort": 8080, "protocol": "tcp"}],
            "environment": [
                {"name": "ENV", "value": "PROD"},
                {"name": "ALPACA_API_KEY", "value": "${ALPACA_API_KEY}"},
                {"name": "ALPACA_SECRET_KEY", "value": "${ALPACA_SECRET_KEY}"},
                {"name": "S3_BUCKET", "value": "${S3_BUCKET}"},
                {"name": "AWS_REGION", "value": "${AWS_REGION}"},
                {"name": "ADMIN_USERNAME", "value": "${ADMIN_USERNAME}"},
                {"name": "ADMIN_PASSWORD", "value": "${ADMIN_PASSWORD}"}
            ],
            "logConfiguration": {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": "/ecs/${APP_NAME}",
                    "awslogs-region": "${AWS_REGION}",
                    "awslogs-stream-prefix": "screener"
                }
            }
        }
    ]
}
TASKDEF
)
aws ecs register-task-definition --cli-input-json "$TASK_DEF" --region "${AWS_REGION}" > /dev/null

# ============================================================
# Step 8: ECS Service
# ============================================================
echo ">>> Managing ECS Service..."
if aws ecs describe-services --cluster "${ECS_CLUSTER}" --services "${SERVICE_NAME}" --region "${AWS_REGION}" --query 'services[?status==`ACTIVE`].serviceName' --output text | grep -q "${SERVICE_NAME}"; then
    aws ecs update-service --cluster "${ECS_CLUSTER}" --service "${SERVICE_NAME}" --task-definition "${TASK_FAMILY}" --force-new-deployment --region "${AWS_REGION}" > /dev/null
else
    aws ecs create-service --cluster "${ECS_CLUSTER}" --service-name "${SERVICE_NAME}" --task-definition "${TASK_FAMILY}" --desired-count 1 --launch-type FARGATE \
        --load-balancers "targetGroupArn=${TG_ARN},containerName=${APP_NAME},containerPort=8080" \
        --network-configuration "awsvpcConfiguration={subnets=[$(echo $SUBNETS | cut -d',' -f1,2)],securityGroups=[${TASK_SG_ID}],assignPublicIp=ENABLED}" \
        --region "${AWS_REGION}" > /dev/null
fi

echo ""
echo "✅ Deployment Complete!"
echo "PUBLIC URL: http://${ALB_DNS}"
echo "Note: It may take 2-3 minutes for the Load Balancer to become active."
