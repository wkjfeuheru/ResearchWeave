"""Bundle the optional web UI when it has been built; keep CLI installs independent."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        web_dist = Path(self.root) / "frontend" / "web" / "dist"
        if web_dist.is_dir():
            build_data["force_include"][str(web_dist)] = "openharness/_web"
