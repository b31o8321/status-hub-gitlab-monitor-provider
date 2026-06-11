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
- Monitored projects and branches through **选择项目和分支**

The project selector is provider-owned. StatusHub only opens the selector; this
provider searches GitLab projects, lets you add projects, and configures branch
watch rules without editing JSON by hand.

Branch selectors are compatible with the previous GitLab Monitor app model:

- Fixed branch: watches one branch such as `main`.
- Dynamic latest match: resolves the latest branch matching a prefix and date
  format, such as `test-20260611`.
- Regex: resolves the latest branch matching a custom regular expression.

## Config Shape

The selector writes `runtime/config.json`. The current shape is:

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

## Local Check

```bash
STATUS_HUB_STATUS_FILE=/tmp/gitlab-monitor-status.json \
PYTHONPATH=src \
bin/gitlab-monitor-provider --once
```
