# Self-Hosted Gemma 4 on GKE with vLLM

This directory provides a standalone reference recipe for deploying self-hosted Gemma 4 inference using vLLM on Google Kubernetes Engine (GKE).

## Target Use Cases

1. **Air-Gapped & Disconnected Environments**: Clusters with no external internet routing where all container images are mirrored internally and model weights are pre-staged on cluster storage.
2. **Policy-Restricted & Regulated Enterprises**: Financial services, healthcare (HIPAA), and sovereign cloud environments where InfoSec mandates that operational data (logs, cluster state, code) must never traverse external multi-tenant LLM APIs.

---

## Prerequisites & Hardware Selection

* **GKE Autopilot**: Automatically provisions and scales GPU accelerator nodes dynamically when pods request `nvidia.com/gpu`. No node pool provisioning is required.
* **GKE Standard**: Requires a GPU node pool.
  * **Recommended Accelerators for 27B / 31B**:
    * **Multi-GPU L4**: 2x NVIDIA L4 (`g2-standard-24`, 48 GB VRAM total) with FP8/AWQ quantization, or 4x NVIDIA L4 (`g2-standard-48`, 96 GB VRAM total) for native bfloat16 (`--tensor-parallel-size=2` or `4`).
    * **High-VRAM A100**: 1x or 2x NVIDIA A100 80GB (`a2-ultragpu-1g` / `a2-highgpu-2g`).
  * **Hardware Boundary Note**: Smaller single-GPU models (2B/4B/9B) lack the parameter depth for reliable agentic tool use and SRE reasoning; only the 27B and 31B models are suggested.

### Provisioning the GPU Node Pool (GKE Standard)

Use the included helper script to verify obtainability via Compute Engine Capacity Advice API and provision an autoscaling GPU pool:

```bash
# Verify GPU quota and obtainability advice
./gpu-nodepool.sh check-obtainability

# Provision the isolated GPU pool (tainted with nvidia.com/gpu:NoSchedule)
./gpu-nodepool.sh create
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
