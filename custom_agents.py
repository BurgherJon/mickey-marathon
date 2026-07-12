"""
Sub-agents exposed as tools to Mickey's root agent.

google_search_agent handles web research — race course profiles (hills?),
typical race-day weather, training methodology questions. It's a separate
Agent because Google-Search grounding can't be mixed with function-calling
tools on the same agent.
"""
import os

from google.adk.agents import Agent
from google.adk.tools import google_search

google_search_agent = Agent(
    model=os.environ.get('QUICK_AGENT_MODEL', 'gemini-3-flash-preview'),
    name='google_search_agent',
    description=(
        'Performs Google searches and returns relevant results. Use for '
        'race research: course elevation profiles, typical weather for a '
        'race date/location, registration dates, and training-methodology '
        'questions.'
    ),
    instruction=(
        'Search the web for the query and return the most relevant, '
        'factual results. Prefer official race sites and course maps for '
        'race questions. Return concise findings with sources.'
    ),
    tools=[google_search],
)
