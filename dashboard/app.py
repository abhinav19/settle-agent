"""Streamlit entrypoint:  streamlit run app.py

Environment: SETTLE_API_URL (default http://localhost:8000), SETTLE_API_KEY (required),
TRACE_URL_TEMPLATE (optional, e.g. http://localhost:16686/trace/{trace_id}).
"""

import os

import streamlit as st

from ops_dashboard.client import ApiClient
from ops_dashboard.ui import UiConfig, render


@st.cache_resource
def get_client(url: str, key: str) -> ApiClient:
    return ApiClient(url, key)


api_url = os.environ.get("SETTLE_API_URL", "http://localhost:8000")
api_key = os.environ.get("SETTLE_API_KEY", "")
if not api_key:
    st.set_page_config(page_title="SettleAgent Ops")
    st.error("SETTLE_API_KEY is not set. Export it (see .env.example) and restart the dashboard.")
    st.stop()

render(
    get_client(api_url, api_key),
    UiConfig(api_url=api_url, trace_url_template=os.environ.get("TRACE_URL_TEMPLATE")),
)
