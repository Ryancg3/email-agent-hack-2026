"""The chat model every Claude call goes through, with whichever credential is set.

An API key (sk-ant-api..., as on Railway) goes in the x-api-key header as usual. A
bearer token (sk-ant-usr..., in ANTHROPIC_AUTH_TOKEN, or by mistake in
ANTHROPIC_API_KEY) goes in the Authorization header, which LangChain doesn't
fill in from the environment itself. ANTHROPIC_AUTH_TOKEN wins when both are set,
so a stale key exported in the shell can't shadow it.
"""

import os

from langchain_anthropic import ChatAnthropic

BEARER_PREFIX = "sk-ant-usr"


def credential() -> tuple[str, str]:
    """("token" or "api_key" or "none", where it came from), without the secret."""
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if token:
        return "token", "ANTHROPIC_AUTH_TOKEN"
    if key.startswith(BEARER_PREFIX):
        return "token", "ANTHROPIC_API_KEY (a bearer token, sent as one)"
    if key:
        return "api_key", "ANTHROPIC_API_KEY"
    return "none", "nothing set"


def chat_model(model: str, max_tokens: int) -> ChatAnthropic:
    kind, _ = credential()
    if kind == "token":
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip() or os.environ["ANTHROPIC_API_KEY"].strip()
        # api_key "" keeps a stale exported key out of the x-api-key header.
        return ChatAnthropic(model=model, max_tokens=max_tokens, api_key="",
                             default_headers={"Authorization": f"Bearer {token}"})
    return ChatAnthropic(model=model, max_tokens=max_tokens)


if __name__ == "__main__":
    # make check-key: which credential is used, and whether Anthropic accepts it (free: lists models).
    kind, source = credential()
    print(f"Credential: {kind} from {source}")
    try:
        model = next(iter(chat_model("claude-haiku-5-5", 10)._client.models.list(limit=1))).id
        print(f"Works: Anthropic answered (newest model {model}).")
    except Exception as e:
        print(f"Fails: {type(e).__name__}: {str(e)[:200]}")
