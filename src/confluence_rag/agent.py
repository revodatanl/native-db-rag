"""A LangGraph agent with one tool, `search_wiki`, served by MLflow's agent server.

MLflow's agent server speaks the OpenAI Responses API the chat UI expects, and proxies
the UI's pages. The UI streams, so only a stream handler is registered. The model
(a Unity Gateway model service), the index and the name of the system prompt come from
environment variables set in resources/app.yml.
"""

import os
from collections.abc import AsyncGenerator

import mlflow
from databricks.sdk import WorkspaceClient
from databricks_langchain import ChatDatabricks
from langchain.agents import create_agent
from langchain.tools import tool
from mlflow.genai.agent_server import AgentServer, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentStreamEvent,
    output_to_responses_items_stream,
    to_chat_completions_input,
)

mlflow.langchain.autolog()

# Loaded once, at startup: restart the app after moving the production alias.
system_prompt = mlflow.genai.load_prompt(f"prompts:/{os.environ['SYSTEM_PROMPT_NAME']}@production").template
if not isinstance(system_prompt, str):
    raise TypeError("The system prompt must be a text prompt.")

w = WorkspaceClient()


@tool
def search_wiki(query: str) -> list[dict]:
    """Search the company wiki for passages relevant to a query."""
    response = w.vector_search_indexes.query_index(
        index_name=os.environ["VECTOR_SEARCH_INDEX"],
        columns=["chunk_to_retrieve", "path"],
        query_text=query,
        query_type="HYBRID",
        num_results=5,
    )
    rows = response.result.data_array if response.result else None
    return [{"text": text, "path": path} for text, path, _score in rows or []]


agent = create_agent(
    model=ChatDatabricks(model=os.environ["LLM_MODEL"], use_ai_gateway=True),
    tools=[search_wiki],
    system_prompt=system_prompt,
)


@stream()
async def stream_handler(request: ResponsesAgentRequest) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    """Emit each step's completed items: the tool call, its results, and the answer."""
    messages = to_chat_completions_input([item.model_dump() for item in request.input])
    async for step in agent.astream({"messages": messages}, stream_mode="updates"):
        for node in step.values():
            for event in output_to_responses_items_stream(node["messages"]):
                yield event


server = AgentServer("ResponsesAgent", enable_chat_proxy=True)
