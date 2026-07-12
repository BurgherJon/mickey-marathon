"""
Custom function tools for your agent.

Each function in this file is wrappable in `google.adk.tools.FunctionTool`
and added to `root_agent.tools` in `agent.py`. The function's docstring
is shown to the LLM as the tool description, so write clear docstrings
that explain what the tool does, what arguments it takes, and what it
returns.

Pattern for adding a new tool:

    def my_tool(some_arg: str) -> dict:
        \"\"\"
        Short one-liner that explains what this does.

        Args:
            some_arg: Description.

        Returns:
            Description of the return structure.
        \"\"\"
        return {"result": some_arg}

Then in agent.py:

    from .custom_functions import my_tool
    ...
    tools=[FunctionTool(my_tool)],
"""
import os
from typing import Any, Dict

from .docs_utilities import get_docs_connector


# ============================================================================
# Persistent memory via Google Docs (wired up by default in agent.py)
#
# The template ships with these two memory tools already registered in
# `root_agent.tools`. The Google Doc ID comes from AGENT_MEMORY_DOC_ID in
# .env (get_started_linux.sh prompts for it). The doc must be shared
# (Editor access) with the per-agent SA email — that's the SA the
# Reasoning Engine runs as (see .agent_engine_config.json).
#
# If you don't want memory:
#   1. Remove the two FunctionTool entries from root_agent.tools in agent.py
#   2. Delete these two functions (or leave them — they'll just go unused)
#   3. Leave AGENT_MEMORY_DOC_ID unset (the tools no-op via the raise)
# ============================================================================

def get_agent_memory() -> str:
    """
    Retrieve the agent's persistent memory from the configured Google Doc.

    The doc ID comes from AGENT_MEMORY_DOC_ID in .env. The doc must be
    shared (Editor access) with the agent's runtime service account
    (BOT_ACCOUNT_ID@AGENT_PROJECT_ID.iam.gserviceaccount.com).

    Returns:
        The full text content of the memory document. May be an empty
        string if the doc has no content yet.

    Raises:
        ValueError: if AGENT_MEMORY_DOC_ID is not set in the environment.
        googleapiclient.errors.HttpError (403): if the doc hasn't been
            shared with the agent's runtime service account.
    """
    doc_id = os.environ.get("AGENT_MEMORY_DOC_ID")
    if not doc_id:
        raise ValueError(
            "AGENT_MEMORY_DOC_ID is not set. Either set it in .env (and on the "
            "deployed Reasoning Engine) or remove the memory tools from "
            "root_agent.tools in agent.py."
        )
    return get_docs_connector().read_doc(doc_id)


def update_agent_memory(updated_memory: str) -> Dict[str, Any]:
    """
    Replace the agent's persistent memory with the provided text.

    Use this at the end of a session (or whenever the agent has new
    information worth persisting) to write back updated notes. The
    write replaces the entire document body.

    Args:
        updated_memory: Complete new memory document text. This replaces
            the existing content — pass the full updated memory, not just
            the changes.

    Returns:
        API response confirming the update.

    Raises:
        ValueError: if AGENT_MEMORY_DOC_ID is not set in the environment.
        googleapiclient.errors.HttpError (403): if the doc hasn't been
            shared with the agent's runtime service account.
    """
    doc_id = os.environ.get("AGENT_MEMORY_DOC_ID")
    if not doc_id:
        raise ValueError(
            "AGENT_MEMORY_DOC_ID is not set. Either set it in .env (and on the "
            "deployed Reasoning Engine) or remove the memory tools from "
            "root_agent.tools in agent.py."
        )
    return get_docs_connector().write_doc(doc_id, updated_memory)
