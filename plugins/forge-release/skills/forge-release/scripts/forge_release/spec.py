"""The --spec JSON: who owns the App, its manifest, where each output goes, the remaining clicks.

infra prints it with `mise run github-app-manifest <name>` from an entry in github-apps.yaml."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional

from .manifest import FlowError

# What the manifest conversion and the install return, by the names outputs uses.
OUTPUTS = (
    "app_id",
    "client_id",
    "client_secret",
    "private_key",
    "webhook_secret",
    "installation_id",
)
STORES = ("pulumi", "sops")
OWNER_TYPES = ("user", "org")


class SpecError(FlowError):
    """The spec can't be used; the message says why."""


@dataclass(frozen=True)
class Spec:
    owner: str
    owner_type: str
    manifest: dict
    # output -> Pulumi stack config key, set under exactly this name
    pulumi: dict[str, str] = field(default_factory=dict)
    # outputs.sops.file as given (relative to the repo root) and output -> Secret data key
    sops_file: Optional[str] = None
    sops_keys: dict[str, str] = field(default_factory=dict)
    manual_steps: list[str] = field(default_factory=list)

    @property
    def org(self) -> Optional[str]:
        return self.owner if self.owner_type == "org" else None

    @property
    def public(self) -> bool:
        return bool(self.manifest.get("public"))

    def stores(self, output: str) -> bool:
        return output in self.pulumi or output in self.sops_keys


def _output_map(where: str, value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise SpecError(f"{where} must map outputs to key names")
    for output, key in value.items():
        if output not in OUTPUTS:
            raise SpecError(f"{where}: {output!r} isn't one of {', '.join(OUTPUTS)}")
        if not isinstance(key, str) or not key.strip():
            raise SpecError(f"{where}: {output} needs a key name")
    return dict(value)


def parse(doc: Any) -> Spec:
    if not isinstance(doc, Mapping):
        raise SpecError("the spec must be a JSON object")
    owner, owner_type = doc.get("owner"), doc.get("owner_type")
    if not isinstance(owner, str) or not owner:
        raise SpecError("the spec needs owner, the account that will own the App")
    if owner_type not in OWNER_TYPES:
        raise SpecError(f"the spec's owner_type must be one of {', '.join(OWNER_TYPES)}")
    app_manifest = doc.get("manifest")
    if not isinstance(app_manifest, Mapping) or not isinstance(app_manifest.get("name"), str):
        raise SpecError("the spec needs manifest, an App manifest with a name")
    outputs = doc.get("outputs") or {}
    if not isinstance(outputs, Mapping):
        raise SpecError("the spec's outputs must be an object")
    unknown = sorted(set(outputs) - set(STORES))
    if unknown:
        raise SpecError(
            f"the spec's outputs has unknown stores {', '.join(unknown)}; "
            f"known: {', '.join(STORES)}"
        )
    pulumi = _output_map("outputs.pulumi", outputs["pulumi"]) if "pulumi" in outputs else {}
    sops_file, sops_keys = None, {}
    if "sops" in outputs:
        sops = outputs["sops"]
        if not isinstance(sops, Mapping) or set(sops) != {"file", "keys"}:
            raise SpecError("outputs.sops must be {file, keys}")
        if not isinstance(sops["file"], str) or not sops["file"]:
            raise SpecError("outputs.sops.file must be a path relative to the repo root")
        sops_file, sops_keys = sops["file"], _output_map("outputs.sops.keys", sops["keys"])
    steps = doc.get("manual_steps") or []
    if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
        raise SpecError("the spec's manual_steps must be a list of strings")
    return Spec(
        owner=owner,
        owner_type=owner_type,
        manifest=dict(app_manifest),
        pulumi=pulumi,
        sops_file=sops_file,
        sops_keys=sops_keys,
        manual_steps=list(steps),
    )
