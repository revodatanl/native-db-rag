"""A LangGraph agent with one tool, `search_wiki`, served by MLflow's agent server.

MLflow's agent server speaks the OpenAI Responses API the chat UI expects, and proxies
the UI's pages. The UI streams, so only a stream handler is registered. The model
(a Unity Gateway model service) and the index come from environment variables set in
resources/app.yml.
"""

import json
import os
from collections.abc import AsyncGenerator

import mlflow
from databricks.sdk import WorkspaceClient
from databricks_langchain import ChatDatabricks
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage
from mlflow.genai.agent_server import AgentServer, stream
from mlflow.types.responses import (
    ResponsesAgentRequest,
    ResponsesAgentStreamEvent,
    output_to_responses_items_stream,
    to_chat_completions_input,
)

mlflow.langchain.autolog()

SYSTEM_PROMPT = """You answer questions about the company wiki.
Always call search_wiki before answering. If the results look thin, search again with a reformulated query.
Answer only from the results, and list the source paths you used. Say so when the wiki does not contain the answer."""

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
    system_prompt=SYSTEM_PROMPT,
)


def answer_text(message: AIMessage) -> str:
    """Return the text blocks of a model message, without its reasoning block.

    A model that thinks answers with a list of typed blocks, which ChatDatabricks hands
    over as one JSON string that starts with the reasoning block. The answer is the
    blocks of type text, and nothing else.
    """
    content = str(message.text)
    if content.startswith('[{"type": "reasoning"'):
        return "".join(block["text"] for block in json.loads(content) if block["type"] == "text")
    return content


@stream()
async def stream_handler(request: ResponsesAgentRequest) -> AsyncGenerator[ResponsesAgentStreamEvent, None]:
    """Emit each step's completed items: the tool call, its results, and the answer."""
    messages = to_chat_completions_input([item.model_dump() for item in request.input])
    async for step in agent.astream({"messages": messages}, stream_mode="updates"):
        for node in step.values():
            # Copies for display only: the graph keeps the reasoning, which the model needs on later turns.
            shown = (
                message.model_copy(update={"content": answer_text(message)})
                if isinstance(message, AIMessage)
                else message
                for message in node["messages"]
            )
            for event in output_to_responses_items_stream(shown):
                yield event


server = AgentServer("ResponsesAgent", enable_chat_proxy=True)
