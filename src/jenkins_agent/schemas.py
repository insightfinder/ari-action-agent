"""Internal config model for what this agent kind expects inside a
request's `jenkins` DataPart block — documentation of the expected shape,
same status as ari_oncall_agent.schemas.RepoConfig (see common/app_factory.py
et al.: nothing at runtime actually instantiates/validates these today;
capabilities read `ctx.config["jenkins"]` as a plain dict)."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict


class JenkinsAuthConfig(BaseModel):
    type: Literal["api_token", "bearer", "basic"] = "api_token"
    user: Optional[str] = None  # required for api_token/basic
    token: str

    model_config = ConfigDict(extra="allow")


class JenkinsConfig(BaseModel):
    base_url: str
    auth: JenkinsAuthConfig
    verify_tls: bool = True
    ca_bundle: Optional[str] = None  # PEM content, not a path — this agent stores no files between requests
    # A tenant-level switch, independent of what the token could actually
    # do: force read-only regardless of what the user asks (design intent,
    # not yet enforced anywhere in code — see docs/design.md's "propose
    # only when the credential check shows the token can trigger builds").
    allow_mutations: bool = True

    model_config = ConfigDict(extra="allow")
