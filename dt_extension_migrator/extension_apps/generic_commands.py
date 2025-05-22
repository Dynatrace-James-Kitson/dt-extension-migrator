import typer
from typing_extensions import Annotated
import pandas as pd
from dynatrace import Dynatrace
from dynatrace.environment_v2.extensions import MonitoringConfigurationDto
from dynatrace.environment_v2.settings import SettingsObjectCreate
from dynatrace.http_client import TOO_MANY_REQUESTS_WAIT
from rich.progress import track
from rich import print
import time
from datetime import datetime
from datetime import timezone
import pathlib

import json
from typing import Optional, List
import math
from enum import Enum
import re

from dt_extension_migrator.remote_unix_utils import (
    build_dt_custom_device_id,
    build_dt_group_id,
    dt_murmur3,
)

from dt_extension_migrator.logging import logger

app = typer.Typer()

EF1_EXTENSION_ID = "custom.remote.python.generic_commands"
EF2_EXTENSION_ID = "custom:generic-commands"

EF1_METRIC_PREFIX = "ext:tech.linux."

TIMEFRAME = "now-6M"

TIMEOUT = 120


class CompareOperator(Enum):
    EQUALS = "=="
    NOT_EQUALS = "!="
    GREATER_THAN = ">"
    GREATER_THAN_OR_EQUAL_TO = ">="
    LESS_THAN = "<"
    LESS_THAN_OR_EQUAL_TO = "<="

ef1_to_ef2_key_mappings = {
    f"{EF1_METRIC_PREFIX}availability": "generic_command.availability",
    f"{EF1_METRIC_PREFIX}performance": "generic_command.performance",
    f"{EF1_METRIC_PREFIX}output": "generic_command.output",
    f"{EF1_METRIC_PREFIX}extracted_metric": "generic_command.extracted_metric"
}

ef1_to_ef2_dimension_mappings = {
    "dt.entity.custom_device": "dt.entity.remote_unix:host",
    "dt.entity.custom_device_group": "dt.entity.remote_unix:host_group",
    "custom_device": "remote_unix:host",
    "custom_device_group": "remote_unix:host_group"
}

def batch(iterable, n=1):
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx:min(ndx + n, l)]

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
    merge_commands: bool = False,
):
    base_config = {
        "enabled": False,
        "description": description,
        "version": version,
        # "featureSets": ["default"],
        "pythonRemote": {"endpoints": []},
    }

    if merge_commands:
        hostname_merged_commands = {}

    print(
        f"{len(ef1_configurations)} endpoints will attempt to be added to the monitoring configuration."
    )
    for index, row in ef1_configurations.iterrows():
        enabled = row["enabled"]
        properties: dict = json.loads(row["properties"])
        endpoint_configuration = {
            "enabled": enabled,
            "hostname": properties.get("hostname"),
            "port": int(properties.get("port")),
            "host_alias": properties.get("alias"),
            "additional_properties": [],
            "commands": [],
            "advanced": {
                "persist_ssh_connection": (
                    "REUSE"
                    if properties.get("persist_ssh_connection") == "true"
                    else "RECREATE"
                ),
                "disable_rsa2": (
                    "DISABLE" if properties.get("disable_rsa2") == "true" else "ENABLE"
                ),
                "max_channel_threads": int(properties.get("max_channel_threads", 5)),
                "log_output": False,
            },
        }

        if properties.get("additional_props"):
            for prop in properties.get("additional_props", "").split("\n"):
                key, value = prop.split("=")
                endpoint_configuration["additional_properties"].append(
                    {"key": key, "value": value}
                )

        command = {
            "command": properties.get("command"),
            "frequency": (
                int(properties.get("frequency")) if properties.get("frequency") else 15
            ),
            "location": (
                properties.get("location")
                if properties.get("location")
                else "ActiveGate"
            ),
            "test_alias": properties.get("test_alias"),
        }

        if properties.get("report_method") in ["FRAMEWORK", "API"]:
            command["report_method"] = "METRIC"
        else:
            command["report_method"] = "SYNTHETIC"

        run_as_different_user = True if properties.get("second_username") else False
        command["run_as_different_user"] = run_as_different_user
        if run_as_different_user:
            command["second_user"] = properties.get("second_username")
            command["second_password"] = properties.get("second_password")

        if properties.get("output_evaluation_behavior") == "TEXT_PATTERN_MATCH":
            command["output_evaluation_behavior"] = "TEXT_PATTERN_MATCH"
            command["output_validation_pattern"] = properties.get(
                "output_validation_pattern"
            )
        elif properties.get("output_evaluation_behavior") == "NUMERIC_VALUE_COMPARISON":
            command["output_evaluation_behavior"] = "NUMERIC_VALUE_COMPARISON"
            command["output_validation_numeric_operator"] = CompareOperator(
                properties.get("output_validation_numeric_operator")
            ).name
            command["output_validation_numeric_value"] = properties.get(
                "output_validation_numeric_value"
            )
        elif properties.get("output_evaluation_behavior") == "SINGLE_VALUE_EXTRACTION":
            command["output_evaluation_behavior"] = "SINGLE_VALUE_EXTRACTION"
        elif properties.get("output_evaluation_behavior") == "MULTI_VALUE_EXTRACTION":
            command["output_evaluation_behavior"] = "MULTI_VALUE_EXTRACTION"
            command["metric_pair_delimiter"] = properties.get("metric_pair_delimiter")
            command["key_value_delimiter"] = properties.get("key_value_delimiter")

        endpoint_configuration["commands"] = [command]

        if merge_commands:
            if not properties.get("hostname") in hostname_merged_commands:
                hostname_merged_commands[properties["hostname"]] = (
                    endpoint_configuration  # first hostname config we see is the base
                )
            else:
                hostname_merged_commands[properties.get("hostname")]["commands"].append(
                    command
                )
        else:
            base_config["pythonRemote"]["endpoints"].append(endpoint_configuration)

    if merge_commands:
        for host in hostname_merged_commands:
            base_config["pythonRemote"]["endpoints"].append(
                hostname_merged_commands[host]
            )
    return base_config

def convert_event(event: dict, prefix: str = None) -> SettingsObjectCreate:

    config = json.loads(event["full_config"])
    if config['value'].get('legacyId'):
        del config['value']['legacyId']
    config['value']['eventEntityDimensionKey'] = 'dt.entity.remote_unix:host'

    # query definition
    query_definition = config["value"]["queryDefinition"]
    config["value"]["summary"] = f"{prefix if prefix else ''}{event['summary']}"
    if query_definition["type"] == "METRIC_KEY":
        dimension_dict = {
            dim_filter["dimensionKey"]: dim_filter["dimensionValue"]
            for dim_filter in query_definition["dimensionFilter"]
        }
        if query_definition["metricKey"] in ef1_to_ef2_key_mappings:

            query_definition["metricKey"] = ef1_to_ef2_key_mappings[
                query_definition["metricKey"]
            ]

            for dim_key in dimension_dict:
                if dim_key == "dt.entity.custom_device":
                    query_definition["dimensionFilter"][
                        list(dimension_dict.keys()).index(dim_key)
                    ]["dimensionKey"] = "dt.entity.remote_unix:host"

            if (
                query_definition["entityFilter"]["dimensionKey"]
                == "dt.entity.custom_device"
            ):
                query_definition["entityFilter"][
                    "dimensionKey"
                ] = "dt.entity.remote_unix:host"


            config["queryDefinition"] = query_definition

    elif query_definition["type"] == "METRIC_SELECTOR":
        query_definition['metricSelector'] = query_definition['metricSelector'].replace("ext:tech.linux.", "generic_commands.")
        query_definition['metricSelector'] = query_definition['metricSelector'].replace("dt.entity.custom_device", "dt.entity.remote_unix:host")
        query_definition['metricSelector'] = query_definition['metricSelector'].replace("CUSTOM_DEVICE_GROUP", "remote_unix:host_group")
        query_definition['metricSelector'] = query_definition['metricSelector'].replace("custom_device_group", "remote_unix:host_group")
        query_definition['metricSelector'] = query_definition['metricSelector'].replace("custom_device", "remote_unix:host")
        query_definition['metricSelector'] = query_definition['metricSelector'].replace("CUSTOM_DEVICE", "remote_unix:host")

    # event template
    event_template = config['value']['eventTemplate']
    # if "dt.entity.custom_device" in event_template['description'] or "dt.entity.custom_device" in event_template['title']:
    event_template['description'] = event_template['description'].replace("dt.entity.custom_device", "dt.entity.remote_unix:host")
    event_template['title'] = event_template['title'].replace("dt.entity.custom_device", "dt.entity.remote_unix:host")


    return SettingsObjectCreate(
        schema_id="builtin:anomaly-detection.metric-events",
        value=config["value"],
        scope="environment",
    )

@app.command(help="Pull EF1 generic commands configurations into a spreadsheet.")
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
    dt = Dynatrace(dt_url, dt_token, too_many_requests_strategy=TOO_MANY_REQUESTS_WAIT, retries=3, log=logger, timeout=TIMEOUT)
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

        full_config.update({"ef1_page": math.ceil((count + 1) / 15), "ef1_group_id": f"CUSTOM_DEVICE_GROUP-{group_id}"})

        print(f"Adding {alias}...")

        for key in properties:
            if key in index or key in ["username"]:
                full_config.update({key: properties[key]})
        full_config["properties"] = json.dumps(properties)
        full_configs.append(full_config)

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
    merge_commands: Annotated[
        bool,
        typer.Option(
            help="Attempt to combine multiple commands against the same host into one endpoint (based on 'hostname' field)"
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
    Convert and push the EF1 generic commands configurations to the EF2 unsigned generic commands extension.
    """
    xls = pd.ExcelFile(input_file)
    df = pd.read_excel(xls, sheet)

    config = build_ef2_config_from_ef1(version, sheet, False, df, merge_commands)
    if print_json:
        print(json.dumps(config))

    if not ag_group.startswith("ag_group-"):
        print(
            f"Appending 'ag_group-' to provided group name. Result: 'ag_group-{ag_group}'"
        )
        ag_group = f"ag_group-{ag_group}"

    dt = Dynatrace(dt_url, dt_token, too_many_requests_strategy=TOO_MANY_REQUESTS_WAIT, retries=3, log=logger, timeout=TIMEOUT)
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

@app.command(help="Migrate events to use EF2 metrics and dimensions.")
def migrate_events(
    dt_url: Annotated[str, typer.Option(envvar="DT_URL")],
    dt_token: Annotated[str, typer.Option(envvar="DT_TOKEN")],
    input_file: Annotated[
        str,
        typer.Option(
            help="The location of a previously pulled/exported list of EF1 generic commands metric events"
        ),
    ],
    prefix: Optional[str] = "[2.0]",
    create: Optional[bool] = False,
    directory: Optional[str] = "."
):
    xls = pd.ExcelFile(input_file)
    df = pd.read_excel(xls, "metric_events")

    dt = Dynatrace(
        dt_url,
        dt_token,
        too_many_requests_strategy=TOO_MANY_REQUESTS_WAIT,
        retries=5,
        log=logger,
        timeout=TIMEOUT,
    )

    events_to_create: List[SettingsObjectCreate] = []
    for index, event in df.iterrows():

        updated_settings_object = convert_event(event, prefix=prefix)
        if updated_settings_object:
            events_to_create.append(updated_settings_object)
    
    output_file = f'generic-commands-event-migration-result-{datetime.now(tz=timezone.utc).strftime(r"%Y-%m-%d_%H-%M")}.txt'
    output_file = pathlib.Path(directory) / output_file
    print(f"Details will be written to {output_file}.")
    with open(output_file, 'a') as f:
        for index, b in enumerate(batch(events_to_create, 50)):
            try:
                print(f"Creating or validating batch {index+1} of events.")
                print(f"###Batch {index}###", file=f)
                r = dt.settings.create_object(validate_only=(not create), body=b)
                print(r, file=f)
                time.sleep(5)
            except Exception as e:
                print(f"Error converting {input_file}: {e}. Check output file {output_file}")
        print("Done.")

@app.command(help="Delete events starting with specified prefix.")
def delete_events(
    dt_url: Annotated[str, typer.Option(envvar="DT_URL")],
    dt_token: Annotated[str, typer.Option(envvar="DT_TOKEN")],
    prefix: Annotated[str, typer.Option(help="Prefix.")],
):
    dt = Dynatrace(
        dt_url,
        dt_token,
        too_many_requests_strategy=TOO_MANY_REQUESTS_WAIT,
        retries=3,
        log=logger,
        timeout=TIMEOUT,
    )

    settings_objects = list(
        dt.settings.list_objects(
            "builtin:anomaly-detection.metric-events",
            filter=f"value.summary starts-with '{prefix}'",
        )
    )

    if settings_objects:
        print("The following metric events would be deleted:")
        for so in settings_objects:
            print(so.value['summary'])

        proceed = typer.confirm(f"{len(settings_objects)} metric events would be deleted. Proceed?")

        if proceed:
            for so in track(settings_objects):
                dt.settings.delete_object(so.object_id)
            print("Done")

@app.command(help="Pull metric events using EF1 generic command metrics.")
def pull_events(
    dt_url: Annotated[str, typer.Option(envvar="DT_URL")],
    dt_token: Annotated[str, typer.Option(envvar="DT_TOKEN")],
    output_file: Optional[str] = None or f"{EF1_EXTENSION_ID}-metric-event-export.xlsx",
):
    dt = Dynatrace(
        dt_url,
        dt_token,
        too_many_requests_strategy=TOO_MANY_REQUESTS_WAIT,
        retries=3,
        log=logger,
        timeout=TIMEOUT,
    )
    settings_objects = list(
        dt.settings.list_objects(
            "builtin:anomaly-detection.metric-events",
            filter=f"value.queryDefinition.metricKey contains '{EF1_METRIC_PREFIX}' or value.queryDefinition.metricSelector contains '{EF1_METRIC_PREFIX}'",
        )
    )

    object_list = []
    for settings_object in track(settings_objects):
        value = settings_object.value

        entity_filter_conditions = 0
        tag_conditions = 0
        management_zone_conditions = 0
        name_filter_conditions = 0
        other_conditions = 0

        selector = f"type(CUSTOM_DEVICE)"
        matching_entity_count = 0

        try:
            if value['queryDefinition'].get("entityFilter"):
                for condition in value['queryDefinition']['entityFilter']['conditions']:
                    entity_filter_conditions += 1
                    if condition['type'] == "TAG":
                        tag_conditions += 1
                        selector += f",tag({condition['value']})"
                    elif condition['type'] == "MANAGEMENT_ZONE":
                        management_zone_conditions += 1
                        selector += f",mzId({condition['value']})"
                    elif condition['type'] == "NAME":
                        name_filter_conditions += 1
                        selector += f",entityName.startsWith({condition['value']})"
                    else:
                        other_conditions += 1
                
                matching_entities = dt.entities.list(entity_selector=selector, fields="+fromRelationships.isInstanceOf", time_from=TIMEFRAME)

                observered_group_ids = []
                group_names = []
                try:
                    if len(list(matching_entities)) < 50:
                        for entity in matching_entities:
                            group_id = entity.from_relationships['isInstanceOf'][0].id
                            if group_id not in observered_group_ids:
                                observered_group_ids.append(group_id)
                                group_names.append(dt.entities.get(group_id).display_name)
                except Exception as e:
                    print(f"Warning - error getting group(s) for {settings_object.summary}: {e}")
    
                entity_names = ", ".join([e.display_name for e in matching_entities])
                matching_entity_count = len(list(matching_entities))
        except Exception as e:
            print(f"Warning: {e}")
        
        object_list.append(
            {
                **settings_object.value,
                **value,
                "full_config": json.dumps(settings_object.json()),
                "number_of_entity_filters": entity_filter_conditions,
                "number_of_mz_filters": management_zone_conditions,
                "number_of_other_conditions": other_conditions,
                "matching_entity_count": matching_entity_count,
                "matching_entities": entity_names,
                "groups": ",".join(group_names)
            }
        )

    writer = pd.ExcelWriter(
        output_file,
        engine="xlsxwriter",
    )
    df = pd.DataFrame(object_list)
    df.to_excel(writer, "metric_events", index=False, header=True)
    writer.close()


if __name__ == "__main__":
    app()
