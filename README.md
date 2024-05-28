# Dynatrace Extension Migrator

Helps with moving the configurations of select Extensions 1.0 extensions to their 2.0 equivalents.

## Requirements

- Python 3.10+
- An API token:
  - Ust the 'Extension Development' token or provide the following scopes:
    - extensions.read
    - extensionConfigurations.read
    - extensionConfigurations.write

## Installation

`pip install dt-extension-migrator`

## Usage
Each supported EF1 extension has a subcommand e.g. `dt-ext-migrator remote-unix --help`

Each will have a similar set of commands for pulling the EF1 configurations and pushing the converted EF2 extensions to the Dynatrace environment.

### Environment details
You can specify the Dynatrace environment URL and API token either in the `--dt-url` and `--dt-token` options or in DT_URL and DT_TOKEN environment variables (recommended).

### Pulling configurations
When pulling EF1 configurations you can specify the output file (optional) and one or more "indexes" by specifying the `--index` option for each field you want to 'group' configurations on. For example, if you want to group just on the 'group' in the EF1 configuration you can run:

`dt-ext-migrator remote-unix pull --index group`

If you want to group by the group and the configured username you can run:

`dt-ext-migrator remote-unix pull --index group --index username`

You will have an Excel spreadsheet generated with a tab for each set of grouped dimensions you will be able to use when pushing the converted configs.

### Pushing configurations
Once you have the spreadsheet generated you can use this to convert and push the configurations to their EF2 equivalents.

`dt-ext-migrator remote-unix push --input-file .\custom.remote.python.remote_agent-export.xlsx --sheet group1-myuser --version 1.0.0 --ag-group ag_group-default`

A summary will be printed after a successful push. E.g.:
```
2 endpoints will attempt to be added to the monitoring configuration.
Configs created successfully. Response: 200
Link to monitoring configuration: https://<environment>/ui/hub/ext/listing/registered/<extensionId>/fe14090c-4bfe-30b5-b88b-84a8e6f65607/read
```

The configuration will be disabled by default so you can review it, add authentication, and making any other needed chagnes before enabling.