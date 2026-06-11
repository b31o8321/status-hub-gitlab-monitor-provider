from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request
import argparse
import json
import os
import re
import tempfile
import time


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_STATUS_FILE = ROOT_DIR / "runtime" / "status.json"
DEFAULT_CONFIG_FILE = ROOT_DIR / "runtime" / "config.json"

STATUS_SEVERITY = {
    "idle": 0,
    "success": 1,
    "unknown": 2,
    "running": 3,
    "attention": 4,
    "failed": 5,
}

GITLAB_TO_HUB = {
    "success": "success",
    "failed": "failed",
    "running": "running",
    "pending": "running",
    "created": "running",
    "waiting_for_resource": "running",
    "preparing": "running",
    "scheduled": "running",
    "manual": "attention",
    "skipped": "attention",
    "canceled": "unknown",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    status_file = Path(os.environ.get("STATUS_HUB_STATUS_FILE") or DEFAULT_STATUS_FILE)
    config_file = Path(os.environ.get("STATUS_HUB_CONFIG_FILE") or DEFAULT_CONFIG_FILE)

    while True:
        snapshot = build_snapshot(load_config(config_file))
        write_json(status_file, snapshot)
        if args.once:
            return 0
        time.sleep(refresh_interval(load_config(config_file)))


def load_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    return data if isinstance(data, dict) else {}


def build_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    repositories = parse_repositories(config.get("repositories"))
    if not repositories:
        return {
            "status": "idle",
            "summary": "未配置 GitLab 仓库",
            "updatedAt": now_iso(),
            "items": [],
        }

    settings = config.get("gitlab", {})
    settings = settings if isinstance(settings, dict) else {}
    base_url = str(settings.get("baseUrl") or "https://gitlab.com").rstrip("/")
    token = str(settings.get("accessToken") or "").strip()

    items = []
    for repository in repositories:
        items.extend(repository_items(base_url, token, repository))
    return {
        "status": overall_status([str(item.get("status") or "unknown") for item in items]),
        "summary": summary_text(items),
        "updatedAt": now_iso(),
        "items": items,
    }


def parse_repositories(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    repositories = []
    for item in value:
        if not isinstance(item, dict):
            continue
        project_id = str(item.get("id") or "").strip()
        if project_id:
            repositories.append(item)
    return repositories


def repository_items(base_url: str, token: str, repository: dict[str, Any]) -> list[dict[str, Any]]:
    watches = branch_watches(repository)
    if not watches:
        watches = [{"selector": {"type": "fixed", "value": str(repository.get("branch") or "main")}}]
    return [
        repository_item(base_url, token, repository, watch)
        for watch in watches
    ]


def branch_watches(repository: dict[str, Any]) -> list[dict[str, Any]]:
    branches = repository.get("branches")
    if isinstance(branches, list):
        return [branch for branch in branches if isinstance(branch, dict)]
    branch = str(repository.get("branch") or "").strip()
    if branch:
        return [{"selector": {"type": "fixed", "value": branch}}]
    return []


def repository_item(base_url: str, token: str, repository: dict[str, Any], watch: dict[str, Any]) -> dict[str, Any]:
    project_id = str(repository.get("id") or "").strip()
    project_path = str(repository.get("projectPath") or project_id).strip()
    name = str(repository.get("name") or project_id)
    selector = watch.get("selector") if isinstance(watch.get("selector"), dict) else {"type": "fixed", "value": "main"}
    branch_hint = selector_hint(selector)
    try:
        branch = resolve_branch(base_url, token, project_path, selector)
        if not branch:
            return {
                "id": stable_id(project_path, branch_hint),
                "title": name,
                "subtitle": f"{branch_hint} 未匹配到分支",
                "status": "attention",
                "value": "无分支",
                "detail": {"selector": branch_hint},
                "links": [project_link(base_url, project_path)],
            }
        pipeline = latest_pipeline(base_url, token, project_path, branch)
    except Exception as exc:
        return {
            "id": stable_id(project_path, branch_hint),
            "title": name,
            "subtitle": str(exc),
            "status": "failed",
            "value": "失败",
            "detail": {"selector": branch_hint},
            "links": [project_link(base_url, project_path)],
        }

    if not pipeline:
        return {
            "id": stable_id(project_path, branch),
            "title": name,
            "subtitle": f"{branch} 没有 pipeline",
            "status": "attention",
            "value": "无记录",
            "detail": {"branch": branch},
            "links": [project_link(base_url, project_path)],
        }

    gitlab_status = str(pipeline.get("status") or "unknown")
    hub_status = GITLAB_TO_HUB.get(gitlab_status, "unknown")
    pipeline_id = pipeline.get("id")
    if hub_status == "running" and pipeline_id:
        pipeline = pipeline_detail(base_url, token, project_path, pipeline_id) or pipeline
    pipeline_url = str(pipeline.get("web_url") or "")
    detail = {
        "branch": branch,
        "pipeline": f"#{pipeline_id}" if pipeline_id else "",
        "updatedText": relative_age(str(pipeline.get("updated_at") or pipeline.get("created_at") or "")),
    }
    if hub_status == "running":
        detail.update(running_progress_detail(base_url, token, project_path, branch, pipeline))
    detail = {key: value for key, value in detail.items() if value}
    links = [project_link(base_url, project_path)]
    if pipeline_url:
        links.insert(0, {"id": f"{stable_id(project_path, branch)}-pipeline", "title": "Pipeline", "url": pipeline_url})
    return {
        "id": stable_id(project_path, branch),
        "title": name,
        "subtitle": branch,
        "status": hub_status,
        "url": pipeline_url or None,
        "value": pipeline_value(gitlab_status),
        "detail": detail,
        "links": links,
    }


def resolve_branch(base_url: str, token: str, project_id: str, selector: dict[str, Any]) -> str | None:
    selector_type = str(selector.get("type") or "fixed")
    if selector_type == "fixed":
        return str(selector.get("value") or "main").strip() or "main"

    if selector_type == "rule":
        prefix = str(selector.get("prefix") or "test")
        pattern = branch_rule_regex(prefix, str(selector.get("format") or "yyyymmdd"))
        search = f"^{prefix}-"
        return latest_matching_branch(base_url, token, project_id, pattern, search)

    if selector_type == "regex":
        pattern = str(selector.get("value") or "").strip()
        if not pattern:
            return None
        return latest_matching_branch(base_url, token, project_id, pattern, None)

    return None


def branch_rule_regex(prefix: str, date_format: str) -> str:
    escaped = re.escape(prefix)
    if date_format == "yyyymmddDashed":
        return rf"^{escaped}-\d{{4}}-\d{{2}}-\d{{2}}$"
    if date_format == "yyyymmddDotted":
        return rf"^{escaped}-\d{{4}}\.\d{{2}}\.\d{{2}}$"
    if date_format == "yyyymmddWithTail":
        return rf"^{escaped}-\d{{8}}-.+$"
    return rf"^{escaped}-\d{{8}}$"


def latest_matching_branch(
    base_url: str,
    token: str,
    project_id: str,
    pattern: str,
    search: str | None,
) -> str | None:
    branches = fetch_branches(base_url, token, project_id, search)
    regex = re.compile(pattern)
    matched = sorted(
        [name for name in branches if regex.search(name)],
        reverse=True,
    )
    return matched[0] if matched else None


def fetch_branches(base_url: str, token: str, project_id: str, search: str | None = None) -> list[str]:
    encoded_project = parse.quote(project_id, safe="")
    query: dict[str, Any] = {"per_page": 100}
    if search:
        query["search"] = search
    url = f"{base_url}/api/v4/projects/{encoded_project}/repository/branches?{parse.urlencode(query)}"
    payload = gitlab_get(url, token)
    if not isinstance(payload, list):
        return []
    return [
        str(branch.get("name"))
        for branch in payload
        if isinstance(branch, dict) and branch.get("name")
    ]


def latest_pipeline(base_url: str, token: str, project_id: str, branch: str) -> dict[str, Any] | None:
    encoded_project = parse.quote(project_id, safe="")
    query = parse.urlencode({"ref": branch, "per_page": 1})
    url = f"{base_url}/api/v4/projects/{encoded_project}/pipelines?{query}"
    payload = gitlab_get(url, token)
    if isinstance(payload, list) and payload:
        return payload[0] if isinstance(payload[0], dict) else None
    return None


def pipeline_detail(base_url: str, token: str, project_id: str, pipeline_id: Any) -> dict[str, Any] | None:
    encoded_project = parse.quote(project_id, safe="")
    url = f"{base_url}/api/v4/projects/{encoded_project}/pipelines/{pipeline_id}"
    payload = gitlab_get(url, token)
    return payload if isinstance(payload, dict) else None


def recent_success_durations(base_url: str, token: str, project_id: str, branch: str, limit: int = 5) -> list[float]:
    encoded_project = parse.quote(project_id, safe="")
    query = parse.urlencode({
        "ref": branch,
        "status": "success",
        "per_page": max(1, min(limit, 20)),
        "order_by": "id",
        "sort": "desc",
    })
    url = f"{base_url}/api/v4/projects/{encoded_project}/pipelines?{query}"
    payload = gitlab_get(url, token)
    if not isinstance(payload, list):
        return []

    durations: list[float] = []
    for pipeline in payload:
        if not isinstance(pipeline, dict) or not pipeline.get("id"):
            continue
        try:
            detail = pipeline_detail(base_url, token, project_id, pipeline["id"]) or {}
        except Exception:
            continue
        duration = run_duration_seconds(detail)
        if duration and duration > 0:
            durations.append(duration)
    return durations


def running_progress_detail(
    base_url: str,
    token: str,
    project_id: str,
    branch: str,
    pipeline: dict[str, Any],
) -> dict[str, str]:
    started = parse_gitlab_datetime(
        str(pipeline.get("started_at") or pipeline.get("created_at") or pipeline.get("updated_at") or "")
    )
    if not started:
        return {}

    elapsed = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    durations = recent_success_durations(base_url, token, project_id, branch)
    baseline = sum(durations) / len(durations) if durations else 0
    if baseline > 0:
        progress = min(elapsed / baseline, 1.0)
        overrun = elapsed > baseline
        return {
            "progressPercent": f"{progress:.3f}",
            "progressText": progress_label(elapsed, baseline, overrun),
            "elapsedText": f"已运行 {format_duration(elapsed)}",
        }
    return {
        "progressText": f"已运行 {format_duration(elapsed)}",
        "elapsedText": f"已运行 {format_duration(elapsed)}",
    }


def run_duration_seconds(pipeline: dict[str, Any]) -> float | None:
    duration = pipeline.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        return float(duration)
    started = parse_gitlab_datetime(str(pipeline.get("started_at") or ""))
    finished = parse_gitlab_datetime(str(pipeline.get("finished_at") or ""))
    if not started or not finished:
        return None
    interval = (finished - started).total_seconds()
    return interval if interval > 0 else None


def gitlab_get(url: str, token: str) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["PRIVATE-TOKEN"] = token
    req = request.Request(url, headers=headers)
    try:
        with request.urlopen(req, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(f"GitLab API {exc.code}: {exc.reason}") from exc


def project_link(base_url: str, project_id: str) -> dict[str, str]:
    if project_id.isdigit():
        return {"id": f"{project_id}-project", "title": "项目", "url": base_url}
    return {"id": f"{stable_id(project_id, '')}-project", "title": "项目", "url": f"{base_url}/{project_id}"}


def selector_hint(selector: dict[str, Any]) -> str:
    selector_type = str(selector.get("type") or "fixed")
    if selector_type == "rule":
        return f"{selector.get('prefix') or 'test'}-..."
    if selector_type == "regex":
        return str(selector.get("value") or "regex")
    return str(selector.get("value") or "main")


def stable_id(project_id: str, branch: str) -> str:
    raw = f"{project_id}-{branch}".strip("-")
    return "".join(char if char.isalnum() else "-" for char in raw).strip("-").lower() or "repository"


def pipeline_value(status: str) -> str:
    return {
        "success": "成功",
        "failed": "失败",
        "running": "运行中",
        "pending": "等待中",
        "manual": "待手动",
        "skipped": "跳过",
        "canceled": "取消",
    }.get(status, status)


def overall_status(statuses: list[str]) -> str:
    if not statuses:
        return "idle"
    return max(statuses, key=lambda status: STATUS_SEVERITY.get(status, 2))


def summary_text(items: list[dict[str, Any]]) -> str:
    failed = sum(1 for item in items if item.get("status") == "failed")
    running = sum(1 for item in items if item.get("status") == "running")
    attention = sum(1 for item in items if item.get("status") == "attention")
    if failed:
        return f"{failed} 个 Pipeline 失败"
    if running:
        return f"{running} 个 Pipeline 运行中"
    if attention:
        return f"{attention} 个 Pipeline 需要关注"
    return f"{len(items)} 个分支正常"


def refresh_interval(config: dict[str, Any]) -> int:
    try:
        value = int(config.get("refreshIntervalSeconds") or 60)
    except (TypeError, ValueError):
        value = 60
    return max(15, value)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def short_datetime(value: str) -> str:
    if not value:
        return ""
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return value


def parse_gitlab_datetime(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def relative_age(value: str) -> str:
    parsed = parse_gitlab_datetime(value)
    if not parsed:
        return short_datetime(value)
    seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    return f"{format_duration(seconds)}前"


def progress_label(elapsed: float, baseline: float, overrun: bool) -> str:
    if overrun:
        return f"{format_duration(elapsed)}（超过历史 {format_duration(baseline)}）"
    return f"{format_duration(elapsed)} / {format_duration(baseline)}"


def format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if remaining_minutes == 0:
        return f"{hours}h"
    return f"{hours}h {remaining_minutes}m"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        temp_name = handle.name
    Path(temp_name).replace(path)
