# Self-Hosted Gemma 4 on GKE with vLLM

This directory provides a standalone reference recipe for deploying self-hosted Gemma 4 inference using vLLM on Google Kubernetes Engine (GKE).

## Target Use Cases

1. **Air-Gapped & Disconnected Environments**: Clusters with no external internet routing where all container images are mirrored internally and model weights are pre-staged on cluster storage.
2. **Policy-Restricted & Regulated Enterprises**: Financial services, healthcare (HIPAA), and sovereign cloud environments where InfoSec mandates that operational data (logs, cluster state, code) must never traverse external multi-tenant LLM APIs.

---

## Prerequisites & Hardware Selection

- **GKE Autopilot**: Automatically provisions and scales GPU accelerator nodes dynamically when pods request `nvidia.com/gpu`. No node pool provisioning is required.
- **GKE Standard**: Requires a GPU node pool.
  - **Recommended Accelerators for 27B / 31B**:
    - **Multi-GPU L4**: 2x NVIDIA L4 (`g2-standard-24`, 48 GB VRAM total) with FP8/AWQ quantization, or 4x NVIDIA L4 (`g2-standard-48`, 96 GB VRAM total) for native bfloat16 (`--tensor-parallel-size=2` or `4`).
    - **High-VRAM A100**: 1x or 2x NVIDIA A100 80GB (`a2-ultragpu-1g` / `a2-highgpu-2g`).
  - **Hardware Boundary Note**: Smaller single-GPU models (2B/4B/9B) lack the parameter depth for reliable agentic tool use and SRE reasoning; only the 27B and 31B models are suggested.

### GPU Architecture & Sizing Research

| Accelerator         | Architecture           | SM SRAM (Shared Memory) | Native bfloat16    | VRAM per GPU | Gemma 4 Support                                                                      |
| :------------------ | :--------------------- | :---------------------- | :----------------- | :----------- | :----------------------------------------------------------------------------------- |
| **NVIDIA Tesla T4** | Turing (`sm_75`)       | 64 KB                   | No (emulated FP32) | 16 GB GDDR6  | **Unsupported**: Fails due to <99KB SRAM kernel barrier and lack of native bfloat16. |
| **NVIDIA L4**       | Ada Lovelace (`sm_89`) | 100 KB                  | Yes                | 24 GB GDDR6  | **Supported**: 2x L4 (48 GB) for FP8/AWQ or 4x L4 (96 GB) for unquantized bfloat16.  |
| **NVIDIA A100**     | Ampere (`sm_80`)       | 164 KB                  | Yes                | 80 GB HBM2e  | **Supported**: 1x A100 80GB for 27B native bfloat16; 1-2x A100 for 31B.              |

#### Architectural Barrier on Legacy GPUs (Tesla T4)

Gemma 4 model architecture and modern vLLM attention kernels (e.g. FlashInfer) require hardware-accelerated `bfloat16` and allocate >99 KB of shared memory per Streaming Multiprocessor (SM). NVIDIA T4 hardware caps SM shared memory at 64 KB, leading to kernel compilation failures and runtime `CUDA error: out of shared memory`. Multi-GPU tensor parallelism cannot bypass this per-SM hardware barrier. Consequently, Ada Lovelace (L4) or Ampere (A100) GPUs are strictly required.

### Cluster Node Provisioning

Per repository engineering rules, GCP infrastructure is provisioned declaratively rather than via imperative shell scripts:

- **GKE Autopilot**: Automatically provisions and scales GPU accelerator nodes dynamically when pods request `nvidia.com/gpu` with accelerator selector `cloud.google.com/gke-accelerator: nvidia-l4`. No manual node pool configuration is required.
- **GKE Standard**: Configure an accelerator node pool declaratively in Terraform using `terraform/modules/gke-cluster/main.tf` or `google_container_node_pool`:
  ```hcl
  resource "google_container_node_pool" "gpu_pool" {
    name       = "l4-inference-pool"
    cluster    = google_container_cluster.primary.id
    node_count = 1

    node_config {
      machine_type = "g2-standard-24" # 2x NVIDIA L4 (48 GB VRAM)
      spot         = true

      guest_accelerator {
        type  = "nvidia-l4"
        count = 2
        gpu_driver_installation_config {
          gpu_driver_version = "DEFAULT"
        }
      }

      taint {
        key    = "nvidia.com/gpu"
        value  = "present"
        effect = "NO_SCHEDULE"
      }
    }
  }
  ```

---

## Deployment Modes

### Option A: Air-Gapped / Disconnected (Zero Internet Egress)

1. Pre-stage Gemma 4-27B or 31B weights into a `PersistentVolumeClaim` (e.g. `gemma4-weights-pvc` backed by Filestore or Cloud Storage FUSE).
2. Uncomment the `model-weights` volume and volume mount in `deployment.yaml`.
3. Set the `--model=/mnt/models/gemma-4-27b` argument in `deployment.yaml`.
4. Apply manifests:

```bash
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
kubectl apply -f networkpolicy.yaml
kubectl apply -f podmonitoring.yaml
```

### Option B: Online / Development Sandbox (Hugging Face Secret)

For connected development clusters pulling weights directly from Hugging Face:

1. Create the access secret:
   ```bash
   kubectl create secret generic hf-secret \
     --namespace kubeagents-system \
     --from-literal=token="<YOUR_HF_TOKEN>"
   ```
2. Apply the manifests:
   ```bash
   kubectl apply -f deployment.yaml
   kubectl apply -f service.yaml
   kubectl apply -f networkpolicy.yaml
   kubectl apply -f podmonitoring.yaml
   ```

---

## Wiring to kube-agents

### 1. Via LiteLLM Proxy Gateway (Recommended for Egress Isolation)

Deploy the `custom` LiteLLM overlay pointing to the in-cluster vLLM service:

```bash
make -C k8s-operator deploy-litellm \
  MODEL_PROVIDER=custom \
  MODEL_DEFAULT_NAME=google/gemma-4-27B-it \
  CUSTOM_API_BASE=http://vllm-gemma.kubeagents-system.svc.cluster.local:8000/v1
```

### 2. Direct Hermes Connection (Zero Proxy)

If bypassing LiteLLM, configure the agent's environment directly:

```yaml
env:
  - name: OPENAI_BASE_URL
    value: "http://vllm-gemma.kubeagents-system.svc.cluster.local:8000/v1"
  - name: OPENAI_API_KEY
    value: "none"
```

---

## Model Sizing & Reasoning Guidance

At present, only the **Gemma 4-27B** (`google/gemma-4-27B-it`) and **Gemma 4-31B** (`google/gemma-4-31B-it`) models are suggested for `kube-agents`.

Smaller model variants (such as 2B, 4B, or 9B) lack the parameter capacity and reasoning depth required for autonomous multi-step Kubernetes diagnostics, tool selection, and YAML reconciliation. Do not deploy smaller model variants for platform or cluster agent operations.
