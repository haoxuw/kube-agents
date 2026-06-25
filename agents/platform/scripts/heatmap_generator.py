#!/usr/bin/env python3
import json
import subprocess
import os
from datetime import datetime
from pathlib import Path

def get_memory_path():
    # Priority: HERMES_HOME/memories/MEMORY.md
    hermes_home = os.environ.get("HERMES_HOME", "/opt/data")
    path = Path(hermes_home) / "memories" / "MEMORY.md"
    
    # Ensure directory exists
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)

MEMORY_FILE = get_memory_path()
NAMESPACE = "cpu-test"

import argparse

def get_jobs_history(cluster=None):
    try:
        cmd = ["kubectl", "get", "jobs", "-n", NAMESPACE, "-o", "json"]
        if cluster:
            # Assuming the context is named appropriately or we use gcloud to get credentials
            # For simplicity, we assume the environment already has the correct context or we're running in a pod that can see the fleet
            pass 
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, check=True
        )
        return json.loads(result.stdout)
    except Exception as e:
        print(f"Error fetching jobs: {e}")
        return {"items": []}

def generate_heatmap_data(jobs_data):
    # 7x24 grid: [day][hour]
    heatmap = [[0 for _ in range(24)] for _ in range(7)]
    for job in jobs_data.get("items", []):
        status = job.get("status", {})
        if status.get("succeeded"):
            completion_str = status.get("completionTime")
            if completion_str:
                ts = datetime.fromisoformat(completion_str.replace("Z", "+00:00"))
                heatmap[ts.weekday()][ts.hour] = 1
    return heatmap

def format_heatmaps(heatmap, cluster_name="Unknown"):
    def get_emoji(val):
        return "🔥" if val > 0 else "❄️"

    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    
    output = f"#### Cluster: `{cluster_name}`\n\n"
    
    # 7 Cells
    output += "### 1. Real Day of Week Intensity (7 Cells)\n"
    output += "| " + " | ".join(days) + " |\n"
    output += "| " + " | ".join([get_emoji(sum(heatmap[d])) for d in range(7)]) + " |\n\n"

    # 24 Cells
    output += "### 2. Real Hour of Day Intensity (24 Cells)\n"
    header1 = "| Hour |" + "|".join([f"{h:02d}" for h in range(12)]) + "|"
    row1 = "| Stat |" + "|".join([get_emoji(any(heatmap[d][h] for d in range(7))) for h in range(12)]) + "|"
    header2 = "| Hour |" + "|".join([f"{h:02d}" for h in range(12, 24)]) + "|"
    row2 = "| Stat |" + "|".join([get_emoji(any(heatmap[d][h] for d in range(7))) for h in range(12, 24)]) + "|"
    
    output += header1 + "\n" + row1 + "\n\n" + header2 + "\n" + row2 + "\n"
    return output

def update_memory(heatmap_text, cluster_name="Unknown"):
    path = Path(MEMORY_FILE)
    if not path.exists():
        content = "# MEMORY.md\n\n"
    else:
        content = path.read_text()

    section_header = f"## CPU Utilization Heatmap: {cluster_name} (Automated)"
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
    new_section = f"{section_header}\n*Last updated: {timestamp}*\n\n{heatmap_text}\n"

    # Find and replace the specific cluster section or the general automated section
    lines = content.splitlines()
    start_idx = -1
    for i, line in enumerate(lines):
        if line.strip() == section_header:
            start_idx = i
            break
    
    if start_idx == -1:
        # Check for the old generic header to replace it
        for i, line in enumerate(lines):
            if line.strip() == "## CPU Utilization Heatmap (Automated)":
                start_idx = i
                break

    if start_idx != -1:
        end_idx = len(lines)
        for i in range(start_idx + 1, len(lines)):
            if lines[i].startswith("## "):
                end_idx = i
                break
        updated_content = "\n".join(lines[:start_idx]) + "\n" + new_section + "\n" + "\n".join(lines[end_idx:])
    else:
        updated_content = content.strip() + "\n\n" + new_section

    path.write_text(updated_content)
    print(f"Updated {MEMORY_FILE}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster", default="sahara-01")
    args = parser.parse_args()

    jobs = get_jobs_history()
    heatmap = generate_heatmap_data(jobs)
    text = format_heatmaps(heatmap, args.cluster)
    update_memory(text, args.cluster)
