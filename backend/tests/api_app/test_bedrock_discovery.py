"""
Bedrock discovery lists inference profiles as models (F66).

A model like Claude Sonnet 4.5 on Bedrock has no on-demand throughput: it can
only be invoked through an inference profile (`us.anthropic.…`, or an
application profile's ARN). Discovery listed the bare foundation model, which
fails on every call, and never the profile — the only way to reach one was the
workspace-wide `bedrock_inference_profile` override, which replaced every
Bedrock model with it. Now the profile is the model you pick.
"""
import pytest

FOUNDATION = {"modelSummaries": [
    {"modelId": "amazon.nova-pro-v1:0", "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0",
     "outputModalities": ["TEXT"], "inferenceTypesSupported": ["ON_DEMAND"]},
    {"modelId": "anthropic.claude-sonnet-4-5-20250929-v1:0",
     "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
     "outputModalities": ["TEXT"], "inferenceTypesSupported": ["INFERENCE_PROFILE"]},
    {"modelId": "amazon.titan-embed-text-v2:0",
     "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0",
     "outputModalities": ["EMBEDDING"], "inferenceTypesSupported": ["ON_DEMAND"]},
    {"modelId": "cohere.embed-v4:0", "modelArn": "arn:aws:bedrock:us-east-1::foundation-model/cohere.embed-v4:0",
     "outputModalities": ["EMBEDDING"], "inferenceTypesSupported": ["INFERENCE_PROFILE"]},
]}

APP_ARN = "arn:aws:bedrock:us-east-1:123456789012:application-inference-profile/team-sonnet"

PROFILES_PAGE_1 = {"inferenceProfileSummaries": [
    {"inferenceProfileId": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
     "inferenceProfileArn": "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
     "inferenceProfileName": "US Claude Sonnet 4.5", "status": "ACTIVE", "type": "SYSTEM_DEFINED",
     "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"}]},
], "nextToken": "page2"}

PROFILES_PAGE_2 = {"inferenceProfileSummaries": [
    {"inferenceProfileId": "team-sonnet", "inferenceProfileArn": APP_ARN,
     "inferenceProfileName": "Team Sonnet", "status": "ACTIVE", "type": "APPLICATION",
     "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0"}]},
    {"inferenceProfileId": "us.cohere.embed-v4:0", "inferenceProfileArn": "arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.cohere.embed-v4:0",
     "inferenceProfileName": "US Cohere Embed v4", "status": "ACTIVE", "type": "SYSTEM_DEFINED",
     "models": [{"modelArn": "arn:aws:bedrock:us-east-1::foundation-model/cohere.embed-v4:0"}]},
]}


@pytest.fixture
def bedrock_account(monkeypatch):
    import core.routes.data as data

    def management_get(path, region, api_key, params=None):
        if path == "/foundation-models":
            return FOUNDATION
        if path == "/inference-profiles":
            return PROFILES_PAGE_2 if (params or {}).get("nextToken") == "page2" else PROFILES_PAGE_1
        raise AssertionError(path)

    monkeypatch.setattr(data, "_bedrock_management_get", management_get)
    monkeypatch.setattr(data, "load_settings", lambda: {"bedrock_api_key": "ABSKexample", "aws_region": "us-east-1"})


class TestProfilesAreModels:
    async def test_profiles_are_listed_and_profile_only_models_are_not(self, client, bedrock_account):
        bedrock = (await client.get("/api/models")).json()["providers"]["bedrock"]

        assert bedrock["available"] is True
        assert "bedrock.us.anthropic.claude-sonnet-4-5-20250929-v1:0" in bedrock["models"]
        assert f"bedrock.{APP_ARN}" in bedrock["models"]
        assert "bedrock.amazon.nova-pro-v1:0" in bedrock["models"]
        # Only invocable through a profile: listing it offered a model that fails on every call.
        assert "bedrock.anthropic.claude-sonnet-4-5-20250929-v1:0" not in bedrock["models"]

    async def test_an_embedding_profile_is_an_embedding_model(self, client, bedrock_account):
        bedrock = (await client.get("/api/models")).json()["providers"]["bedrock"]

        assert "bedrock.us.cohere.embed-v4:0" in bedrock["embedding_models"]
        assert "bedrock.us.cohere.embed-v4:0" not in bedrock["models"]
        assert "bedrock.amazon.titan-embed-text-v2:0" in bedrock["embedding_models"]
        assert "bedrock.cohere.embed-v4:0" not in bedrock["embedding_models"]

    async def test_the_bedrock_model_list_carries_them_too(self, client, bedrock_account):
        models = (await client.get("/api/bedrock/models")).json()["models"]
        assert "bedrock.us.anthropic.claude-sonnet-4-5-20250929-v1:0" in models
        assert f"bedrock.{APP_ARN}" in models
        assert "bedrock.anthropic.claude-sonnet-4-5-20250929-v1:0" not in models

    async def test_a_listing_that_cannot_read_profiles_still_lists_models(self, client, monkeypatch, bedrock_account):
        import core.routes.data as data
        real = data._bedrock_management_get

        def no_profiles(path, region, api_key, params=None):
            if path == "/inference-profiles":
                raise PermissionError("bedrock:ListInferenceProfiles denied")
            return real(path, region, api_key, params)

        monkeypatch.setattr(data, "_bedrock_management_get", no_profiles)
        bedrock = (await client.get("/api/models")).json()["providers"]["bedrock"]
        assert "bedrock.amazon.nova-pro-v1:0" in bedrock["models"]
