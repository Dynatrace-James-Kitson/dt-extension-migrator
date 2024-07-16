import typer
from dynatrace import Dynatrace
import pandas as pd
from rich import print

from typing import Optional, List

from typing_extensions import Annotated

app = typer.Typer()

DIVIDER = "------------------------------------"

TIMEFRAME = "now-1M"

@app.command(
    help="Read tags from EF1 entities and push them to the corresponding EF2 entities"
)
def migrate_tags(
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
    skip_tag: Annotated[
        Optional[List[str]],
        typer.Option(
            help="Tags to skip when migrating. Can be specified multiple times."
        ),
    ] = [],
):
    dt = Dynatrace(dt_url, dt_token, print_bodies=False)
    xls = pd.ExcelFile(input_file)
    df = pd.read_excel(xls, sheet)

    summary_report = {
        "no_tags_found": [],
        "unable_to_add_tags": [],
        "ef2_hosts_not_found": [],
        "ef1_hosts_not_found": [],
        "multiple_ef2_hosts_found": [],
    }

    successfully_migrated = 0
    failed_migration = 0
    migrated_with_warnings = 0

    tags_by_ef1_id = {}

    group_id = df.iloc[0]["ef1_group_id"]
    selector = (
        f"type(custom_device),fromRelationships.isInstanceOf(entityId({group_id}))"
    )
    for ef1_device in dt.entities.list(
        entity_selector=selector, fields="+tags,+fromRelationships", time_from=TIMEFRAME
    ):
        if len(ef1_device.tags) == 0:
            summary_report["no_tags_found"].append(
                f"{ef1_device.display_name} ({ef1_device.entity_id})"
            )
        tags_by_ef1_id[ef1_device.entity_id] = [
            tag for tag in ef1_device.tags if tag.key not in skip_tag
        ]  # filters out skipped tags

    for index, row in df.iterrows():
        try:
            ef1_id = row["ef1_device_id"]
            ef2_selector = row["ef2_entity_selector"]

            if tags_by_ef1_id.get(ef1_id, []):
                response = dt.custom_tags.post(
                    ef2_selector, tags_by_ef1_id.get(ef1_id, []), time_from=TIMEFRAME
                )
                print(response)
                if response.matched_entities_count > 1:
                    summary_report["multiple_ef2_hosts_found"].append(
                        f'EF2 hosts found for selector "{ef2_selector}": {response.matched_entities_count}'
                    )
                    migrated_with_warnings += 1
                elif response.matched_entities_count == 0:
                    summary_report["ef2_hosts_not_found"].append(
                        f'No matching EF2 hosts found for {ef1_id} ("{ef2_selector}").'
                    )
                    migrated_with_warnings += 1
                else:
                    successfully_migrated += 1
            else:
                summary_report["ef1_hosts_not_found"].append(
                        f'No EF1 host found for {ef1_id}.'
                    )
                migrated_with_warnings += 1
        except Exception as e:
            summary_report["unable_to_add_tags"].append(
                f"EF1 entity: {ef1_id}, EF2 selector: {ef2_selector}, issue: {e}"
            )
            failed_migration += 1

    # printing summary
    print("Summary:")
    print(
        f"[green]{successfully_migrated} sets of tags successfuly migrated to EF2 entities.[/green]"
    )
    print(
        f"[yellow]{migrated_with_warnings} sets of tags migrated with warnings to EF2 entities.[/yellow]"
    )
    print(f"[red]{failed_migration} sets of tags had issues migrating tags.[/red]")
    print(DIVIDER)
    if len(summary_report["unable_to_add_tags"]) > 0:
        print("Potential issues:")
        for issue in summary_report["unable_to_add_tags"]:
            print(f"[red]{issue}[/red]")
        print(DIVIDER)
    if len(summary_report["no_tags_found"]) > 0:
        print("Entities with no tags to migrate:")
        for issue in summary_report["no_tags_found"]:
            print(f"[yellow]{issue}[/yellow]")
        print(DIVIDER)
    if len(summary_report["ef2_hosts_not_found"]) > 0:
        print("EF2 hosts not found:")
        for issue in summary_report["ef2_hosts_not_found"]:
            print(f"[red]{issue}[/red]")
        print(DIVIDER)
    if len(summary_report["ef1_hosts_not_found"]) > 0:
        print("EF1 hosts not found:")
        for issue in summary_report["ef1_hosts_not_found"]:
            print(f"[yellow]{issue}[/yellow]")
        print(DIVIDER)
    if len(summary_report["multiple_ef2_hosts_found"]) > 0:
        print("Multiple EF2 hosts found:")
        for issue in summary_report["multiple_ef2_hosts_found"]:
            print(f"[yellow]{issue}[/yellow]")
        print(DIVIDER)