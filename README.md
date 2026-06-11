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
    "branch": "main"
  }
]
```

`id` may be a numeric GitLab project ID or a URL-encoded/path project ID such
as `group/project`.

## Local Check

```bash
STATUS_HUB_STATUS_FILE=/tmp/gitlab-monitor-status.json \
PYTHONPATH=src \
bin/gitlab-monitor-provider --once
```

