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

## Evaluate

```bash
databricks bundle run evaluate --profile <profile>
```

The `evaluate` job asks the agent a fixed set of questions and scores what it retrieved and what it answered. Run it once the wiki is loaded.

Nobody writes the questions. On the first run, Databricks writes 20 of them from a sample of the wiki's pages that have at least 500 characters of text ([`generate_evals_df`](https://docs.databricks.com/aws/en/agents/agent-evaluation/synthesize-evaluation-set)), each with the facts a right answer contains and the page it was written from. They are stored as an MLflow [evaluation dataset](https://docs.databricks.com/aws/en/mlflow3/genai/eval-monitor/build-eval-dataset), the table `evaluation_questions` in the bundle's schema. Later runs reuse it, so the scores of two runs can be compared. Drop the table to have new questions written.

| Score | Question it answers | How |
|---|---|---|
| `source_page_retrieved` | Did `search_wiki` return the page the question was written from? | code |
| `RetrievalRelevance` | Are the passages it returned about the question? | judge |
| `RetrievalSufficiency` | Do the passages hold what is needed to give the expected facts? | judge |
| `RetrievalGroundedness` | Is the answer supported by the passages? | judge |
| `RelevanceToQuery` | Does the answer address the question? | judge |
| `Correctness` | Does the answer agree with the expected facts? | judge |
| `cites_sources` | Does the answer name wiki paths, or say the wiki has no answer? The judge sees only the question and the answer. | judge |

The judges are MLflow's built-in ones, run on `judge_model`, a different and stronger model than `llm_model`, which answers (both in `databricks.yml`): a model that grades its own answers favors them. The questions go straight to the agent, not through the app, and the traces and scores land in the bundle's experiment, next to the app's. The retrieval judges score every search the agent made, so a question with three searches counts three times in their averages. The job prints the average of each score and does not fail on low ones; the evaluation run in the experiment shows every question with its trace and each judge's reasoning. It does fail when a scorer returns nothing at all, which is what a `judge_model` the workspace does not allow looks like: set the variable to one it does.

Generated questions are a smoke test, not the last word. They are written from one page each, so they are easier than what people really ask, and every one of them has an answer in the wiki. Nobody has checked the judges against a person's verdict either: read a few questions and a few verdicts before you trust the numbers. Then make the set yours in the experiment's **Datasets** tab: edit or remove questions, and add real ones, such as the answers that got a thumbs down.

The job runs as the identity that deployed the bundle, not as whoever clicks Run; `run_as` in `databricks.yml` changes that. That identity needs `EXECUTE` on both model services, which the app's grant does not reach. Writing the questions is a Databricks service: it needs partner-powered AI features enabled, and does not work in a workspace with serverless egress control.

## Tear down

```bash
databricks bundle destroy --profile <profile>
```

This deletes everything the bundle owns, including the schema and its data.

## What is not here, on purpose

- No attachments or images. The connector ships attachment metadata only, so images and attachments are left out. Image tags carry no text and drop out of the Markdown on their own.
- No prompt registry. The prompt is a constant in `src/confluence_rag/agent.py`; change it and redeploy. The [MLflow Prompt Registry](https://docs.databricks.com/aws/en/mlflow3/genai/prompt-version-mgmt/prompt-registry) would version it.
- No chat history. The UI runs in ephemeral mode, so there is no Lakebase.
- No per-user authorization. The app searches as its own service principal, so every user sees what the index holds.
