"""Write THIRD_PARTY_NOTICES.md and copy dependency license files to third_party_licenses/.

Run in the project's environment after changing dependencies:

    uv run python scripts/third_party_notices.py

Only runtime dependencies (and their dependencies) are listed: the packages shipped with
the app. Development tools such as pytest and ruff are not shipped and not listed.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
LICENSES_DIR = ROOT / "third_party_licenses"
NOTICES_FILE = ROOT / "THIRD_PARTY_NOTICES.md"
_LICENSE_FILE_RE = re.compile(r"^(licen[cs]e|copying|notice|authors)", re.IGNORECASE)
# Include a dependency if it is needed on this machine or in the Linux container.
_ENVIRONMENTS = [
    default_environment(),
    {
        **default_environment(),
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
    },
]


def _needed(requirement: Requirement, active_extras: set[str]) -> bool:
    """Whether a dependency applies on this machine or in the Linux container, given the
    extras requested for the package that declares it (e.g. pyjwt[crypto])."""
    if requirement.marker is None:
        return True
    return any(
        requirement.marker.evaluate({**env, "extra": extra})
        for env in _ENVIRONMENTS
        for extra in {"", *active_extras}
    )


def distributions_for(specs: list[str]) -> tuple[dict[str, metadata.Distribution], list[str]]:
    """The installed distributions needed by ``specs``, following their dependencies and
    the extras each requirement asks for."""
    pending: list[tuple[Requirement, set[str]]] = [(Requirement(spec), set()) for spec in specs]
    found: dict[str, metadata.Distribution] = {}
    extras_of: dict[str, set[str]] = {}
    missing: list[str] = []
    while pending:
        requirement, declaring_extras = pending.pop()
        if not _needed(requirement, declaring_extras):
            continue
        name = canonicalize_name(requirement.name)
        wanted = set(requirement.extras)
        if name in missing or (name in found and wanted <= extras_of[name]):
            continue
        try:
            dist = found.get(name) or metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError:
            missing.append(name)  # e.g. a Linux-only dependency when run on Windows
            continue
        found[name] = dist
        extras_of[name] = extras_of.get(name, set()) | wanted
        pending.extend((Requirement(spec), extras_of[name]) for spec in dist.requires or [])
    return dict(sorted(found.items())), sorted(missing)


def license_of(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    if expression := meta.get("License-Expression"):
        return expression
    classifiers = [
        c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License ::")
    ]
    if classifiers:
        return ", ".join(classifiers)
    text = (meta.get("License") or "").strip()
    return text if text and len(text) <= 80 else "see license file"


def project_url(dist: metadata.Distribution) -> str:
    meta = dist.metadata
    if home := meta.get("Home-page"):
        return home
    urls = {}
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if url.strip():
            urls[label.strip().lower()] = url.strip()
    for label in ("homepage", "home", "source", "source code", "repository", "documentation"):
        if label in urls:
            return urls[label]
    return next(iter(urls.values()), "")


def copy_license_files(dist: metadata.Distribution, target: Path) -> list[str]:
    copied = []
    for file in dist.files or []:
        if not _LICENSE_FILE_RE.match(Path(file).name):
            continue
        if "dist-info" not in str(file) and "licenses" not in str(file):
            continue
        source = Path(dist.locate_file(file))
        if source.is_file():
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target / source.name)
            copied.append(source.name)
    return sorted(set(copied))


def table_rows(distributions: dict[str, metadata.Distribution]) -> list[str]:
    rows = []
    for name, dist in distributions.items():
        version = dist.version
        folder = LICENSES_DIR / f"{name}-{version}"
        files = copy_license_files(dist, folder)
        where = f"`third_party_licenses/{folder.name}/`" if files else "not included in the package"
        rows.append(f"| {name} | {version} | {license_of(dist)} | {project_url(dist)} | {where} |")
    return rows


def main() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    core, missing = distributions_for(project["dependencies"])
    extra_all, extra_missing = distributions_for(
        project.get("optional-dependencies", {}).get("claude-code", [])
    )
    extra = {name: dist for name, dist in extra_all.items() if name not in core}
    if LICENSES_DIR.exists():
        shutil.rmtree(LICENSES_DIR)
    header = [
        "| Package | Version | License | Project URL | License files |",
        "|---|---|---|---|---|",
    ]

    lines = [
        "# Third-party notices",
        "",
        "The project's own code and documentation are MIT-licensed (see `LICENSE`). The app",
        "depends on the open-source packages below, which keep their own licenses. License",
        "files shipped with each package are copied to `third_party_licenses/`.",
        "",
        "Generated by `scripts/third_party_notices.py` from the installed dependencies.",
        "Development tools (pytest, ruff) are not shipped and therefore not listed.",
        "",
        "## Runtime dependencies",
        "",
        *header,
        *table_rows(core),
        "",
    ]
    if extra:
        lines += [
            "## Optional extra `claude-code` (local use with a Claude subscription)",
            "",
            "Installed only with `uv sync --extra claude-code`; not part of the default app or",
            "a Docker image. The `claude-agent-sdk` wheel also bundles the Claude Code CLI,",
            "which is Anthropic's proprietary software, used under Anthropic's terms. It is",
            "not part of this repository and is not redistributed by it.",
            "",
            *header,
            *table_rows(extra),
            "",
        ]
    missing = sorted(set(missing) | set(extra_missing))
    if missing:
        lines += [
            "Not installed where this file was generated (platform-specific, listed for "
            f"completeness): {', '.join(missing)}.",
            "",
        ]
    lines += [
        "Other material: the synthetic evaluation data in `eval/` and all documentation were",
        "written for this project. No third-party images, fonts or datasets are included.",
        "",
    ]
    NOTICES_FILE.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"{len(core)} runtime and {len(extra)} optional packages listed in {NOTICES_FILE.name}")


if __name__ == "__main__":
    main()
