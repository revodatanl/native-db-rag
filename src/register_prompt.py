"""Register the system prompt as a new version and point `production` at it.

Runs as the `register_prompt` job (resources/prompt.yml), with the prompt's full name
`<catalog>.<schema>.system_prompt` as its only argument. The agent loads the
`production` alias when the app starts, so restart the app after a run.
"""

import sys

import mlflow

SYSTEM_PROMPT = """You answer questions about the company wiki.
Always call search_wiki before answering. If the results look thin, search again with a reformulated query.
Answer only from the results, and list the source paths you used. Say so when the wiki does not contain the answer."""


def main() -> None:
    """Register SYSTEM_PROMPT under the name in argv[1] and move the alias to the new version."""
    mlflow.set_registry_uri("databricks-uc")
    prompt = mlflow.genai.register_prompt(name=sys.argv[1], template=SYSTEM_PROMPT)
    mlflow.genai.set_prompt_alias(name=prompt.name, alias="production", version=prompt.version)
    print(f"Registered {prompt.name} version {prompt.version} and set the production alias.")


if __name__ == "__main__":
    main()
