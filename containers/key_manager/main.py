from __future__ import annotations

from cove_container_runtime.certificates import generate_ed25519_keypair_material
from cove_container_runtime.common import (
    RuntimeErrorBase,
    SidecarConfigError,
    load_inline_sidecar_context,
    log,
    required_mapping,
    required_string,
    write_json_file,
    write_text_file,
)


def run(config: dict[str, object]) -> None:
    keypairs = config.get("keypairs")
    if not isinstance(keypairs, list):
        raise SidecarConfigError("keypairs must be a list")

    for raw_keypair in keypairs:
        keypair = required_mapping(raw_keypair, "keypair")
        keypair_name = required_string(keypair, "name")
        algorithm = required_string(keypair, "algorithm")
        if algorithm != "ed25519":
            raise RuntimeErrorBase(f"unsupported keypair algorithm: {algorithm}")

        private_key_path = required_string(keypair, "private_key_path")
        public_key_path = required_string(keypair, "public_key_path")
        certificate_path = required_string(keypair, "certificate_path")
        metadata_path = required_string(keypair, "metadata_path")
        certificate_common_name = required_string(keypair, "certificate_common_name")

        material = generate_ed25519_keypair_material(
            keypair_name=keypair_name,
            certificate_common_name=certificate_common_name,
        )
        write_text_file(private_key_path, material.private_key_pem)
        write_text_file(public_key_path, material.public_key_pem)
        write_text_file(certificate_path, material.certificate_pem)
        write_json_file(metadata_path, material.metadata)
        log("key_manager", f"generated keypair {keypair_name}")


def main() -> int:
    try:
        run(load_inline_sidecar_context().config)
        return 0
    except (RuntimeErrorBase, SidecarConfigError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
