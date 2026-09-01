"""Scaffold a copy, run the SBF build, and print the manifest cargo actually emitted."""
import asyncio, json, sys
from pathlib import Path
from composer.cargo.sbf import Built, sbf_build
from composer.cargo.session import CargoSession
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.conf import DEFAULT_FEATURE, TEMPLATE_BASE, tools_version
from composer.spec.cvlr.preflight import prepare_workspace


async def main(root: Path) -> int:
    pre = await prepare_workspace(root, package="vault")
    session = CargoSession(workdir=pre.workspace_root, sandbox=SandboxConfig.from_env())
    await session.warm()
    run = await sbf_build(
        session,
        manifest_path=pre.workspace_root / pre.package_dir / "Cargo.toml",
        features=(DEFAULT_FEATURE,),
        tools_version=tools_version(dict(TEMPLATE_BASE)),
    )
    if not isinstance(run.verdict, Built):
        print("BUILD FAILED:", run.verdict)
        return 1
    m = run.verdict.manifest
    print("project_directory:", m.project_directory)
    print("executables:", m.executables)
    print("sources:", json.dumps(list(m.sources), indent=2))
    print("solana_inlining:", m.solana_inlining)
    print("solana_summaries:", m.solana_summaries)
    for s in m.sources:
        p = m.project_directory / s
        print(f"  exists? {p.exists()}  {s}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(Path(sys.argv[1]).resolve())))
