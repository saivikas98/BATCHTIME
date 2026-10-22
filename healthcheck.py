"""Temporary deployment health check.
Set this file as the main module only to verify the Streamlit container."""
import platform
import streamlit as st

st.set_page_config(page_title="BatchTime Health Check")
st.title("BatchTime deployment is healthy")
st.success("The Streamlit backend and browser session are connected.")
st.write({"Python": platform.python_version(), "Streamlit": st.__version__})
