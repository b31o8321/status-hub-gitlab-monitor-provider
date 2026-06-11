# status-hub-gitlab-monitor-provider

StatusHub provider for monitoring GitLab repository pipeline status.

This project is the StatusHub plugin version of the previous local
`gitlab-monitor` menu-bar app. The provider owns GitLab polling and status
snapshot generation; StatusHub owns display, configuration, installation, and
provider lifecycle.

## Configuration

Configure the provider in StatusHub settings:

- GitLab base URL, for example `https://gitlab.com`
- Personal access token
- Refresh interval in seconds
- Repositories JSON:

```json
[
  {
    "id": "group/project",
    "name": "Project",
    "branches": [
      {
        "selector": {
          "type": "fixed",
          "value": "main"
        }
      },
      {
        "selector": {
          "type": "rule",
          "prefix": "test",
          "format": "yyyymmdd"
        }
      }
    ]
  }
]
```

`id` may be a numeric GitLab project ID or a URL-encoded/path project ID such
as `group/project`.

The simple legacy shape is also supported:

```json
[
  {
    "id": "group/project",
    "name": "Project",
    "branch": "main"
  }
]
```

Branch selectors are compatible with the previous GitLab Monitor app model:

- `{"type":"fixed","value":"main"}` watches one branch.
- `{"type":"rule","prefix":"test","format":"yyyymmdd"}` resolves the latest
  matching branch such as `test-20260611`.
- `{"type":"regex","value":"^release-.*$"}` resolves the latest matching branch
  by regex.

The previous app's two-step interaction, connection first and then project /
branch selection, should be implemented as provider-owned configuration UI or a
configuration helper command. StatusHub should stay generic and only launch that
provider-owned flow.

## Local Check

```bash
STATUS_HUB_STATUS_FILE=/tmp/gitlab-monitor-status.json \
PYTHONPATH=src \
bin/gitlab-monitor-provider --once
```
