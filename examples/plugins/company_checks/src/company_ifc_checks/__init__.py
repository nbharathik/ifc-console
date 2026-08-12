"""Example typed operation plugin for ifc-console."""

from pydantic import BaseModel, ConfigDict

from ifc_console import Capability, Envelope, PluginAPI, PluginManifest


class CompanyStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str
    ready: bool


class CompanyChecks:
    manifest = PluginManifest(
        api_version="1",
        name="company_checks",
        version="0.1.0",
        description="Example company submission checks.",
    )

    def register(self, api: PluginAPI) -> None:
        @api.registry.tool(
            name="company_checks_status",
            description="Return the active model submission status.",
            data_model=CompanyStatus,
            required_capabilities=[Capability.MODEL_READ],
        )
        async def company_checks_status() -> Envelope:
            session = api.core.session
            session.require_loaded()
            return api.success(
                {"model": session.name, "ready": not session.dirty},
                plugin=self.manifest.name,
            )
