#!/usr/bin/env bash
#
# gpu-nodepool.sh manages GPU accelerator node pools for self-hosted Gemma 4
# inference on GKE using Compute Engine Capacity Advice (Obtainability) telemetry
# and GKE Flex/Spot scheduling best practices.
#
set -euo pipefail

readonly DEFAULT_POOL_NAME="gpu-pool"
readonly DEFAULT_GPU_TYPE="nvidia-l4"
readonly DEFAULT_GPU_COUNT="1"
readonly DEFAULT_MACHINE_TYPE="g2-standard-8"
readonly DEFAULT_ACCELERATOR="type=nvidia-l4,count=1,gpu-driver-version=default"
readonly DEFAULT_PROVISIONING_MODEL="SPOT"
readonly DEFAULT_MIN_NODES="0"
readonly DEFAULT_MAX_NODES="2"
readonly DEFAULT_INITIAL_NODES="1"
readonly DEFAULT_LOCATION_POLICY="ANY"
readonly DEFAULT_TARGET_SHAPE="any-single-zone"
readonly HIGH_OBTAINABILITY_THRESHOLD="0.7"
readonly MODERATE_OBTAINABILITY_THRESHOLD="0.4"
readonly GPU_TYPE_L4="nvidia-l4"
readonly GPU_TYPE_T4="nvidia-tesla-t4"
readonly GPU_TYPE_P4="nvidia-tesla-p4"
readonly AUTOPILOT_TRUE="True"

usage() {
  cat <<HELP_EOF
Usage: $(basename "$0") <command> [options]

Commands:
  check-obtainability  Query Compute Engine Capacity Advice API for GPU obtainability
  validate             Validate GPU type, count, and machine type configuration
  create               Provision a Flex/Spot GPU node pool with obtainability pre-check
  delete               Tear down the GPU node pool

Environment variables:
  PROJECT_ID           GCP project ID (default: current gcloud / kubectl context)
  CLUSTER_NAME         GKE cluster name (default: current kubectl / gcloud context)
  LOCATION             GKE cluster location/region/zone (default: current kubectl / gcloud context)
  POOL_NAME            Node pool name (default: ${DEFAULT_POOL_NAME})
  GPU_TYPE             GPU accelerator type: nvidia-l4, nvidia-tesla-t4, nvidia-tesla-p4 (default: ${DEFAULT_GPU_TYPE})
  GPU_COUNT            GPU count per node: 1, 2, 4 (default: ${DEFAULT_GPU_COUNT})
  GPU_MACHINE_TYPE     Machine type (default: auto-resolved based on GPU_TYPE and GPU_COUNT)
  PROVISIONING_MODEL   SPOT or FLEX_START (default: ${DEFAULT_PROVISIONING_MODEL})
  INITIAL_NODES        Initial node count (default: ${DEFAULT_INITIAL_NODES})
  MIN_NODES            Autoscaling min nodes (default: ${DEFAULT_MIN_NODES})
  MAX_NODES            Autoscaling max nodes (default: ${DEFAULT_MAX_NODES})
HELP_EOF
  exit 1
}

resolve_project() {
  local project="${PROJECT_ID:-}"
  if [[ -z "${project}" ]]; then
    project="$(gcloud config get-value project 2>/dev/null || true)"
  fi
  if [[ -z "${project}" ]]; then
    echo "ERROR: PROJECT_ID is not set and could not be resolved from gcloud config." >&2
    exit 1
  fi
  echo "${project}"
}

resolve_region_from_location() {
  local loc="$1"
  if [[ "${loc}" =~ ^([a-z0-9-]+)-[a-z]$ ]]; then
    echo "${BASH_REMATCH[1]}"
  else
    echo "${loc}"
  fi
}

resolve_cluster_and_location() {
  local cluster="${CLUSTER_NAME:-}"
  local location="${LOCATION:-${REGION:-}}"
  local project="${PROJECT_ID:-}"

  if [[ -z "${cluster}" ]]; then
    cluster="$(gcloud config get-value container/cluster 2>/dev/null || true)"
  fi
  if [[ -z "${location}" ]]; then
    location="$(gcloud config get-value compute/zone 2>/dev/null || true)"
    if [[ -z "${location}" ]]; then
      location="$(gcloud config get-value compute/region 2>/dev/null || true)"
    fi
  fi

  if [[ -z "${cluster}" || -z "${location}" || -z "${project}" ]]; then
    local current_ctx
    current_ctx="$(kubectl config current-context 2>/dev/null || true)"
    if [[ "${current_ctx}" =~ ^gke_([^_]+)_([^_]+)_(.+)$ ]]; then
      if [[ -z "${project}" ]]; then
        project="${BASH_REMATCH[1]}"
      fi
      if [[ -z "${location}" ]]; then
        location="${BASH_REMATCH[2]}"
      fi
      if [[ -z "${cluster}" ]]; then
        cluster="${BASH_REMATCH[3]}"
      fi
    fi
  fi

  RESOLVED_CLUSTER="${cluster}"
  RESOLVED_LOCATION="${location}"
  if [[ -n "${project}" && -z "${PROJECT_ID:-}" ]]; then
    PROJECT_ID="${project}"
  fi
}

resolve_gpu_configuration() {
  local gpu_type="${GPU_TYPE:-${DEFAULT_GPU_TYPE}}"
  local gpu_count="${GPU_COUNT:-${DEFAULT_GPU_COUNT}}"

  case "${gpu_type}" in
    l4|L4) gpu_type="${GPU_TYPE_L4}" ;;
    t4|T4) gpu_type="${GPU_TYPE_T4}" ;;
    p4|P4) gpu_type="${GPU_TYPE_P4}" ;;
  esac

  if [[ "${gpu_type}" == "${GPU_TYPE_P4}" ]]; then
    echo "ERROR: NVIDIA Tesla P4 (Pascal architecture, compute capability 6.1) is unsupported for Gemma 4 self-hosted inference." >&2
    echo "vLLM requires compute capability >= 7.0 (Volta or newer), and Gemma 4 attention kernels (Triton / FlashAttention)" >&2
    echo "require modern tensor cores and hardware bfloat16 support." >&2
    echo "Recommendation: Use 1x NVIDIA L4 (Ada Lovelace, sm_89) via 'g2-standard-8' (Spot: ~\$0.22/hr), which offers native bfloat16 and 24GB VRAM." >&2
    exit 1
  fi

  if [[ "${gpu_type}" == "${GPU_TYPE_T4}" ]]; then
    echo "WARNING: NVIDIA Tesla T4 (Turing architecture, sm_75) has a hardware shared memory ceiling of 64KB (65,536 bytes) per SM block." >&2
    echo "Gemma 4's heterogeneous attention architecture (global_head_dim=512) requires 96KB (98,304 bytes) of SM shared memory in Triton," >&2
    echo "failing with OutOfResources during attention compilation. In addition, 2x T4 Spot (~\$0.30/hr) is more expensive than 1x L4 Spot (~\$0.22/hr)." >&2
    echo "Recommended: Use 1x NVIDIA L4 (Ada Lovelace, sm_89) via 'g2-standard-8' (24GB VRAM, 100KB SM SRAM, native bfloat16)." >&2
  fi

  if [[ "${gpu_type}" != "${GPU_TYPE_L4}" && "${gpu_type}" != "${GPU_TYPE_T4}" ]]; then
    echo "ERROR: Unsupported GPU_TYPE '${gpu_type}'. Supported types: '${GPU_TYPE_L4}', '${GPU_TYPE_T4}'." >&2
    exit 1
  fi

  local machine_type="${GPU_MACHINE_TYPE:-}"
  if [[ -z "${machine_type}" ]]; then
    if [[ "${gpu_type}" == "${GPU_TYPE_L4}" ]]; then
      case "${gpu_count}" in
        1) machine_type="g2-standard-8" ;;
        2) machine_type="g2-standard-24" ;;
        4) machine_type="g2-standard-48" ;;
        *)
          echo "ERROR: Unsupported GPU_COUNT '${gpu_count}' for L4. Supported counts: 1, 2, 4." >&2
          exit 1
          ;;
      esac
    elif [[ "${gpu_type}" == "${GPU_TYPE_T4}" ]]; then
      case "${gpu_count}" in
        1) machine_type="n1-standard-4" ;;
        2) machine_type="n1-standard-8" ;;
        4) machine_type="n1-standard-16" ;;
        *)
          echo "ERROR: Unsupported GPU_COUNT '${gpu_count}' for T4. Supported counts: 1, 2, 4." >&2
          exit 1
          ;;
      esac
    fi
  fi

  local accelerator="type=${gpu_type},count=${gpu_count},gpu-driver-version=default"
  RESOLVED_GPU_TYPE="${gpu_type}"
  RESOLVED_GPU_COUNT="${gpu_count}"
  RESOLVED_MACHINE_TYPE="${machine_type}"
  RESOLVED_ACCELERATOR="${accelerator}"
}

cmd_validate() {
  resolve_gpu_configuration
  echo "Valid GPU configuration:"
  echo "  GPU_TYPE: ${RESOLVED_GPU_TYPE}"
  echo "  GPU_COUNT: ${RESOLVED_GPU_COUNT}"
  echo "  MACHINE_TYPE: ${RESOLVED_MACHINE_TYPE}"
  echo "  ACCELERATOR: ${RESOLVED_ACCELERATOR}"
}

cmd_check_obtainability() {
  resolve_cluster_and_location
  local project
  project="$(resolve_project)"
  local loc="${RESOLVED_LOCATION}"
  if [[ -z "${loc}" ]]; then
    echo "ERROR: LOCATION or REGION must be specified or resolvable from kubectl/gcloud context." >&2
    exit 1
  fi

  resolve_gpu_configuration

  local region
  region="$(resolve_region_from_location "${loc}")"
  local machine_type="${RESOLVED_MACHINE_TYPE}"
  local prov_model="${PROVISIONING_MODEL:-${DEFAULT_PROVISIONING_MODEL}}"

  echo "==> Checking GPU obtainability via Compute Advice API..."
  echo "    Project:            ${project}"
  echo "    Region:             ${region}"
  echo "    Location/Zone:      ${loc}"
  echo "    GPU Type:           ${RESOLVED_GPU_TYPE}"
  echo "    GPU Count:          ${RESOLVED_GPU_COUNT}"
  echo "    Machine Type:       ${machine_type}"
  echo "    Provisioning Model: ${prov_model}"

  local zone_flag=()
  if [[ "${loc}" =~ ^[a-z0-9-]+-[a-z]$ ]]; then
    zone_flag=(--zones="${loc}")
  fi

  local output
  if ! output="$(gcloud beta compute advice capacity \
    --project="${project}" \
    --region="${region}" \
    "${zone_flag[@]}" \
    --provisioning-model="${prov_model}" \
    --size=1 \
    --instance-selection-machine-types="${machine_type}" \
    --target-distribution-shape="${DEFAULT_TARGET_SHAPE}" \
    --format="json" 2>&1)"; then
    echo "WARNING: Could not query Capacity Advice API: ${output}" >&2
    echo "Proceeding with standard node pool provisioning..."
    return 0
  fi

  python3 -c "
import json, sys
data = json.loads('''${output}''')
recs = data.get('recommendations', [])
if not recs:
    print('  No specific recommendation returned by Capacity Advice API.')
    sys.exit(0)
for r in recs:
    scores = r.get('scores', {})
    obt = scores.get('obtainability', 0.0)
    uptime = scores.get('estimatedUptime', 'unknown')
    shards = r.get('shards', [])
    zone = shards[0].get('zone', '').split('/')[-1] if shards else 'any'
    status = 'HIGH (Optimal)' if obt >= ${HIGH_OBTAINABILITY_THRESHOLD} else ('MODERATE' if obt >= ${MODERATE_OBTAINABILITY_THRESHOLD} else 'LOW (Stockout Risk)')
    print(f'  Obtainability Score: {obt:.2f} [{status}]')
    print(f'  Estimated Uptime:    {uptime}')
    print(f'  Recommended Zone:    {zone}')
"
}

cmd_create() {
  resolve_cluster_and_location
  local cluster="${RESOLVED_CLUSTER}"
  local location="${RESOLVED_LOCATION}"
  local pool_name="${POOL_NAME:-${DEFAULT_POOL_NAME}}"
  local initial_nodes="${INITIAL_NODES:-${DEFAULT_INITIAL_NODES}}"
  local min_nodes="${MIN_NODES:-${DEFAULT_MIN_NODES}}"
  local max_nodes="${MAX_NODES:-${DEFAULT_MAX_NODES}}"

  if [[ -z "${cluster}" || -z "${location}" ]]; then
    echo "ERROR: CLUSTER_NAME and LOCATION must be specified or resolvable from kubectl/gcloud context." >&2
    usage
  fi

  local project
  project="$(resolve_project)"

  local is_autopilot
  is_autopilot="$(gcloud container clusters describe "${cluster}" \
    --location="${location}" \
    --project="${project}" \
    --format="value(autopilot.enabled)" 2>/dev/null || true)"

  if [[ "${is_autopilot}" == "${AUTOPILOT_TRUE}" ]]; then
    echo "INFO: Cluster '${cluster}' is a GKE Autopilot cluster."
    echo "GKE Autopilot manages GPU accelerator node provisioning automatically at Pod scheduling time."
    echo "No manual node pool creation is required or supported. Simply deploy vLLM with 'cloud.google.com/gke-accelerator: nvidia-l4'."
    return 0
  fi

  resolve_gpu_configuration

  local machine_type="${RESOLVED_MACHINE_TYPE}"
  local accelerator="${RESOLVED_ACCELERATOR}"

  cmd_check_obtainability

  echo "==> Checking if node pool '${pool_name}' already exists in cluster '${cluster}'..."
  if gcloud container node-pools describe "${pool_name}" \
    --cluster="${cluster}" \
    --location="${location}" \
    --project="${project}" >/dev/null 2>&1; then
    echo "Node pool '${pool_name}' already exists. Skipping creation."
    return 0
  fi

  echo "==> Creating GPU node pool '${pool_name}' (${machine_type}, ${accelerator}) with Flex/Spot location-policy ANY..."
  gcloud container node-pools create "${pool_name}" \
    --cluster="${cluster}" \
    --location="${location}" \
    --project="${project}" \
    --machine-type="${machine_type}" \
    --accelerator="${accelerator}" \
    --num-nodes="${initial_nodes}" \
    --spot \
    --enable-autoscaling \
    --min-nodes="${min_nodes}" \
    --max-nodes="${max_nodes}" \
    --location-policy="${DEFAULT_LOCATION_POLICY}"

  echo "==> Node pool '${pool_name}' successfully provisioned."
}

cmd_delete() {
  resolve_cluster_and_location
  local cluster="${RESOLVED_CLUSTER}"
  local location="${RESOLVED_LOCATION}"
  local pool_name="${POOL_NAME:-${DEFAULT_POOL_NAME}}"

  if [[ -z "${cluster}" || -z "${location}" ]]; then
    echo "ERROR: CLUSTER_NAME and LOCATION must be specified or resolvable from kubectl/gcloud context." >&2
    usage
  fi

  local project
  project="$(resolve_project)"

  echo "==> Checking if node pool '${pool_name}' exists..."
  if ! gcloud container node-pools describe "${pool_name}" \
    --cluster="${cluster}" \
    --location="${location}" \
    --project="${project}" >/dev/null 2>&1; then
    echo "Node pool '${pool_name}' does not exist. Nothing to delete."
    return 0
  fi

  echo "==> Deleting GPU node pool '${pool_name}' from cluster '${cluster}'..."
  gcloud container node-pools delete "${pool_name}" \
    --cluster="${cluster}" \
    --location="${location}" \
    --project="${project}" \
    --quiet
  echo "==> Node pool '${pool_name}' deleted."
}

main() {
  if [[ $# -lt 1 ]]; then
    usage
  fi

  case "$1" in
    check-obtainability)
      cmd_check_obtainability
      ;;
    validate)
      cmd_validate
      ;;
    create)
      cmd_create
      ;;
    delete)
      cmd_delete
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "ERROR: Unknown command '$1'" >&2
      usage
      ;;
  esac
}

main "$@"
