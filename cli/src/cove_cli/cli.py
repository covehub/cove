from __future__ import annotations

import argparse
from collections.abc import Sequence

from .check import check_workflow, format_report
from .compile import CompileCommandError, compile_workflow
from .config import ConfigError
from .deploy import DeployCommandError, PhalaDeployOptions, deploy_workflow
from .hub import HubCommandError, get_hub_object_command, inspect_hub_object_command
from .init_command import InitCommandError, initialize_cove_home
from .provision import (
    ProvisionCommandError,
    allow_artifact_for_compose,
    inspect_and_allow,
    provision_artifact,
    reset_runtime_state,
    serve_provisioner,
    start_owner_service,
)
from .publish import PublishCommandError, pull_workflow, push_workflow


def run(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            print(initialize_cove_home(cove_home=args.cove_home))
            return 0

        if args.command == "start":
            return start_owner_service(args.port, cove_home=args.cove_home)

        if args.command == "check":
            report = check_workflow(
                args.workflow_path,
                cove_home=args.cove_home,
            )
            print(format_report(report))
            return 0 if report.ok else 1

        if args.command == "compile":
            print(
                compile_workflow(
                    args.workflow_path,
                    cove_home=args.cove_home,
                )
            )
            return 0

        if args.command == "push":
            print(
                push_workflow(
                    args.workflow_path,
                    cove_home=args.cove_home,
                    overwrite=args.overwrite,
                )
            )
            return 0

        if args.command == "pull":
            print(
                pull_workflow(
                    args.published_ref,
                    args.destination,
                    cove_home=args.cove_home,
                )
            )
            return 0

        if args.command == "hub":
            if args.hub_command == "get":
                print(
                    get_hub_object_command(
                        args.hub_path,
                        server_url=args.server_url,
                        output=args.output,
                        cove_home=args.cove_home,
                    )
                )
                return 0
            if args.hub_command == "inspect":
                print(
                    inspect_hub_object_command(
                        args.hub_path,
                        server_url=args.server_url,
                        cove_home=args.cove_home,
                    )
                )
                return 0

        if args.command == "deploy":
            print(
                deploy_workflow(
                    args.published_ref,
                    cove_home=args.cove_home,
                    phala_options=PhalaDeployOptions(
                        instance_type=args.phala_instance_type,
                        region=args.phala_region,
                        os_image=args.phala_os_image,
                        node_id=args.phala_node_id,
                        disk_size_gb=args.phala_disk_size_gb,
                        public_logs=args.phala_public_logs,
                        public_sysinfo=args.phala_public_sysinfo,
                        listed=args.phala_listed,
                        docker_username=args.phala_docker_username,
                        docker_access_token=args.phala_docker_access_token,
                        docker_registry=args.phala_docker_registry,
                        staged_launch=args.staged_launch,
                        workflow_node_id=args.workflow_node,
                        dependency_timeout_seconds=args.dependency_timeout_seconds,
                        dependency_poll_interval_seconds=args.dependency_poll_interval_seconds,
                    ),
                )
            )
            return 0

        if args.command == "reset-runtime":
            print(
                reset_runtime_state(
                    args.published_ref,
                    cove_home=args.cove_home,
                )
            )
            return 0

        if args.command == "provision":
            if args.first_arg == "serve":
                if args.second_arg is None:
                    port = None
                else:
                    try:
                        port = int(args.second_arg)
                    except ValueError as exc:
                        raise ProvisionCommandError("port must be an integer") from exc
                return serve_provisioner(port, cove_home=args.cove_home)

            if args.first_arg == "tunnel-config":
                raise ProvisionCommandError(
                    "cove provision tunnel-config has been removed; configure explicit owner_server_url values instead"
                )

            if args.first_arg == "allow":
                if args.second_arg is None or args.third_arg is None:
                    parser.error(
                        "cove provision allow requires '<artifact_id> <compose_file_path>'"
                    )
                print(
                    allow_artifact_for_compose(
                        args.second_arg,
                        args.third_arg,
                        cove_home=args.cove_home,
                    )
                )
                return 0

            if args.first_arg == "inspect":
                if args.second_arg is None:
                    parser.error(
                        "cove provision inspect requires '<publisher>/<workflow_id>'"
                    )
                print(
                    inspect_and_allow(
                        args.second_arg,
                        cove_home=args.cove_home,
                    )
                )
                return 0

            if args.first_arg is None or args.second_arg is None:
                parser.error(
                    "cove provision requires either 'serve [port]', "
                    "'allow <artifact_id> <compose_file_path>', "
                    "'inspect <publisher>/<workflow_id>', or "
                    "'<artifact_name> <file_path>'"
                )

            print(
                provision_artifact(
                    args.first_arg,
                    args.second_arg,
                    cove_home=args.cove_home,
                    overwrite=args.overwrite,
                )
            )
            return 0

    except (
        CompileCommandError,
        DeployCommandError,
        HubCommandError,
        InitCommandError,
        PublishCommandError,
        ProvisionCommandError,
        ConfigError,
    ) as exc:
        print(f"ERROR: {exc}")
        return 1

    parser.error(f"unsupported command: {args.command}")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cove")
    parser.add_argument(
        "--cove-home",
        help="Path to the local Cove home (defaults to ~/.cove or COVE_HOME)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "init",
        help="Interactively initialize a local Cove home",
    )

    start_parser = subparsers.add_parser(
        "start",
        help="Run the local owner identity and provisioning service",
    )
    start_parser.add_argument(
        "port",
        nargs="?",
        type=int,
        help="Local owner service port (defaults to 9000)",
    )

    check_parser = subparsers.add_parser(
        "check",
        help="Validate a static-only Cove workflow",
    )
    check_parser.add_argument(
        "workflow_path",
        nargs="?",
        help="Path to workflow.cove.yaml (defaults to ./workflow.cove.yaml)",
    )

    compile_parser = subparsers.add_parser(
        "compile",
        help="Generate sidecar-injected node Compose files for a workflow",
    )
    compile_parser.add_argument(
        "workflow_path",
        nargs="?",
        help="Path to workflow.cove.yaml (defaults to ./workflow.cove.yaml)",
    )

    push_parser = subparsers.add_parser(
        "push",
        help="Publish a compiled workflow bundle",
    )
    push_parser.add_argument(
        "workflow_path",
        nargs="?",
        help="Path to workflow.cove.yaml (defaults to ./workflow.cove.yaml)",
    )
    push_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing workflow slot on Covehub",
    )

    pull_parser = subparsers.add_parser(
        "pull",
        help="Pull a published workflow bundle",
    )
    pull_parser.add_argument(
        "published_ref",
        help="Published workflow ref in the form <publisher>/<workflow_id>",
    )
    pull_parser.add_argument(
        "destination",
        nargs="?",
        help="Optional destination directory for the pulled workflow bundle",
    )

    hub_parser = subparsers.add_parser(
        "hub",
        help="Read public CoveHub objects without authenticating",
    )
    hub_subparsers = hub_parser.add_subparsers(dest="hub_command", required=True)

    hub_get_parser = hub_subparsers.add_parser(
        "get",
        help="Download one public CoveHub object by typed hub path",
    )
    hub_get_parser.add_argument("hub_path", help="Typed path such as v1/workflows/alice.example.test/demo/latest")
    hub_get_parser.add_argument(
        "--output",
        help="Destination file path. Defaults to a name derived from the object path.",
    )
    hub_get_parser.add_argument(
        "--server-url",
        help="CoveHub API URL. Defaults to covehub_server_url in the local Cove config.",
    )

    hub_inspect_parser = hub_subparsers.add_parser(
        "inspect",
        help="Inspect one public CoveHub object by typed hub path",
    )
    hub_inspect_parser.add_argument(
        "hub_path",
        help="Typed path such as v1/runtime/alice.example.test/demo/certificates/node/latest",
    )
    hub_inspect_parser.add_argument(
        "--server-url",
        help="CoveHub API URL. Defaults to covehub_server_url in the local Cove config.",
    )

    deploy_parser = subparsers.add_parser(
        "deploy",
        help="Pull a published workflow bundle and deploy its nodes to Phala",
    )
    deploy_parser.add_argument(
        "published_ref",
        help="Published workflow ref in the form <publisher>/<workflow_id>",
    )
    deploy_parser.add_argument(
        "--workflow-node",
        help="Launch only the specified workflow node id instead of the full DAG",
    )
    deploy_parser.add_argument(
        "--staged-launch",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Wait for upstream runtime certificates before launching dependent nodes",
    )
    deploy_parser.add_argument(
        "--dependency-timeout-seconds",
        type=float,
        help="Maximum time to wait for upstream runtime certificates during staged launch",
    )
    deploy_parser.add_argument(
        "--dependency-poll-interval-seconds",
        type=float,
        help="Polling interval for staged-launch dependency checks",
    )
    deploy_parser.add_argument(
        "--phala-instance-type",
        required=True,
        help="Phala instance type to use for each deployed node, e.g. tdx.small or h200.small",
    )
    deploy_parser.add_argument(
        "--phala-region",
        help="Optional Phala region preference",
    )
    deploy_parser.add_argument(
        "--phala-os-image",
        help="Optional Phala OS image/version preference",
    )
    deploy_parser.add_argument(
        "--phala-node-id",
        type=int,
        help="Optional Phala node/TEEPod ID",
    )
    deploy_parser.add_argument(
        "--phala-disk-size-gb",
        type=int,
        help="Optional Phala disk size in GB",
    )
    deploy_parser.add_argument(
        "--phala-public-logs",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable public Phala CVM logs",
    )
    deploy_parser.add_argument(
        "--phala-public-sysinfo",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable public Phala CVM sysinfo",
    )
    deploy_parser.add_argument(
        "--phala-listed",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable or disable Phala public listing for deployed CVMs",
    )
    deploy_parser.add_argument(
        "--phala-docker-username",
        help="Optional Docker registry username for Phala image pulls",
    )
    deploy_parser.add_argument(
        "--phala-docker-access-token",
        help="Optional Docker registry access token for Phala image pulls",
    )
    deploy_parser.add_argument(
        "--phala-docker-registry",
        help="Optional Docker registry host for Phala image pulls; blank/default means Docker Hub",
    )

    reset_parser = subparsers.add_parser(
        "reset-runtime",
        help="Delete published runtime artifacts and runtime certificates for a workflow",
    )
    reset_parser.add_argument(
        "published_ref",
        help="Published workflow ref in the form <publisher>/<workflow_id>",
    )

    provision_parser = subparsers.add_parser(
        "provision",
        help="Upload a named artifact slot, inspect or allow a pulled node, or run the local provisioner",
    )
    provision_parser.add_argument(
        "first_arg",
        nargs="?",
        help="Artifact name, or one of 'serve', 'allow', or 'inspect'",
    )
    provision_parser.add_argument(
        "second_arg",
        nargs="?",
        help="File path for uploads, port when using 'serve', artifact id when using 'allow', or published ref when using 'inspect'",
    )
    provision_parser.add_argument(
        "third_arg",
        nargs="?",
        help="Compose file path when using 'allow'",
    )
    provision_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite an existing named artifact slot during upload",
    )

    return parser
