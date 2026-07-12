"""
Root agent for Mickey Marathon.

Bare-bones hello-world verifier: replies "Hello" to every message so we
can confirm the deploy pipeline (Vertex AI Reasoning Engine -> The Forum
-> messaging platform) works end-to-end before adding real logic.
"""
import os

# Force model API calls to the `global` endpoint so preview models
# (e.g. gemini-3.1-pro-preview) are accessible even when the Agent Engine
# itself is deployed in a regional location like us-central1.
os.environ['GOOGLE_CLOUD_LOCATION'] = 'global'

from google.adk.agents import Agent

root_agent = Agent(
    model=os.environ.get('HIGH_QUALITY_AGENT_MODEL', 'gemini-3.1-pro-preview'),
    name='root_agent',
    description='Mickey Marathon — a Comites.ai agent.',
    instruction='Reply to every message with exactly: Hello',
)
