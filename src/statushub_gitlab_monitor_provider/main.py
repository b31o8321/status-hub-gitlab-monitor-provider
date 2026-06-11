from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, parse, request
import argparse
import json
import os
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

    items = [repository_item(base_url, token, repository) for repository in repositories]
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


def repository_item(base_url: str, token: str, repository: dict[str, Any]) -> dict[str, Any]:
    project_id = str(repository.get("id") or "").strip()
    name = str(repository.get("name") or project_id)
    branch = str(repository.get("branch") or "main").strip()
    try:
        pipeline = latest_pipeline(base_url, token, project_id, branch)
    except Exception as exc:
        return {
            "id": stable_id(project_id, branch),
            "title": name,
            "subtitle": str(exc),
            "status": "failed",
            "value": "失败",
            "detail": {"branch": branch},
            "links": [project_link(base_url, project_id)],
        }

    if not pipeline:
        return {
            "id": stable_id(project_id, branch),
            "title": name,
            "subtitle": f"{branch} 没有 pipeline",
            "status": "attention",
            "value": "无记录",
            "detail": {"branch": branch},
            "links": [project_link(base_url, project_id)],
        }

    gitlab_status = str(pipeline.get("status") or "unknown")
    hub_status = GITLAB_TO_HUB.get(gitlab_status, "unknown")
    pipeline_url = str(pipeline.get("web_url") or "")
    detail = {
        "branch": branch,
        "pipeline": f"#{pipeline.get('id')}" if pipeline.get("id") else "",
        "updated": short_datetime(str(pipeline.get("updated_at") or pipeline.get("created_at") or "")),
    }
    detail = {key: value for key, value in detail.items() if value}
    links = [project_link(base_url, project_id)]
    if pipeline_url:
        links.insert(0, {"id": f"{stable_id(project_id, branch)}-pipeline", "title": "最新 Pipeline", "url": pipeline_url})
    return {
        "id": stable_id(project_id, branch),
        "title": name,
        "subtitle": branch,
        "status": hub_status,
        "url": pipeline_url or None,
        "value": pipeline_value(gitlab_status),
        "detail": detail,
        "links": links,
    }


def latest_pipeline(base_url: str, token: str, project_id: str, branch: str) -> dict[str, Any] | None:
    encoded_project = parse.quote(project_id, safe="")
    query = parse.urlencode({"ref": branch, "per_page": 1})
    url = f"{base_url}/api/v4/projects/{encoded_project}/pipelines?{query}"
    payload = gitlab_get(url, token)
    if isinstance(payload, list) and payload:
        return payload[0] if isinstance(payload[0], dict) else None
    return None


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


def stable_id(project_id: str, branch: str) -> str:
    raw = f"{project_id}-{branch}".strip("-")
    return "".join(char if char.isalnum() else "-" for char in raw).strip("-").lower() or "repository"


def pipeline_value(status: str) -> str:
    return {
        "success": "通过",
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
    return f"{len(items)} 个仓库正常"


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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        temp_name = handle.name
    Path(temp_name).replace(path)
