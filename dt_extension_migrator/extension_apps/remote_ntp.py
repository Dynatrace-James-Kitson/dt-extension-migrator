import typer
from typing_extensions import Annotated
import pandas as pd
from dynatrace import Dynatrace
from dynatrace.environment_v2.extensions import MonitoringConfigurationDto
from dynatrace.http_client import TOO_MANY_REQUESTS_WAIT
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

from dt_extension_migrator.logging import logger

EF1_EXTENSION_ID = "custom.remote.python.remote_ntp"
EF2_EXTENSION_ID = "com.dynatrace.extension.remote-ntp"
# EF2_EXTENSION_ID = "custom:remote-ntp"

TIMEOUT = 30


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
        "pythonRemote": {"ntp_servers": [], "error_on_outage": False},
    }

    print(
        f"{len(ef1_configurations)} endpoints will attempt to be added to the monitoring configuration."
    )
    for index, row in ef1_configurations.iterrows():
        try:
            base_config["pythonRemote"]["group"] = row['group']
            enabled = row["enabled"]
            properties: dict = json.loads(row["properties"])
            ntp_server = {
                "hostname": properties.get("ntp_servers").strip(),
                "alias": properties.get("alias"),
                "port": properties.get("port", 123)
            }


            base_config["pythonRemote"]["ntp_servers"].append(ntp_server)

        except Exception as e:
            print(f"Error processing endpoint {row['endpointName']}: {e}")

    return base_config


@app.command(help="Pull EF1 remote ntp configurations into a spreadsheet.")
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
    dt = Dynatrace(
        dt_url,
        dt_token,
        too_many_requests_strategy=TOO_MANY_REQUESTS_WAIT,
        retries=3,
        log=logger,
        timeout=TIMEOUT,
    )
    configs = list(dt.extensions.list_instances(extension_id=EF1_EXTENSION_ID))
    full_configs = []

    count = 0
    for config in track(configs, description="Pulling EF1 configs"):
        config = config.get_full_configuration(EF1_EXTENSION_ID)
        full_config = config.json()
        properties = full_config.get("properties", {})

        alias = properties.get("alias")

        group_id = dt_murmur3(build_dt_group_id(properties.get("group"), ""))

        ef1_custom_device_id = (
            f"CUSTOM_DEVICE-{dt_murmur3(build_dt_custom_device_id(group_id, alias))}"
        )
        full_config.update({"ef1_device_id": ef1_custom_device_id})

        ef2_entity_selector = f'type(ntp:server),alias("{alias}")'
        full_config.update({"ef2_entity_selector": ef2_entity_selector})

        full_config.update(
            {"num_of_servers": len(properties.get("ntp_servers").split("\n"))}
        )
        full_config.update({"alias": alias})
        full_config.update({"group": properties.get("group")})

        full_config.update(
            {
                "ef1_page": math.ceil((count + 1) / 15),
                "ef1_group_id": f"CUSTOM_DEVICE_GROUP-{group_id}",
            }
        )

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
        group.to_excel(writer, sheet_name or "Default", index=False, header=True)
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

    config = build_ef2_config_from_ef1(version, sheet, False, df)
    if print_json:
        print(json.dumps(config))

    if not ag_group.startswith("ag_group-"):
        print(
            f"Appending 'ag_group-' to provided group name. Result: 'ag_group-{ag_group}'"
        )
        ag_group = f"ag_group-{ag_group}"

    dt = Dynatrace(
        dt_url,
        dt_token,
        too_many_requests_strategy=TOO_MANY_REQUESTS_WAIT,
        retries=3,
        log=logger,
        timeout=TIMEOUT,
    )
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
