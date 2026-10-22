# BatchTime boot fix

The previous build let Streamlit install PyArrow 26 and then replace it with PyArrow 24 after dependency installation. This package pins PyArrow 24 directly, avoiding that second environment mutation during startup.

Replace app.py, parser.py, requirements.txt and .streamlit/config.toml together. Deploy on Python 3.12.

If the full app still does not start, temporarily set the Streamlit main file to healthcheck.py. If healthcheck.py opens, the platform is healthy and the full app startup can be inspected separately. If healthcheck.py also loops at provisioning, the failure is in Community Cloud provisioning rather than application code.
