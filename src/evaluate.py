# Databricks notebook source
# MAGIC %md
# MAGIC # Evaluate
# MAGIC
# MAGIC Scores what the agent retrieves and what it answers, on a fixed set of questions.
# MAGIC
# MAGIC Nobody writes the questions. On the first run, `generate_evals_df` writes them
# MAGIC from a sample of the wiki's pages, each with the facts a right answer contains
# MAGIC and the page it was written from, and they are stored as an MLflow evaluation
# MAGIC dataset in the bundle's schema. Later runs reuse that dataset, so scores compare
# MAGIC from run to run. Drop the table to have new questions written.

# COMMAND ----------

import os

import mlflow
from databricks.agents.evals import generate_evals_df
from mlflow.entities import SpanType, Trace
from mlflow.genai.datasets import create_dataset, get_dataset
from mlflow.genai.scorers import (
    Correctness,
    Guidelines,
    RelevanceToQuery,
    RetrievalGroundedness,
    RetrievalRelevance,
    RetrievalSufficiency,
    scorer,
)
from pyspark.sql import functions as F

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
num_questions = int(dbutils.widgets.get("num_questions"))
# The judges are a different, stronger model than the one that answers: a model grading itself favors its own answers.
judge = f"databricks:/{dbutils.widgets.get('judge_model')}"

# The agent reads these two when it is imported, below.
os.environ["LLM_MODEL"] = dbutils.widgets.get("llm_model")
os.environ["VECTOR_SEARCH_INDEX"] = f"{catalog}.{schema}.chunks_index"
# The app's experiment, so the scores sit next to the app's traces.
mlflow.set_experiment(experiment_id=dbutils.widgets.get("experiment_id"))

# COMMAND ----------

questions_table = f"{catalog}.{schema}.evaluation_questions"
if not spark.catalog.tableExists(questions_table):
    pages = (
        spark.table(f"{catalog}.{schema}.page_documents")
        # A page with next to no text gets a placeholder for a question, so only pages with something to ask about.
        .where(F.length("content") >= 500)
        .select("content", F.col("path").alias("doc_uri"))
        .orderBy(F.rand(seed=42))
        .limit(num_questions)
    )
    questions = generate_evals_df(
        pages,
        num_evals=num_questions,
        agent_description="A chat assistant that answers employees' questions from the pages of the company wiki.",
        # A question that repeats its page's wording is too easy to retrieve.
        question_guidelines="Ask as a colleague would who has not read the page: in their own words, not the page's.",
    )
    create_dataset(name=questions_table).merge_records(questions)
dataset = get_dataset(name=questions_table)

# COMMAND ----------

from confluence_rag.agent import agent


def answer(messages: list[dict]) -> str:
    """Ask the agent directly, not through the app; its LangChain autolog records the trace."""
    return agent.invoke({"messages": messages})["messages"][-1].text


@scorer
def source_page_retrieved(expectations: dict, trace: Trace) -> bool | None:
    """Did search_wiki return the page the question was written from? Checked in code, not by a judge."""
    expected = {page["doc_uri"] for page in (expectations or {}).get("expected_retrieved_context") or []}
    retrieved = {hit["path"] for span in trace.search_spans(span_type=SpanType.RETRIEVER) for hit in span.outputs or []}
    return bool(expected & retrieved) if expected else None


results = mlflow.genai.evaluate(
    data=dataset,
    predict_fn=answer,
    scorers=[
        # Retrieval: the right page, relevant passages, and enough in them to answer.
        source_page_retrieved,
        RetrievalRelevance(model=judge),
        RetrievalSufficiency(model=judge),
        # The answer: supported by the passages, on the question, and containing the expected facts.
        RetrievalGroundedness(model=judge),
        RelevanceToQuery(model=judge),
        Correctness(model=judge),
        Guidelines(
            name="cites_sources",
            guidelines="The response names the wiki paths it used, or says that the wiki does not answer the question.",
            model=judge,
        ),
    ],
)
print(results.metrics)

# A scorer that fails on a question leaves that score out without stopping the run. One that fails on every
# question is misconfigured (a judge model the workspace does not allow, say), so the job stops for that.
errors = results.result_df.filter(like="/error_message")
broken = errors.columns[errors.notna().all()]
if len(broken):
    raise RuntimeError(f"No scores from {list(broken)}. The first error: {errors[broken[0]].iloc[0]}")
