import typer
from typing_extensions import Annotated
import pandas as pd
from dynatrace import Dynatrace
from dynatrace.environment_v2.extensions import MonitoringConfigurationDto
from rich.progress import track
from rich import print

import json
import math
from typing import Optional, List
import re

from dt_extension_migrator.remote_unix_utils import (
    build_dt_custom_device_id,
    build_dt_group_id,
    dt_murmur3,
)

app = typer.Typer()

EF1_EXTENSION_ID = "custom.remote.python.remote_logs"
EF2_EXTENSION_ID = "com.dynatrace.extension.remote-logs"


def build_authentication_from_ef1(ef1_config: dict):
    authentication = {"username": ef1_config.get("username")}

    password = ef1_config.get("password")
    ssh_key_contents = ef1_config.get("ssh_key_contents")
    ssh_key_file = ef1_config.get("ssh_key_file")
    ssh_key_passphrase = ef1_config.get("ssh_key_passphrase", "")

    # doesn't seem like a good way to pre-build the auth since the secrets (password or key contents) will always be null
    if True:
        authentication.update(
            {"password": "password", "scheme": "password", "useCredentialVault": False}
        )
    elif ssh_key_contents:
        authentication.update(
            {
                "ssh_key_contents": ssh_key_contents,
                "passphrase": ssh_key_passphrase,
                "scheme": "ssh_key_contents",
            }
        )
    elif ssh_key_file:
        authentication.update(
            {
                "key_path": ssh_key_file,
                "passphrase": ssh_key_passphrase,
                "scheme": "key_path",
            }
        )
    return authentication


def build_ef2_config_from_ef1(
    version: str,
    description: str,
    skip_endpoint_authentication: bool,
    ef1_configurations: pd.DataFrame,
    merge_logs: bool = False,
):

    base_config = {
        "enabled": False,
        "description": description,
        "version": version,
        # "featureSets": ["default"],
        "pythonRemote": {"endpoints": []},
    }

    if merge_logs:
        hostname_merged_logs = {}

    print(
        f"{len(ef1_configurations)} endpoints will attempt to be added to the monitoring configuration."
    )
    for index, row in ef1_configurations.iterrows():
        try:
            enabled = row["enabled"]
            properties: dict = json.loads(row["properties"])
            endpoint_configuration = {
                "enabled": enabled,
                "hostname": properties.get("hostname"),
                "port": int(properties.get("port")),
                "host_alias": properties.get("alias"),
                "additional_properties": [],
                "logs_to_monitor": [],
                "os": properties.get("os"),
                "advanced": {
                    "persist_ssh_connection": (
                        "REUSE"
                        if properties.get("persist_ssh_connection") == "true"
                        else "RECREATE"
                    ),
                    "disable_rsa2": (
                        "DISABLE" if properties.get("disable_rsa2") == "true" else "ENABLE"
                    ),
                },
            }

            if properties.get("additional_props"):
                for prop in properties.get("additional_props", "").split("\n"):
                    key, value = prop.split("=")
                    endpoint_configuration["additional_properties"].append(
                        {"key": key, "value": value}
                    )

            log = {
                "log_directory_path": properties.get("log_directory_path"),
                "log_pattern": properties.get("log_pattern"),
                "log_alias": (
                    properties.get("log_alias")
                    if properties.get("log_alias")
                    else properties.get("log_pattern")
                ),
                "frequency": 1,
                "patterns_to_match": [],
                "patterns_to_exclude": [],
                "patterns_to_extract": [],
            }

            report_event_on_match = (
                True if properties.get("report_event_on_match") == "true" else False
            )
            if report_event_on_match:
                log.update(
                    {
                        "report_event_on_match": report_event_on_match,
                        "event_severity": properties.get("event_severity"),
                        "context_lines": int(properties.get("context_lines")),
                        "event_prefix": properties.get("event_prefix"),
                    }
                )
            else:
                log.update({"report_event_on_match": report_event_on_match})

            if properties.get("patterns_to_match"):
                for pattern in properties.get("patterns_to_match").strip().split("\n"):
                    if len(pattern.rsplit(";", 1)) == 1:
                        pattern, name = pattern, pattern
                        print(f"Warning: configuration \"{row.get("endpointName")}\" had an incorrect pattern match configuration and will attempt to be corrected in the new configuration. Please review.")
                    else:
                        pattern, name = pattern.rsplit(";", 1)
                    log["patterns_to_match"].append({"pattern": pattern, "name": name})

            if properties.get("patterns_to_extract"):
                for pattern in properties.get("patterns_to_extract").strip().split("\n"):
                    pattern, name = pattern.rsplit(";", 1)
                    if " " in name:
                        name, aggregation = name.split(" ")
                        aggregation = aggregation.upper()
                    else:
                        aggregation = "SUM"
                    log["patterns_to_extract"].append(
                        {"pattern": pattern, "name": name, "aggregation": aggregation}
                    )

            if properties.get("patterns_to_exclude"):
                for pattern in properties.get("patterns_to_exclude").strip().split("\n"):
                    log["patterns_to_exclude"].append(pattern)

            endpoint_configuration["logs_to_monitor"] = [log]

            if merge_logs:
                if not properties.get("hostname") in hostname_merged_logs:
                    hostname_merged_logs[properties["hostname"]] = endpoint_configuration
                else:
                    print(
                        f"Endpoint {row['endpointId']} beng added to merged log host {properties['hostname']}"
                    )
                    hostname_merged_logs[properties.get("hostname")][
                        "logs_to_monitor"
                    ].append(log)
            else:
                base_config["pythonRemote"]["endpoints"].append(endpoint_configuration)
        
        except Exception as e:
            print(f"Error parsing config: {e}")
            print(properties)

    if merge_logs:
        for host in hostname_merged_logs:
            base_config["pythonRemote"]["endpoints"].append(hostname_merged_logs[host])
    return base_config


@app.command(help="Pull EF1 remote logs configurations into a spreadsheet.")
def pull(
    dt_url: Annotated[str, typer.Option(envvar="DT_URL")],
    dt_token: Annotated[str, typer.Option(envvar="DT_TOKEN")],
    output_file: Optional[str] = None or f"{EF1_EXTENSION_ID}-export.xlsx",
    index: Annotated[
        Optional[List[str]],
        typer.Option(
            help="Specify what property to group sheets by. Can be specified multiple times."
        ),
    ] = ["group"],
):
    dt = Dynatrace(dt_url, dt_token)
    configs = list(dt.extensions.list_instances(extension_id=EF1_EXTENSION_ID))
    full_configs = []

    count = 0
    for config in track(configs, description="Pulling EF1 configs"):
        config = config.get_full_configuration(EF1_EXTENSION_ID)
        full_config = config.json()
        properties = full_config.get("properties", {})

        alias = (
            properties.get("alias")
            if properties.get("alias")
            else properties.get("hostname")
        )
        group_id = dt_murmur3(build_dt_group_id(properties.get("group"), ""))

        ef1_custom_device_id = (
            f"CUSTOM_DEVICE-{dt_murmur3(build_dt_custom_device_id(group_id, alias))}"
        )
        full_config.update({"ef1_device_id": ef1_custom_device_id})

        ef2_entity_selector = f'type(remote_unix:host),alias("{alias}")'
        full_config.update({"ef2_entity_selector": ef2_entity_selector})

        full_config.update({"ef1_page": math.ceil((count + 1) / 15)})

        for key in properties:
            if key in index or key in ["username"]:
                full_config.update({key: properties[key]})
        full_config["properties"] = json.dumps(properties)
        full_configs.append(full_config)

        print(f"Adding {alias}...")

        count += 1

    print("Finished pulling configs...")
    print("Adding data to document...")
    writer = pd.ExcelWriter(
        output_file,
        engine="xlsxwriter",
    )
    df = pd.DataFrame(full_configs)
    df_grouped = df.groupby(index)
    for key, group in df_grouped:
        key = [subgroup for subgroup in key if subgroup]
        sheet_name = "-".join(key)
        sheet_name = re.sub(r"[\[\]\:\*\?\/\\\s]", "_", sheet_name)
        if len(sheet_name) >= 31:
            sheet_name = sheet_name[:31]
        group.to_excel(
            writer, sheet_name or "Default", index=False, header=True
        )
    print("Closing document...")
    writer.close()
    print(f"Exported configurations available in '{output_file}'")


@app.command()
def push(
    dt_url: Annotated[str, typer.Option(envvar="DT_URL")],
    dt_token: Annotated[str, typer.Option(envvar="DT_TOKEN")],
    input_file: Annotated[
        str,
        typer.Option(
            help="The location of a previously pulled/exported list of EF1 endpoints"
        ),
    ],
    sheet: Annotated[
        str,
        typer.Option(
            help="The name of a sheet in a previously pulled/exported list of EF1 endpoints"
        ),
    ],
    ag_group: Annotated[str, typer.Option()],
    version: Annotated[
        str,
        typer.Option(
            help="The version of the EF2 extension you would look to create this configuration for"
        ),
    ],
    merge_logs: Annotated[
        bool,
        typer.Option(
            help="Attempt to combine multiple log analyses against the same host into one endpoint (based on 'hostname' field)"
        ),
    ] = False,
    print_json: Annotated[
        bool, typer.Option(help="Print the configuration json that will be sent")
    ] = False,
    do_not_create: Annotated[
        bool,
        typer.Option(
            help="Does every step except for sending the configuration. Combine with '--print-json' to review the config that would be created"
        ),
    ] = False,
):
    """
    Convert and push the EF1 remote logs configurations to the EF2 extension.
    """
    xls = pd.ExcelFile(input_file)
    df = pd.read_excel(xls, sheet)

    config = build_ef2_config_from_ef1(version, sheet, False, df, merge_logs)
    if print_json:
        print(json.dumps(config))

    if not ag_group.startswith("ag_group-"):
        print(
            f"Appending 'ag_group-' to provided group name. Result: 'ag_group-{ag_group}'"
        )
        ag_group = f"ag_group-{ag_group}"

    dt = Dynatrace(dt_url, dt_token, print_bodies=False)
    config = MonitoringConfigurationDto(ag_group, config)

    if not do_not_create:
        try:
            result = dt.extensions_v2.post_monitoring_configurations(
                EF2_EXTENSION_ID, [config]
            )[0]
            print(f"Configs created successfully. Response: {result['code']}")
            base_url = dt_url if not dt_url.endswith("/") else dt_url[:-1]
            print(
                f"Link to monitoring configuration: {base_url}/ui/hub/ext/listing/registered/{EF2_EXTENSION_ID}/{result['objectId']}/edit"
            )
        except Exception as e:
            print(f"[bold red]{e}[/bold red]")


if __name__ == "__main__":
    app()
