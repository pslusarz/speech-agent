import asyncio
import os

import boto3
from anthropic import AnthropicBedrock, AsyncAnthropicBedrock
from anthropic.types import Message

from . import llm_cache

MODEL = os.getenv("ANTHROPIC_MODEL", "us.anthropic.claude-sonnet-4-6")
PROFILE = os.getenv("AWS_PROFILE", "development")
REGION = os.getenv("AWS_REGION", "us-east-1")


def _creds():
    c = (
        boto3.Session(profile_name=PROFILE, region_name=REGION)
        .get_credentials()
        .get_frozen_credentials()
    )
    return dict(
        aws_access_key=c.access_key,
        aws_secret_key=c.secret_key,
        aws_session_token=c.token,
        aws_region=REGION,
    )


def _cache(cli):
    """Replay identical requests from llm_cache.jsonl instead of calling Bedrock."""
    if not llm_cache.ENABLED:
        return cli
    inner = cli.messages.create

    def create(**kw):
        k = llm_cache.key(kw)
        hit = llm_cache.get(k)
        if hit is not None:
            return Message.model_validate(hit)
        r = inner(**kw)
        llm_cache.put(k, kw, r.model_dump(mode="json"))
        return r

    async def acreate(**kw):
        k = llm_cache.key(kw)
        hit = llm_cache.get(k)
        if hit is not None:
            # a real call suspends; cached replay must too, or spawned tasks never run
            await asyncio.sleep(0)
            return Message.model_validate(hit)
        r = await inner(**kw)
        llm_cache.put(k, kw, r.model_dump(mode="json"))
        return r

    cli.messages.create = acreate if isinstance(cli, AsyncAnthropicBedrock) else create
    return cli


def raw_client() -> AnthropicBedrock:
    return _cache(AnthropicBedrock(**_creds()))
