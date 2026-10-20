# native-db-rag

Chat with your Confluence wiki on Databricks, built from native building blocks only. The [article](https://medium.com/@brouwer.thomas/a-minimal-confluence-rag-on-databricks-5948c064deda?source=friends_link&sk=defef2055df856b848d0efdc73ae0154) walks through the code.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) for the Python side.
- [Databricks CLI](https://docs.databricks.com/dev-tools/cli/index.html), version 1.3.0 or above.
- A workspace with serverless compute, Lakeflow Connect, AI Search, and Databricks Apps enabled.
- A Unity Catalog catalog you can create schemas in (default `main`). The app's service principal needs `USE CATALOG` on it.
- A Unity Catalog connection of type **Confluence** (here named `confluence`), created once in Catalog Explorer > Connections (OAuth needs a browser, so there is no CLI equivalent).

## Deploy

```bash
databricks bundle validate --profile <profile>
databricks bundle deploy --profile <profile>
```

The first deploy also creates the AI Search endpoint, which takes a few minutes to come online. Deploying uploads the app's code but does not start it; start it once (later deploys of new code need the same command):

```bash
databricks bundle run chat --profile <profile>
```

The principal exists once the app does, so the catalog grant comes after the first deploy. `main` grants `USE CATALOG` to `account users`, which includes service principals, so nothing is needed there. On another catalog, its owner runs:

```bash
# Look up the app's service principal.
principal=$(databricks apps get confluence-rag -o json --profile <profile> | grep service_principal_client_id | cut -d '"' -f 4)

# Let it use the catalog.
databricks grants update catalog <catalog> --profile <profile> \
  --json '{"changes": [{"principal": "'$principal'", "add": ["USE_CATALOG"]}]}'
```

`bundle destroy` deletes the principal with the app, so a later deploy needs the grant again. The schema grants need no such step: the bundle applies them on every deploy.

## Load the wiki

```bash
databricks bundle run refresh --profile <profile>
```

One job, three tasks:

1. **ingest**: the connector lands pages, blog posts, spaces, labels, classification levels and attachments in the bundle's schema. Page bodies arrive in Confluence storage format (XHTML); attachments arrive as metadata plus a download link.
2. **documents**: the pipeline streams the connector's change feed into `page_documents` (Markdown and chunks from `ai_prep_search`, one row per current page) and keeps `chunks` (one row per chunk, Change Data Feed on) in step with Auto CDC. A run converts and chunks only the pages that changed, removes the pages that left the wiki, and touches only the chunks that changed. Only pages are used: the connector does not ship attachment files, so images and attachments are left out.
3. **index**: creates the `chunks_index` Delta Sync index on the first run and syncs it on later runs. Only changed chunks produce change events, so only those are re-embedded.

The first run processes everything; later runs chunk only the pages that changed. The initial embedding of the index runs in the background after the job.

## Use it

Open the URL that `bundle run chat` printed and ask something only your wiki knows. The agent is one file, `src/confluence_rag/agent.py`: a LangGraph agent whose only tool, `search_wiki`, runs a hybrid search on the index and returns each hit's `chunk_to_retrieve` and path. The model decides how to phrase the search, searches again when results are thin, and cites the paths it used. The search, its results, and the answer appear as each step finishes; every answer is traced in the MLflow experiment the bundle creates.

The server is MLflow's `AgentServer`, from Databricks' [agent-langgraph](https://github.com/databricks/app-templates/tree/main/agent-langgraph) template. The chat UI is Databricks' [e2e-chatbot-app-next](https://github.com/databricks/app-templates/tree/main/e2e-chatbot-app-next) template, cloned from GitHub and built with npm when the app starts, so the first start takes a few minutes and needs outbound access to GitHub.

The app searches as its own service principal: the bundle grants it `USE SCHEMA` and `SELECT` on the schema, which covers the index and anything added to the schema later. Everyone in the organization can open it: the bundle grants `CAN_USE` to `account users`, and users sign in with their Databricks SSO.

## Tear down

```bash
databricks bundle destroy --profile <profile>
```

This deletes everything the bundle owns, including the schema and its data.

## What is not here, on purpose

- No attachments or images. The connector ships attachment metadata only, so images and attachments are left out. Image tags carry no text and drop out of the Markdown on their own.
- No prompt registry. The prompt is a constant in `src/confluence_rag/agent.py`; change it and redeploy. The [MLflow Prompt Registry](https://docs.databricks.com/aws/en/mlflow3/genai/prompt-version-mgmt/prompt-registry) would version it.
- No chat history. The UI runs in ephemeral mode, so there is no Lakebase.
- No evaluation loop. Nothing quantifies the quality of the answers yet.
- No per-user authorization. The app searches as its own service principal, so every user sees what the index holds.
