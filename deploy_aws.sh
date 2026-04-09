#!/usr/bin/env bash
# ==============================================================================
# deploy_aws.sh — Brand Guardian AI: Clean AWS ECR → ECS Deployment
# ==============================================================================
#
# Usage:
#   ./deploy_aws.sh [options]
#
# Options:
#   --region          AWS region              (default: from .env / us-east-1)
#   --account-id      AWS Account ID         (auto-detected from STS if omitted)
#   --ecr-repo        ECR repository name     (default: brand-guardian)
#   --cluster         ECS cluster name        (default: brand-guardian-cluster)
#   --service         ECS service name        (default: brand-guardian-service)
#   --task-family     ECS task def family     (default: brand-guardian-task)
#   --tag             Image tag               (default: git short SHA)
#   --skip-push       Build only, skip ECR push
#   --help            Show this help
#
# Prerequisites:
#   - Docker daemon running
#   - AWS CLI v2 installed (aws --version)
#   - .env file in the project root with AWS_ACCESS_KEY_ID & AWS_SECRET_ACCESS_KEY
#
# ==============================================================================
set -euo pipefail

# ── Colour helpers ──────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }
banner()  { echo -e "\n${BOLD}${CYAN}══════════════════════════════════════════${NC}"; echo -e "${BOLD}${CYAN}  $*${NC}"; echo -e "${BOLD}${CYAN}══════════════════════════════════════════${NC}\n"; }

# ── Defaults ────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"

AWS_REGION=""
AWS_ACCOUNT_ID=""
ECR_REPO="brand-guardian"
ECS_CLUSTER="brand-guardian-cluster"
ECS_SERVICE="brand-guardian-service"
TASK_FAMILY="brand-guardian-task"
IMAGE_TAG=""
SKIP_PUSH=false

# ── Parse args ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --region)      AWS_REGION="$2";     shift 2 ;;
    --account-id)  AWS_ACCOUNT_ID="$2"; shift 2 ;;
    --ecr-repo)    ECR_REPO="$2";       shift 2 ;;
    --cluster)     ECS_CLUSTER="$2";    shift 2 ;;
    --service)     ECS_SERVICE="$2";    shift 2 ;;
    --task-family) TASK_FAMILY="$2";    shift 2 ;;
    --tag)         IMAGE_TAG="$2";      shift 2 ;;
    --skip-push)   SKIP_PUSH=true;      shift   ;;
    --help)
      head -35 "$0" | tail -30; exit 0 ;;
    *) error "Unknown argument: $1. Use --help for usage." ;;
  esac
done

# ── Load .env ────────────────────────────────────────────────────────────────
if [[ -f "$ENV_FILE" ]]; then
  info "Loading credentials from ${ENV_FILE} ..."
  # Export only the AWS keys from .env (avoids polluting env with Azure secrets)
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^(AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_REGION)' "$ENV_FILE" | sed 's/ *= */=/g')
  set +a
  success "AWS credentials loaded from .env"
else
  warn ".env file not found at ${ENV_FILE}. Relying on system AWS credentials."
fi

# ── Resolve region ───────────────────────────────────────────────────────────
if [[ -z "$AWS_REGION" ]]; then
  AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
  info "AWS region not specified. Using: ${AWS_REGION}"
fi
export AWS_DEFAULT_REGION="$AWS_REGION"

# ── Preflight checks ─────────────────────────────────────────────────────────
banner "Step 0 — Preflight checks"

command -v docker >/dev/null 2>&1 || error "Docker is not installed or not in PATH."
command -v aws    >/dev/null 2>&1 || error "AWS CLI v2 is not installed. Install from: https://aws.amazon.com/cli/"

docker info >/dev/null 2>&1 || error "Docker daemon is not running. Start Docker and retry."
success "Docker daemon is running."

# Verify AWS credentials
if ! aws sts get-caller-identity >/dev/null 2>&1; then
  error "AWS credentials are invalid or not set. Check AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY in .env"
fi

# Auto-detect Account ID if not provided
if [[ -z "$AWS_ACCOUNT_ID" ]]; then
  AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
  info "Auto-detected AWS Account ID: ${AWS_ACCOUNT_ID}"
fi

success "AWS credentials valid. Account: ${AWS_ACCOUNT_ID} | Region: ${AWS_REGION}"

# ── Set image tag ────────────────────────────────────────────────────────────
if [[ -z "$IMAGE_TAG" ]]; then
  IMAGE_TAG="$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo "latest")"
fi
ECR_BASE="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_IMAGE="${ECR_BASE}/${ECR_REPO}:${IMAGE_TAG}"
ECR_LATEST="${ECR_BASE}/${ECR_REPO}:latest"

info "Image will be tagged: ${ECR_IMAGE}"
info "               and also: ${ECR_LATEST}"

# ==============================================================================
# STEP 1: Ensure ECR repository exists
# ==============================================================================
banner "Step 1 — ECR Repository"

if aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null 2>&1; then
  success "ECR repository '${ECR_REPO}' already exists."
else
  info "Repository '${ECR_REPO}' not found. Creating ..."
  aws ecr create-repository \
    --repository-name "$ECR_REPO" \
    --region "$AWS_REGION" \
    --image-scanning-configuration scanOnPush=true \
    --image-tag-mutability MUTABLE \
    --output table
  success "ECR repository '${ECR_REPO}' created."
fi

# ==============================================================================
# STEP 2: Docker login to ECR
# ==============================================================================
banner "Step 2 — Docker Login to ECR"

info "Authenticating Docker with ECR (${ECR_BASE}) ..."
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_BASE"
success "Docker authenticated with ECR."

# ==============================================================================
# STEP 3: Build Docker image
# ==============================================================================
banner "Step 3 — Docker Build"

info "Building image: ${ECR_IMAGE}"
info "Context: ${SCRIPT_DIR}"
echo ""

docker build \
  --file "${SCRIPT_DIR}/Dockerfile" \
  --tag "${ECR_IMAGE}" \
  --tag "${ECR_LATEST}" \
  --label "deploy.git-sha=${IMAGE_TAG}" \
  --label "deploy.timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "${SCRIPT_DIR}"

echo ""
success "Docker build complete."
docker images "${ECR_BASE}/${ECR_REPO}" --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedAt}}"

# ==============================================================================
# STEP 4: Push to ECR
# ==============================================================================
if [[ "$SKIP_PUSH" == "true" ]]; then
  warn "--skip-push specified. Skipping ECR push and ECS update."
  exit 0
fi

banner "Step 4 — Push to ECR"

info "Pushing ${ECR_IMAGE} ..."
docker push "${ECR_IMAGE}"
info "Pushing ${ECR_LATEST} ..."
docker push "${ECR_LATEST}"

success "Image pushed to ECR successfully."

# ==============================================================================
# STEP 5: Register / update ECS Task Definition
# ==============================================================================
banner "Step 5 — ECS Task Definition"

info "Fetching current task definition for family: ${TASK_FAMILY} ..."

if CURRENT_TD=$(aws ecs describe-task-definition \
      --task-definition "$TASK_FAMILY" \
      --region "$AWS_REGION" \
      --output json 2>/dev/null); then

  # Extract the current task definition and patch the image URI
  info "Patching task definition with new image: ${ECR_IMAGE}"

  # Use environment variable to pass JSON to python safely (avoids echo/pipe issues)
  export CURRENT_TD
  NEW_TD=$(python3 - <<PYEOF
import sys, json, os

data = json.loads(os.environ['CURRENT_TD'])
td   = data["taskDefinition"]

# Update the first container's image to the new ECR image
for c in td.get("containerDefinitions", []):
    c["image"] = "${ECR_IMAGE}"

# Strip keys that are not accepted when re-registering
for key in ["taskDefinitionArn", "revision", "status",
            "requiresAttributes", "compatibilities",
            "registeredAt", "registeredBy"]:
    td.pop(key, None)

print(json.dumps(td, indent=2))
PYEOF
)

  NEW_TD_ARN=$(aws ecs register-task-definition \
    --region "$AWS_REGION" \
    --cli-input-json "$NEW_TD" \
    --query "taskDefinition.taskDefinitionArn" \
    --output text)

  success "New task definition registered: ${NEW_TD_ARN}"

else
  warn "No existing task definition found for '${TASK_FAMILY}'."
  warn "You must create an ECS Task Definition manually in the AWS Console first,"
  warn "or supply a task-definition.json and run:"
  warn "  aws ecs register-task-definition --cli-input-json file://task-definition.json"
  warn ""
  warn "Skipping ECS service update. Push to ECR was still successful."
  exit 0
fi

# ==============================================================================
# STEP 6: Update ECS Service
# ==============================================================================
banner "Step 6 — ECS Service Update"

# Check if the service exists
if aws ecs describe-services \
      --cluster "$ECS_CLUSTER" \
      --services "$ECS_SERVICE" \
      --region "$AWS_REGION" \
      --query "services[0].status" \
      --output text 2>/dev/null | grep -q "ACTIVE"; then

  info "Updating ECS service '${ECS_SERVICE}' in cluster '${ECS_CLUSTER}' ..."
  aws ecs update-service \
    --cluster "$ECS_CLUSTER" \
    --service "$ECS_SERVICE" \
    --task-definition "$NEW_TD_ARN" \
    --force-new-deployment \
    --region "$AWS_REGION" \
    --output table

  success "ECS service update initiated!"
  info ""
  info "Monitor the deployment at:"
  info "  https://${AWS_REGION}.console.aws.amazon.com/ecs/v2/clusters/${ECS_CLUSTER}/services/${ECS_SERVICE}"
  info ""
  info "Or tail logs with:"
  info "  aws logs tail /ecs/${TASK_FAMILY} --follow --region ${AWS_REGION}"
  info ""
  info "Wait for service stability (≈2-5 min):"
  info "  aws ecs wait services-stable --cluster ${ECS_CLUSTER} --services ${ECS_SERVICE} --region ${AWS_REGION}"

else
  warn "ECS service '${ECS_SERVICE}' not found in cluster '${ECS_CLUSTER}'."
  warn "Create it in the AWS Console using the new task definition:"
  warn "  Task Definition ARN: ${NEW_TD_ARN}"
  warn ""
  warn "Image has been pushed to ECR. ECR image URI:"
  warn "  ${ECR_IMAGE}"
fi

banner "Deployment Complete 🚀"
echo -e "  ${GREEN}ECR Image :${NC} ${ECR_IMAGE}"
echo -e "  ${GREEN}Task Def  :${NC} ${NEW_TD_ARN:-N/A}"
echo -e "  ${GREEN}Cluster   :${NC} ${ECS_CLUSTER}"
echo -e "  ${GREEN}Service   :${NC} ${ECS_SERVICE}"
echo ""
