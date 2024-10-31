import typer
from typing_extensions import Annotated
import pandas as pd
from dynatrace import Dynatrace
from dynatrace.environment_v2.extensions import MonitoringConfigurationDto
from dynatrace.configuration_v1.dashboard import Dashboard, DashboardStub
from dynatrace.environment_v2.settings import SettingsObjectCreate
from dynatrace.http_client import TOO_MANY_REQUESTS_WAIT
from rich.progress import track
import time
import pathlib
from rich import print

import json
import math
from typing import Optional, List
import re
from datetime import datetime, timezone

from dt_extension_migrator.logging import logger

from dt_extension_migrator.extension_apps.remote_logs import EF1_METRIC_PREFIX as logs_metric_prefix
from dt_extension_migrator.extension_apps.generic_commands import EF1_METRIC_PREFIX as commands_metric_prefix

from dt_extension_migrator.remote_unix_utils import (
    build_dt_custom_device_id,
    build_dt_group_id,
    dt_murmur3,
)

app = typer.Typer()

EF1_EXTENSION_ID = "custom.remote.python.remote_agent"
EF2_EXTENSION_ID = "com.dynatrace.extension.remote-unix"
# EF2_EXTENSION_ID = "custom:remote-unix"

EF1_METRIC_PREFIX = "ext:tech.RemoteAgent."

NETWORK_METRICS = ["ext:tech.RemoteAgent.network_bytes", "ext:tech.RemoteAgent.packets", 
                   "ext:tech.RemoteAgent.network_errors", "ext:tech.RemoteAgent.packets_dropped"]

TIMEFRAME = "now-6M"

TIMEOUT = 120

ef1_to_ef2_key_mappings = {
    "ext:tech.RemoteAgent.availability": "remote_unix.availability",
    "ext:tech.RemoteAgent.cpu_utilization": "remote_unix.cpu_utilization",
    "ext:tech.RemoteAgent.cpu_user": "remote_unix.cpu_user",
    "ext:tech.RemoteAgent.cpu_system": "remote_unix.cpu_system",
    "ext:tech.RemoteAgent.cpu_idle": "remote_unix.cpu_idle",
    "ext:tech.RemoteAgent.waiting_processes": "remote_unix.waiting_processes",
    "ext:tech.RemoteAgent.paged_in": "remote_unix.paged_in",
    "ext:tech.RemoteAgent.paged_out": "remote_unix.paged_out",
    "ext:tech.RemoteAgent.physical_memory_free": "remote_unix.physical_memory_free",
    "ext:tech.RemoteAgent.physical_memory_used_percent": "remote_unix.physical_memory_used_percent",
    "ext:tech.RemoteAgent.memory_free": "remote_unix.physical_memory_free",
    "ext:tech.RemoteAgent.memory_used_percent": "remote_unix.physical_memory_used_percent",
    "ext:tech.RemoteAgent.swap_free": "remote_unix.swap_free",
    "ext:tech.RemoteAgent.swap_total": "remote_unix.swap_total",
    "ext:tech.RemoteAgent.swap_used_percent": "remote_unix.swap_used_percent",
    "ext:tech.RemoteAgent.swap_free_percent": "remote_unix.swap_free_percent",
    "ext:tech.RemoteAgent.top_process_cpu": "remote_unix.top_process_cpu",
    "ext:tech.RemoteAgent.top_process_size": "remote_unix.top_process_size",
    "ext:tech.RemoteAgent.filtered_process_cpu": "remote_unix.filtered_process_cpu",
    "ext:tech.RemoteAgent.filtered_process_size": "remote_unix.filtered_process_size",
    "ext:tech.RemoteAgent.filtered_process_group_cpu": "remote_unix.filtered_process_cpu",
    "ext:tech.RemoteAgent.filtered_process_group_size": "remote_unix.filtered_process_size",
    "ext:tech.RemoteAgent.filtered_process_match_count": "remote_unix.filtered_process_matches",
    "ext:tech.RemoteAgent.filtered_process_thread_match_count": None,
    "ext:tech.RemoteAgent.filtered_process_pids_changed": "remote_unix.filtered_process_pids_changed",
    "ext:tech.RemoteAgent.user_count": "remote_unix.users",
    "ext:tech.RemoteAgent.mount_used": "remote_unix.mount_used",
    "ext:tech.RemoteAgent.mount_capacity": "remote_unix.mount_capacity",
    "ext:tech.RemoteAgent.mount_available": "remote_unix.mount_available",
    "ext:tech.RemoteAgent.network_bytes": {
        "Outgoing": "remote_unix.network_bytes_sent_count",
        "Incoming": "remote_unix.network_bytes_received_count",
    },
    "ext:tech.RemoteAgent.packets": {
        "Outgoing": "remote_unix.packets_sent_count",
        "Incoming": "remote_unix.packets_received_count",
    },
    "ext:tech.RemoteAgent.network_errors": {
        "Outgoing": "remote_unix.network_errors_outgoing_count",
        "Incoming": "remote_unix.network_errors_incoming_count",
    },
    "ext:tech.RemoteAgent.packets_dropped": {
        "Outgoing": "remote_unix.packets_dropped_outgoing_count",
        "Incoming": "remote_unix.packets_dropped_incoming_count",
    },
    "ext:tech.RemoteAgent.uptime": "remote_unix.uptime",
    "ext:tech.RemoteAgent.individual_cpu_time_user": "remote_unix.individual_cpu_time_user",
    "ext:tech.RemoteAgent.individual_cpu_time_system": "remote_unix.individual_cpu_time_system",
    "ext:tech.RemoteAgent.individual_cpu_time_idle": "remote_unix.individual_cpu_time_idle",
    "ext:tech.RemoteAgent.individual_cpu_time_iowait": "remote_unix.individual_cpu_time_iowait",
    "ext:tech.RemoteAgent.disk_read": "remote_unix.disk_read_count",
    "ext:tech.RemoteAgent.disk_write": "remote_unix.disk_write_count",
    "ext:tech.RemoteAgent.bytes_per_transfer": "remote_unix.bytes_per_transfer",
    "ext:tech.RemoteAgent.transfers": "remote_unix.transfers",
    "ext:tech.RemoteAgent.disk_read_ops": "remote_unix.disk_read_ops",
    "ext:tech.RemoteAgent.disk_write_ops": "remote_unix.disk_write_ops",
    "ext:tech.RemoteAgent.load_avg_1_min": "remote_unix.load_avg_1_min",
    "ext:tech.RemoteAgent.load_avg_5_min": "remote_unix.load_avg_5_min",
    "ext:tech.RemoteAgent.load_avg_15_min": "remote_unix.load_avg_15_min",
}

ef1_to_ef2_dimension_mappings = {
    "Process": "group",
    "PID": "pid",
    "Name": "name",
    "Group": "group",
    "Mount": "mount",
    "Interface": "interface",
    "Direction": None,
    "Id": "id",
    "Disk": "disk",
    "dt.entity.custom_device": "dt.entity.remote_unix:host",
    "dt.entity.custom_device_group": "dt.entity.remote_unix:host_group"
}

def batch(iterable, n=1):
    l = len(iterable)
    for ndx in range(0, l, n):
        yield iterable[ndx:min(ndx + n, l)]

def replace_dimension(match: re.Match):
    if ef1_to_ef2_dimension_mappings.get(match.group(1)):
        return r"{dims:" + ef1_to_ef2_dimension_mappings.get(match.group(1)) + r"}"
    else:
        return ""

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

def convert_to_selector_based(query_definition: dict):
    old_query_definition = query_definition
    new_query_definition = {
        "type": "METRIC_SELECTOR",
        "metricSelector": f"",
        "managementZone": old_query_definition.get("managementZone"),
        "queryOffset": None
    }


    filter_text = ":filter(and("
    split_by = f":splitBy(\"dt.entity.remote_unix:host\""

    for filter in old_query_definition['dimensionFilter']:
        split_by += f",\"{filter['dimensionKey']}\""
        filter_text += f"eq({filter['dimensionKey']},{filter['dimensionValue']})"
    split_by += ")"

    entity_filter = old_query_definition['entityFilter']
    filter_text += f",in(\"dt.entity.remote_unix:host\",entitySelector(\"type(remote_unix:host)"
    for condition in entity_filter['conditions']:
        if condition['type'] == "TAG":
            filter_text += f",tag(~\"{condition['value']}~\")"
        else:
            raise Exception(f"Non-tag entity filter found: {condition['type']}")

    filter_text+="\"))))"
    new_query_definition["metricSelector"] = f"{old_query_definition['metricKey']}{filter_text}{split_by}"
    # if len(old_query_definition['entityFilter']['conditions']) == 1:
    #     print(old_query_definition)
    #     print(new_query_definition)
    return new_query_definition

def convert_event(event: dict, prefix: str = None) -> SettingsObjectCreate:

    config = json.loads(event["full_config"])
    if config['value'].get('legacyId'):
        del config['value']['legacyId']
    config['value']['eventEntityDimensionKey'] = 'dt.entity.remote_unix:host'

    # query definition
    query_definition = config["value"]["queryDefinition"]

    if query_definition["type"] == "METRIC_KEY":
        config["value"]["summary"] = f"{prefix if prefix else ''}{event['summary']}"
        dimension_dict = {
            dim_filter["dimensionKey"]: dim_filter["dimensionValue"]
            for dim_filter in query_definition["dimensionFilter"]
        }
        if query_definition["metricKey"] in ef1_to_ef2_key_mappings:  # network events
            if query_definition["metricKey"] in [
                "ext:tech.RemoteAgent.network_bytes",
                "ext:tech.RemoteAgent.packets",
                "ext:tech.RemoteAgent.network_errors",
                "ext:tech.RemoteAgent.packets_dropped",
            ]:
                if dimension_dict.get("Direction"):
                    query_definition["metricKey"] = ef1_to_ef2_key_mappings[
                        query_definition["metricKey"]
                    ][dimension_dict["Direction"]]
                    del query_definition["dimensionFilter"][
                        list(dimension_dict.keys()).index("Direction")
                    ]
                    del dimension_dict["Direction"]

            else:
                query_definition["metricKey"] = ef1_to_ef2_key_mappings[
                    query_definition["metricKey"]
                ]

            for dim_key in dimension_dict:
                if dim_key == "dt.entity.custom_device":
                    query_definition["dimensionFilter"][
                        list(dimension_dict.keys()).index(dim_key)
                    ]["dimensionKey"] = "dt.entity.remote_unix:host"
                if dim_key in ef1_to_ef2_dimension_mappings:
                    query_definition["dimensionFilter"][
                        list(dimension_dict.keys()).index(dim_key)
                    ]["dimensionKey"] = ef1_to_ef2_dimension_mappings[dim_key]

            if (
                query_definition["entityFilter"]["dimensionKey"]
                == "dt.entity.custom_device"
            ):
                query_definition["entityFilter"][
                    "dimensionKey"
                ] = "dt.entity.remote_unix:host"

            if query_definition['metricKey'].endswith("count"):
                query_definition['aggregation'] = "VALUE"
                query_definition = convert_to_selector_based(query_definition)

            config['value']["queryDefinition"] = query_definition
    

            # event template
            event_template = config['value']['eventTemplate']
            dim_pattern = r"\{dims\:([^\}++]+)\}"
            event_template['description'] = re.sub(dim_pattern, replace_dimension, event_template['description'])
            
            title = config['value']['eventTemplate']
            dim_pattern = r"\{dims\:([^\}++]+)\}"
            event_template['title'] = re.sub(dim_pattern, replace_dimension, event_template['title'])

            return SettingsObjectCreate(
                schema_id="builtin:anomaly-detection.metric-events",
                value=config["value"],
                scope="environment",
            )

        else:
            print(f"Unknown key: {query_definition['metricKey']}")

        settings_object = SettingsObjectCreate(schema_id="builtin:anomaly-detection.metric-events", value=config['value'], scope="environment")
        return settings_object

    else:
        print(f"Found selector: {query_definition}")

def build_ef2_config_from_ef1(
    version: str,
    description: str,
    skip_endpoint_authentication: bool,
    ef1_configurations: pd.DataFrame,
):

    # {
    #     "os": "Generic Linux",
    #     "disable_iostat": "false",
    #     "ssh_key_contents": None,
    #     "top_threads_mode": "false",
    #     "log_level": "INFO",
    #     "persist_ssh_connection": "true",
    #     "mounts_to_exclude": "",
    #     "additional_props": "key=value\ntest=tess1",
    #     "ssh_key_file": "",
    #     "ssh_key_passphrase": None,
    #     "hostname": "172.26.231.39",
    #     "password": None,
    #     "disable_rsa2": "false",
    #     "fail_on_initial_error": "false",
    #     "mounts_to_include": ".*\nabc\ndef",
    #     "port": "22",
    #     "process_filter": "ssh;SSH",
    #     "custom_path": "",
    #     "alias": "ubuntu",
    #     "max_channel_threads": "5",
    #     "username": "jpwk",
    #     "group": "testing",
    # }

    base_config = {
        "enabled": False,
        "description": description,
        "version": version,
        "featureSets": ["default"],
        "pythonRemote": {"endpoints": []},
    }

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
                "alias": properties.get("alias"),
                "os": properties.get("os"),
                "additional_properties": [],
                "top_processes": {"top_count": 10, "report_log_events": False},
                "process_filters": [],
                "mount_filters": [],
                "advanced": {
                    "persist_ssh_connection": (
                        "REUSE"
                        if properties.get("persist_ssh_connection") == "true"
                        else "RECREATE"
                    ),
                    "disable_rsa2": (
                        "DISABLE"
                        if properties.get("disable_rsa2") == "true"
                        else "ENABLE"
                    ),
                    "top_mode": (
                        "THREADS_MODE"
                        if properties.get("top_threads_mode") == "true"
                        else "DEFAULT"
                    ),
                    "max_channel_threads": int(
                        properties.get("max_channel_threads", 5)
                    ),
                    "log_output": False,
                },
            }

            if not skip_endpoint_authentication:
                endpoint_configuration["authentication"] = (
                    build_authentication_from_ef1(properties)
                )

            if properties.get("custom_path", None):
                endpoint_configuration["advanced"]["custom_path"] = properties[
                    "custom_path"
                ]

            if properties.get("additional_props"):
                for prop in properties.get("additional_props", "").split("\n"):
                    key, value = prop.split("=")
                    endpoint_configuration["additional_properties"].append(
                        {"key": key, "value": value}
                    )

            if properties.get("process_filter"):
                for process in properties.get("process_filter").split("\n"):
                    pattern, group_key = process.split(";")
                    endpoint_configuration["process_filters"].append(
                        {"group_key": group_key, "pattern": pattern, "user": None}
                    )

            if properties.get("mounts_to_include"):
                for pattern in properties.get("mounts_to_include").split("\n"):
                    endpoint_configuration["mount_filters"].append(
                        {"filter_type": "include", "pattern": pattern}
                    )

            if properties.get("mounts_to_exclude"):
                for pattern in properties.get("mounts_to_exclude").split("\n"):
                    endpoint_configuration["mount_filters"].append(
                        {"filter_type": "exclude", "pattern": pattern}
                    )

            base_config["pythonRemote"]["endpoints"].append(endpoint_configuration)

        except Exception as e:
            print(f"Error parsing config: {e}")
            print(properties)

    return base_config

@app.command(
    help="Migrate dashboards using either IDs or an input file from 'pull-dashboards'"
)
def migrate_dashboard(
    dt_url: Annotated[str, typer.Option(envvar="DT_URL")],
    dt_token: Annotated[str, typer.Option(envvar="DT_TOKEN")],
    id: Annotated[
        Optional[List[str]],
        typer.Option(
            help="Specify a dashboard ID to filter. Can be passed multiple times."
        ),
    ] = [],
    input_file: Annotated[
        str,
        typer.Option(
            help="The location of a previously pulled/exported list of EF1 endpoints"
        ),
    ] = None or f"{EF1_EXTENSION_ID}-dashboard-export.xlsx"
):
    dt = Dynatrace(
        dt_url,
        dt_token,
        too_many_requests_strategy=TOO_MANY_REQUESTS_WAIT,
        retries=3,
        log=logger,
        timeout=TIMEOUT,
    )
    for dash_id in id:
        try:
            dashboard = dt.dashboards.get(dash_id)
            body = dashboard.raw_json
            print(f"### {body['dashboardMetadata']['name']} ###")
            del body['id']
            body['dashboardMetadata']['name'] = f"Migrated - {body['dashboardMetadata']['name']}"
            for tile in body['tiles']:
                tile_type = tile['tileType']
                if tile_type == "DATA_EXPLORER":
                    for query in tile['queries']:
                        if query['metric']:
                            if not query['metric'].startswith(EF1_METRIC_PREFIX):
                                continue
                            if query['metric'] in NETWORK_METRICS:
                                print(f"Review network metrics in tile {tile['name']}: {query['metric']} ")
                                continue
                            query['metric'] = ef1_to_ef2_key_mappings.get(query['metric'], "missing")
                            split_by = query['splitBy']
                            for index, dimension in enumerate(split_by):
                                split_by[index] = ef1_to_ef2_dimension_mappings.get(dimension, "missing")
                                # print(query)

                        elif query['metricSelector']:
                            selector: str = query['metricSelector']
                            if not EF1_METRIC_PREFIX in query['metricSelector']:
                                continue
                            for network_metric in NETWORK_METRICS:
                                if network_metric in query['metricSelector']:
                                    print(f"Review network metric in selector in tile {tile['name']}: {query['metricSelector']} ")
                                    continue
                            for old_key in ef1_to_ef2_key_mappings:
                                if old_key in selector:
                                     selector = selector.replace(old_key, ef1_to_ef2_key_mappings[old_key])
                            for old_dimension in ef1_to_ef2_dimension_mappings:
                                if old_dimension in selector:
                                    selector = selector.replace(old_dimension, ef1_to_ef2_dimension_mappings[old_dimension])
                            query['metricSelector'] = selector
                            print(f"Review updated metric selector in tile {tile['name']}: {query['metricSelector']} ")
                            continue

            response = dt.dashboards.post(body)
            response.raise_for_status()
            
            base_url = dt_url if not dt_url.endswith("/") else dt_url[:-1]
            print(f"Migrated dashboard: {base_url}/#dashboard;id={response.json().get('id')}")
        except Exception as e:
            print(f"Error migrating dashboard {dash_id}: {e}")


@app.command(
    help="Pull dashboards using EF1 remote ssh style metrics."
)
def pull_dashboards(
    dt_url: Annotated[str, typer.Option(envvar="DT_URL")],
    dt_token: Annotated[str, typer.Option(envvar="DT_TOKEN")],
    id: Annotated[
        Optional[List[str]],
        typer.Option(
            help="Specify a dashboard ID to filter. Can be passed multiple times."
        ),
    ] = [],
    tag: Annotated[
        Optional[List[str]],
        typer.Option(help="A tag to filter for when finding dashboards to convert."),
    ] = None,
    output_file: Optional[str] = None or f"{EF1_EXTENSION_ID}-dashboard-export.xlsx",
):
    dt = Dynatrace(
        dt_url,
        dt_token,
        too_many_requests_strategy=TOO_MANY_REQUESTS_WAIT,
        retries=3,
        log=logger,
        timeout=TIMEOUT,
    )
    dashboard_full_configs: List[Dashboard] = []
    dashboard_stubs: List[DashboardStub] = []
    if id and tag:
        raise Exception("Only use tag or id for filtering.")
    if id:
        for id in id:
            try:
                dashboard_full_configs.append(dt.dashboards.get(id))
            except Exception as e:
                print(f"Error pulling dashboard with id {id}: {e}")
    elif tag:
        dashboard_stubs = dt.dashboards.list(tags=tag)
    else:
        dashboard_stubs = dt.dashboards.list()

    for stub in track(dashboard_stubs):
        try:
            dashboard_full_configs.append(stub.get_full_dashboard())
        except Exception as e:
            print(f"Error pulling full config for dashboard with id {stub.id}: {e}")

    ssh_dashboards = []

    for dashboard in dashboard_full_configs:
        dashboard_string = json.dumps(dashboard.json())
        if any(x in dashboard_string for x in [EF1_METRIC_PREFIX, logs_metric_prefix, commands_metric_prefix]):
            print(dashboard.dashboard_metadata.json())
            ssh_dashboards.append({
                "id": dashboard.id,
                "name": dashboard.dashboard_metadata.name,
                "owner": dashboard.dashboard_metadata.owner,
                "tags": dashboard.dashboard_metadata.tags,
                "tile_types": ",".join(set([t.tile_type for t in dashboard.tiles])),
                "configuration": json.dumps(dashboard.json())           }
            )
    
    writer = pd.ExcelWriter(
        output_file,
        engine="xlsxwriter",
    )
    df = pd.DataFrame(ssh_dashboards)
    df.to_excel(writer, "dashboards", index=False, header=True)
    writer.close()

@app.command(help="Migrate events to use EF2 metrics and dimensions.")
def migrate_events(
    dt_url: Annotated[str, typer.Option(envvar="DT_URL")],
    dt_token: Annotated[str, typer.Option(envvar="DT_TOKEN")],
    input_file: Annotated[
        str,
        typer.Option(
            help="The location of a previously pulled/exported list of EF1 remote unix metric events"
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
    
    output_file = f'remote-unix-event-migration-result-{datetime.now(tz=timezone.utc).strftime(r"%Y-%m-%d_%H-%M")}.txt'
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

@app.command(help="Pull metric events using EF1 remote unix metrics.")
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

@app.command(help="Pull EF1 remote unix configurations into a spreadsheet.")
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

        full_config.update(
            {
                "ef1_page": math.ceil((count + 1) / 15),
                "ef1_group_id": f"CUSTOM_DEVICE_GROUP-{group_id}",
            }
        )

        for key in properties:
            if key in index or key == "username":
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
    Convert and push the EF1 remote unix configurations to the EF2 extension.
    """
    xls = pd.ExcelFile(input_file)
    df = pd.read_excel(xls, sheet)

    config = build_ef2_config_from_ef1(version, sheet, True, df)
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
