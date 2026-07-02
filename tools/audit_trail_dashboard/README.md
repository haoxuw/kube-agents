# Audit Trail Dashboard

Read-only Streamlit dashboard for inspecting Hermes session metadata and OTel
events.

Copy live data from the platform agent pod:

```bash
mkdir -p /tmp/kube-agents-session-debug
POD=$(kubectl -n kubeagents-system get pod -l app=platform-agent-gateway -o jsonpath='{.items[0].metadata.name}')
kubectl -n kubeagents-system cp "$POD:/opt/data/session_kv.db" /tmp/kube-agents-session-debug/session_kv.db -c platform-agent
kubectl -n kubeagents-system cp "$POD:/opt/data/plugins/hermes_otel/live.db" /tmp/kube-agents-session-debug/live.db -c platform-agent
```

Run locally:

```bash
streamlit run tools/audit_trail_dashboard/streamlit_app.py -- \
  --session-kv-db /tmp/kube-agents-session-debug/session_kv.db \
  --otel-db /tmp/kube-agents-session-debug/live.db
```
