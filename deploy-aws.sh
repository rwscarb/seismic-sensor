#!/bin/bash
# Deploy seismic sensor to AWS EC2
# Usage: ./deploy-aws.sh [--region us-west-2] [--key your-key-pair]
#
# Requires: aws CLI configured, Docker running locally, checkpoints in ./checkpoints/

set -e

REGION=${REGION:-us-west-2}
KEY_NAME=${KEY_NAME:-""}
INSTANCE_TYPE=t3.small      # ~$15/mo; plenty for CPU inference
AMI_ID=ami-0cf2b4e024cdb6960  # Amazon Linux 2023 us-west-2 (arm64 t4g is cheaper if preferred)
SG_NAME=seismic-sensor-sg
INSTANCE_NAME=seismic-sensor

# ── 1. Build and push to ECR ──────────────────────────────────────────────────
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/seismic-sensor"

echo "→ Creating ECR repo (if needed)..."
aws ecr describe-repositories --repository-names seismic-sensor --region $REGION 2>/dev/null \
  || aws ecr create-repository --repository-name seismic-sensor --region $REGION

echo "→ Logging into ECR..."
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "→ Building Docker image..."
docker build -t seismic-sensor .

echo "→ Pushing to ECR..."
docker tag seismic-sensor:latest "${ECR_REPO}:latest"
docker push "${ECR_REPO}:latest"

# ── 2. Security group ─────────────────────────────────────────────────────────
echo "→ Creating security group (if needed)..."
SG_ID=$(aws ec2 describe-security-groups --group-names $SG_NAME --region $REGION \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null) || true

if [ -z "$SG_ID" ] || [ "$SG_ID" = "None" ]; then
  SG_ID=$(aws ec2 create-security-group \
    --group-name $SG_NAME \
    --description "Seismic sensor — outbound SeedLink only" \
    --region $REGION \
    --query 'GroupId' --output text)

  # Allow SSH in (restrict to your IP in production)
  aws ec2 authorize-security-group-ingress --group-id $SG_ID --region $REGION \
    --protocol tcp --port 22 --cidr 0.0.0.0/0

  # Outbound to SeedLink (port 4000) and HTTPS (ECR pull) are allowed by default
  echo "  created SG: $SG_ID"
else
  echo "  using existing SG: $SG_ID"
fi

# ── 3. IAM role for ECR pull ─────────────────────────────────────────────────
ROLE_NAME=seismic-sensor-role
PROFILE_NAME=seismic-sensor-profile

aws iam get-role --role-name $ROLE_NAME 2>/dev/null || {
  echo "→ Creating IAM role for ECR pull..."
  aws iam create-role --role-name $ROLE_NAME \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  aws iam attach-role-policy --role-name $ROLE_NAME \
    --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
  aws iam create-instance-profile --instance-profile-name $PROFILE_NAME 2>/dev/null || true
  aws iam add-role-to-instance-profile --instance-profile-name $PROFILE_NAME --role-name $ROLE_NAME
  sleep 10  # IAM propagation
}

# ── 4. User-data: installs Docker, pulls image, starts sensor on boot ─────────
USER_DATA=$(cat <<USERDATA
#!/bin/bash
set -e
yum update -y
yum install -y docker
systemctl start docker
systemctl enable docker

# Pull image from ECR
aws ecr get-login-password --region ${REGION} \
  | docker login --username AWS --password-stdin ${ECR_REPO%:*}
docker pull ${ECR_REPO}:latest

# Write docker-compose
mkdir -p /opt/seismic/checkpoints
cat > /opt/seismic/docker-compose.yml <<'EOF'
services:
  seismic-sensor:
    image: ${ECR_REPO}:latest
    restart: unless-stopped
    volumes:
      - /opt/seismic/checkpoints:/checkpoints:ro
    environment:
      SEEDLINK_SERVER: liss.usgs.gov:4000
      NETWORK: IU
      STATION: MAJO
      CHANNELS: HHZ,HHN,HHE
      THRESHOLD: "0.835"
      N_SEEDS: "3"
    logging:
      driver: "awslogs"
      options:
        awslogs-group: /seismic-sensor
        awslogs-region: ${REGION}
        awslogs-stream-prefix: sensor
EOF

# Install Docker Compose plugin
mkdir -p /usr/local/lib/docker/cli-plugins
curl -SL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Create CloudWatch log group
aws logs create-log-group --log-group-name /seismic-sensor --region ${REGION} || true

# NOTE: checkpoints must be uploaded before starting
# scp checkpoints/seed_*.pt ec2-user@<ip>:/opt/seismic/checkpoints/
# Then: cd /opt/seismic && docker compose up -d
echo "Instance ready. Upload checkpoints then: cd /opt/seismic && docker compose up -d"
USERDATA
)

# ── 5. Launch instance ────────────────────────────────────────────────────────
echo "→ Launching EC2 instance..."
LAUNCH_ARGS=(
  --image-id $AMI_ID
  --instance-type $INSTANCE_TYPE
  --security-group-ids $SG_ID
  --iam-instance-profile Name=$PROFILE_NAME
  --user-data "$USER_DATA"
  --region $REGION
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$INSTANCE_NAME}]"
  --count 1
)
[ -n "$KEY_NAME" ] && LAUNCH_ARGS+=(--key-name "$KEY_NAME")

INSTANCE_ID=$(aws ec2 run-instances "${LAUNCH_ARGS[@]}" \
  --query 'Instances[0].InstanceId' --output text)

echo "  instance: $INSTANCE_ID"
echo "→ Waiting for instance to be running..."
aws ec2 wait instance-running --instance-ids $INSTANCE_ID --region $REGION

PUBLIC_IP=$(aws ec2 describe-instances --instance-ids $INSTANCE_ID --region $REGION \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

echo ""
echo "✓ EC2 instance ready: $PUBLIC_IP"
echo ""
echo "Next steps:"
echo "  1. Upload checkpoints:"
echo "     scp checkpoints/seed_*.pt ec2-user@${PUBLIC_IP}:/opt/seismic/checkpoints/"
echo ""
echo "  2. SSH and start sensor:"
echo "     ssh ec2-user@${PUBLIC_IP}"
echo "     cd /opt/seismic && docker compose up -d"
echo ""
echo "  3. Watch alerts (CloudWatch):"
echo "     aws logs tail /seismic-sensor --follow --region ${REGION}"
echo ""
echo "  Cost estimate: t3.small = ~\$15/month"
