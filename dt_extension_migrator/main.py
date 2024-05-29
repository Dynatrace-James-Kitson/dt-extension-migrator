import dt_extension_migrator.extension_apps.generic_commands
import dt_extension_migrator.extension_apps.remote_logs
import typer
from typing_extensions import Annotated
from dynatrace import Dynatrace
import pandas
from rich import print

from typing import Optional
import json

import dt_extension_migrator.extension_apps.remote_unix

app = typer.Typer()
app.add_typer(dt_extension_migrator.extension_apps.remote_unix.app, name="remote-unix")
app.add_typer(
    dt_extension_migrator.extension_apps.generic_commands.app, name="generic-commands"
)
app.add_typer(dt_extension_migrator.extension_apps.remote_logs.app, name="remote-logs")

SUPPORTED_EF1_EXTENSION_MAPPINGS = {
    "custom.remote.python.remote_agent": "com.dynatrace.extension.remote-unix",
    "custom.remote.python.generic_linux_commands": "custom:generic-commands",
}


@app.command()
def export_generic_configs(
    dt_url: Annotated[str, typer.Option(envvar="DT_URL")],
    dt_token: Annotated[str, typer.Option(envvar="DT_TOKEN")],
    extension_id: Annotated[str, typer.Option()],
    output_file: Optional[str] = None,
    index: Optional[str] = "group",
):
    dt = Dynatrace(dt_url, dt_token)
    configs = dt.extensions.list_instances(extension_id=extension_id)
    full_configs = []
    for config in configs:
        config = config.get_full_configuration(extension_id)
        full_config = config.json()
        properties = full_config.get("properties", {})
        for key in properties:
            if (key in index) or (key == "username"):
                full_config.update({key: properties[key]})
        full_config["properties"] = json.dumps(properties)
        full_configs.append(full_config)

    writer = pandas.ExcelWriter(
        output_file or f"{extension_id}-export.xlsx", engine="xlsxwriter"
    )
    df = pandas.DataFrame(full_configs)
    df_grouped = df.groupby(index)
    for key, group in df_grouped:
        group.to_excel(writer, sheet_name=key or "Default", index=False, header=True)
    writer.close()

    if extension_id not in SUPPORTED_EF1_EXTENSION_MAPPINGS:
        print(
            f"[bold yellow]WARNING - {extension_id} is not an extension currently supported for migrationl.[/bold yellow]"
        )


if __name__ == "__main__":
    app()
