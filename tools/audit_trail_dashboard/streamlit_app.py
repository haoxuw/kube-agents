import argparse
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


DEFAULT_SESSION_KV_DB = "/tmp/kube-agents-session-debug/session_kv.db"
DEFAULT_OTEL_DB = "/tmp/kube-agents-session-debug/live.db"
REQUIRED_SPAN_ATTRS = ("session.id", "user.id", "chat.id", "chat.platform")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--session-kv-db", default=DEFAULT_SESSION_KV_DB)
    parser.add_argument("--otel-db", default=DEFAULT_OTEL_DB)
    args, _ = parser.parse_known_args()
    return args


def connect_readonly(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    if not db_path.exists():
        raise FileNotFoundError(str(db_path))
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


@st.cache_data(show_spinner=False)
def load_sessions(path: str, mtime: float) -> pd.DataFrame:
    del mtime
    with connect_readonly(path) as conn:
        rows = conn.execute(
            "select session_id, metadata, updated_at from session_metadata order by updated_at desc"
        ).fetchall()

    records: List[Dict[str, Any]] = []
    for session_id, raw_metadata, updated_at in rows:
        try:
            metadata = json.loads(raw_metadata)
        except Exception:
            metadata = {"raw_metadata": raw_metadata}
        if not isinstance(metadata, dict):
            metadata = {"raw_metadata": raw_metadata}
        metadata.setdefault("session_id", session_id)
        metadata["kv_updated_at"] = updated_at
        metadata["actor"] = normalized_actor(metadata)
        records.append(metadata)
    return pd.DataFrame(records)


@st.cache_data(show_spinner=False)
def load_events(path: str, mtime: float) -> pd.DataFrame:
    del mtime
    with connect_readonly(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("select seq, kind, data from events order by seq asc").fetchall()

    records: List[Dict[str, Any]] = []
    for row in rows:
        try:
            data = json.loads(row["data"])
        except Exception:
            data = {"raw_data": row["data"]}
        attrs = data.get("attrs") or data.get("attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}

        input_value = attrs.get("input.value", "")
        command = extract_command(input_value)
        records.append(
            {
                "seq": row["seq"],
                "kind": row["kind"],
                "name": data.get("name", ""),
                "trace_id": data.get("trace_id", ""),
                "span_id": data.get("span_id", ""),
                "session_id": attrs.get("session.id") or attrs.get("session_id") or "",
                "user_id": plain_user_id(attrs),
                "actor": attrs.get("user.id", ""),
                "sender": attrs.get("hermes.sender.id", ""),
                "chat_id": attrs.get("chat.id", ""),
                "thread_id": attrs.get("chat.thread_id", ""),
                "platform": attrs.get("chat.platform", ""),
                "tool_name": attrs.get("tool.name") or attrs.get("mcp.tool.name") or "",
                "input_value": input_value,
                "command": command,
                "target_context": extract_kube_context(command),
                "kubectl_action": extract_kubectl_action(command),
                "missing_attrs": missing_required_attrs(attrs),
                "raw_attrs": attrs,
            }
        )
    return pd.DataFrame(records)


def db_mtime(path: str) -> float:
    try:
        return Path(path).stat().st_mtime
    except FileNotFoundError:
        return 0.0


def normalized_actor(metadata: Dict[str, Any]) -> str:
    platform = metadata.get("platform") or ""
    user_id = metadata.get("user_id") or metadata.get("user_email") or ""
    if platform and user_id and ":" not in str(user_id):
        return f"{platform}:{user_id}"
    return str(user_id)


def plain_user_id(attrs: Dict[str, Any]) -> str:
    sender = attrs.get("hermes.sender.id") or ""
    if sender:
        return sender
    actor = attrs.get("user.id") or ""
    if ":" in str(actor):
        return str(actor).split(":", 1)[1]
    return str(actor)


def missing_required_attrs(attrs: Dict[str, Any]) -> str:
    missing = [name for name in REQUIRED_SPAN_ATTRS if not attrs.get(name)]
    return ", ".join(missing)


def extract_command(input_value: Any) -> str:
    if not input_value:
        return ""
    if isinstance(input_value, str):
        try:
            parsed = json.loads(input_value)
        except Exception:
            return input_value
    else:
        parsed = input_value
    if isinstance(parsed, dict):
        return str(parsed.get("command") or "")
    return ""


def extract_kube_context(command: str) -> str:
    if not command:
        return ""
    match = re.search(r"--context(?:=|\s+)([^\s]+)", command)
    return match.group(1) if match else ""


def extract_kubectl_action(command: str) -> str:
    if not command or "kubectl" not in command:
        return ""
    parts = command.split()
    try:
        index = parts.index("kubectl")
    except ValueError:
        return ""
    for part in parts[index + 1 :]:
        if part.startswith("-") or "=" in part:
            continue
        return part
    return ""


def selectable_values(series: pd.Series) -> List[str]:
    if series.empty:
        return []
    return sorted(value for value in series.dropna().astype(str).unique() if value)


def filtered_events(
    events: pd.DataFrame,
    actor: str,
    user_id: str,
    chat_id: str,
    session_id: str,
    trace_id: str,
) -> pd.DataFrame:
    df = events.copy()
    if actor != "All":
        df = df[df["actor"] == actor]
    if user_id != "All":
        df = df[df["user_id"] == user_id]
    if chat_id != "All":
        df = df[df["chat_id"] == chat_id]
    if session_id != "All":
        df = df[df["session_id"] == session_id]
    if trace_id != "All":
        df = df[df["trace_id"] == trace_id]
    return df


def filter_sessions(
    sessions: pd.DataFrame,
    actor: str,
    user_id: str,
    chat_id: str,
    session_id: str,
) -> pd.DataFrame:
    df = sessions.copy()
    if actor != "All" and "actor" in df:
        df = df[df["actor"] == actor]
    if user_id != "All" and "user_id" in df:
        df = df[df["user_id"] == user_id]
    if chat_id != "All" and "chat_id" in df:
        df = df[df["chat_id"] == chat_id]
    if session_id != "All" and "session_id" in df:
        df = df[df["session_id"] == session_id]
    return df


def add_selectbox(label: str, values: Iterable[str]) -> str:
    options = ["All"] + list(values)
    return st.sidebar.selectbox(label, options)


def sankey_for_events(events: pd.DataFrame) -> Optional[go.Figure]:
    tool_events = events[events["tool_name"].astype(str) != ""].copy()
    if tool_events.empty:
        return None

    flows: Dict[Tuple[str, str], int] = {}
    for _, row in tool_events.iterrows():
        nodes = [
            row.get("user_id") or row.get("actor") or "unknown-user",
            row.get("chat_id") or "unknown-chat",
            row.get("session_id") or "unknown-session",
            row.get("trace_id") or "unknown-trace",
            row.get("tool_name") or row.get("name") or "unknown-tool",
        ]
        action = row.get("kubectl_action") or row.get("command") or row.get("name") or ""
        if action:
            nodes.append(action[:80])
        for source, target in zip(nodes, nodes[1:]):
            flows[(source, target)] = flows.get((source, target), 0) + 1

    labels = sorted({item for pair in flows for item in pair})
    index = {label: idx for idx, label in enumerate(labels)}
    return go.Figure(
        data=[
            go.Sankey(
                node={"label": labels, "pad": 15, "thickness": 14},
                link={
                    "source": [index[source] for source, _ in flows],
                    "target": [index[target] for _, target in flows],
                    "value": list(flows.values()),
                },
            )
        ]
    )


def pie_from_counts(df: pd.DataFrame, column: str, title: str) -> None:
    data = df[df[column].astype(str) != ""][column].value_counts().reset_index()
    data.columns = [column, "count"]
    if data.empty:
        st.caption(f"No data for {title}.")
        return
    st.plotly_chart(px.pie(data, names=column, values="count", title=title), use_container_width=True)


def bar_from_counts(df: pd.DataFrame, column: str, title: str) -> None:
    data = df[df[column].astype(str) != ""][column].value_counts().reset_index()
    data.columns = [column, "count"]
    if data.empty:
        st.caption(f"No data for {title}.")
        return
    st.plotly_chart(px.bar(data, x=column, y="count", title=title), use_container_width=True)


def audit_table(events: pd.DataFrame) -> pd.DataFrame:
    spans = events[events["kind"] == "span"].copy()
    return spans[spans["missing_attrs"].astype(str) != ""][
        ["seq", "name", "trace_id", "span_id", "session_id", "tool_name", "missing_attrs"]
    ]


def main() -> None:
    args = parse_args()
    st.set_page_config(page_title="Audit Trail Dashboard", layout="wide")
    st.title("Audit Trail Dashboard")

    with st.sidebar:
        st.header("Data")
        session_kv_db = st.text_input("session_kv.db", args.session_kv_db)
        otel_db = st.text_input("hermes_otel live.db", args.otel_db)
        if st.button("Refresh"):
            st.cache_data.clear()

    try:
        sessions = load_sessions(session_kv_db, db_mtime(session_kv_db))
        events = load_events(otel_db, db_mtime(otel_db))
    except Exception as exc:
        st.error(f"Could not load dashboard data: {exc}")
        st.stop()

    with st.sidebar:
        st.header("Filters")
        actor_values = sorted(set(selectable_values(events["actor"])) | set(selectable_values(sessions.get("actor", pd.Series(dtype=str)))))
        user_values = sorted(set(selectable_values(events["user_id"])) | set(selectable_values(sessions.get("user_id", pd.Series(dtype=str)))))
        chat_values = sorted(set(selectable_values(events["chat_id"])) | set(selectable_values(sessions.get("chat_id", pd.Series(dtype=str)))))
        session_values = sorted(set(selectable_values(events["session_id"])) | set(selectable_values(sessions.get("session_id", pd.Series(dtype=str)))))
        actor = add_selectbox("Actor", actor_values)
        user_id = add_selectbox("User ID", user_values)
        chat_id = add_selectbox("Chat Space", chat_values)
        session_id = add_selectbox("Session", session_values)

        candidate_events = filtered_events(events, actor, user_id, chat_id, session_id, "All")
        trace_id = add_selectbox("Trace", selectable_values(candidate_events["trace_id"]))

    visible_events = filtered_events(events, actor, user_id, chat_id, session_id, trace_id)
    visible_sessions = filter_sessions(sessions, actor, user_id, chat_id, session_id)

    spans = visible_events[visible_events["kind"] == "span"]
    tool_spans = spans[spans["tool_name"].astype(str) != ""]
    missing = audit_table(visible_events)

    metric_cols = st.columns(5)
    metric_cols[0].metric("Sessions", len(visible_sessions))
    metric_cols[1].metric("Traces", spans["trace_id"].replace("", pd.NA).dropna().nunique())
    metric_cols[2].metric("Spans", len(spans))
    metric_cols[3].metric("Tool Calls", len(tool_spans))
    metric_cols[4].metric("Missing Attribution", len(missing))

    tab_overview, tab_flow, tab_trace, tab_audit, tab_raw = st.tabs(
        ["Overview", "Data Flow", "Trace Explorer", "Audit Gaps", "Raw Data"]
    )

    with tab_overview:
        st.subheader("Recent Sessions")
        session_cols = [
            col
            for col in (
                "kv_updated_at",
                "platform",
                "user_id",
                "actor",
                "user_resource",
                "chat_id",
                "thread_id",
                "session_id",
            )
            if col in visible_sessions.columns
        ]
        st.dataframe(visible_sessions[session_cols].head(100), use_container_width=True, hide_index=True)

        left, middle, right = st.columns(3)
        with left:
            pie_from_counts(visible_events, "user_id", "Activity by User ID")
        with middle:
            pie_from_counts(visible_events, "chat_id", "Activity by Chat Space")
        with right:
            pie_from_counts(visible_events, "tool_name", "Tool Mix")

        bar_from_counts(tool_spans, "target_context", "Kubectl Tool Calls by Context")

    with tab_flow:
        st.subheader("Who Sent What, And Which Actions Followed")
        fig = sankey_for_events(visible_events)
        if fig is None:
            st.info("No tool calls matched the current filters.")
        else:
            st.plotly_chart(fig, use_container_width=True)

        action_cols = [
            "seq",
            "user_id",
            "chat_id",
            "session_id",
            "trace_id",
            "tool_name",
            "kubectl_action",
            "target_context",
            "command",
        ]
        st.dataframe(tool_spans[action_cols], use_container_width=True, hide_index=True)

    with tab_trace:
        st.subheader("Ordered Trace Events")
        trace_cols = [
            "seq",
            "kind",
            "name",
            "trace_id",
            "span_id",
            "session_id",
            "user_id",
            "chat_id",
            "tool_name",
            "input_value",
        ]
        st.dataframe(visible_events[trace_cols], use_container_width=True, hide_index=True)

    with tab_audit:
        st.subheader("Spans Missing Required Attribution")
        if missing.empty:
            st.success("All matching spans have the required attribution fields.")
        else:
            st.dataframe(missing, use_container_width=True, hide_index=True)

    with tab_raw:
        st.subheader("Raw Session Metadata")
        st.dataframe(visible_sessions, use_container_width=True, hide_index=True)
        st.subheader("Raw OTel Events")
        st.dataframe(visible_events, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
