"""
Google Tasks ADK Tools

ADK-compatible wrappers for Google Tasks operations.
"""

from typing import Optional
import logging

from tools.resilience.retry_handler import UnconfirmedWrite
logger = logging.getLogger(__name__)


# ============================================================================
# AUTHENTICATION HELPER
# ============================================================================

def _get_credentials():
    """Get OAuth credentials from token file"""
    try:
        from auth.oauth_manager import get_oauth_manager
        oauth_manager = get_oauth_manager()
        creds = oauth_manager.get_credentials()
        if creds and creds.valid:
            return creds
        logger.warning("No valid credentials available for Tasks operations")
        return None
    except Exception as e:
        logger.error(f"Failed to get credentials: {e}")
        return None


# ============================================================================
# TASKS TOOLS
# ============================================================================

async def tasks_list_task_lists() -> dict:
    """
    List all task lists (projects/categories).

    Task lists help organize tasks into different projects or categories.
    Examples: "Work", "Personal", "Projects", "Shopping"

    Returns:
        Dictionary with:
        {
            "task_lists": [
                {
                    "id": "tasklist_id",
                    "title": "Work",
                    "updated": "2024-01-15T10:30:00Z"
                },
                ...
            ],
            "count": 3
        }

    Example:
        result = await tasks_list_task_lists()
        for task_list in result['task_lists']:
            print(f"{task_list['title']}: {task_list['id']}")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated",
            "task_lists": [],
            "count": 0
        }

    try:
        from tools.api_implementations.tasks_api import tasks_list_task_lists as tasks_list_impl
        result = await tasks_list_impl(creds)
        logger.info(f"Listed {result.get('count', 0)} task lists")
        return result
    except Exception as e:
        logger.error(f"Error listing task lists: {e}")
        return {
            "error": str(e),
            "status": "error",
            "task_lists": [],
            "count": 0
        }


async def tasks_list_tasks(
    tasklist_id: str = "@default",
    show_completed: bool = False,
    max_results: int = 100
) -> dict:
    """
    List tasks in a specific task list.

    Args:
        tasklist_id: Task list ID or "@default" for primary list
        show_completed: Include completed tasks (default: False)
        max_results: Maximum number of tasks to return (default: 100)

    Returns:
        Dictionary with tasks and their details:
        {
            "tasks": [
                {
                    "id": "task_id",
                    "title": "Send quarterly report",
                    "status": "needsAction",
                    "due": "2024-01-20T17:00:00Z",
                    "notes": "Include Q4 financials",
                    "completed": null,
                    "updated": "2024-01-15T10:30:00Z"
                },
                ...
            ],
            "count": 5,
            "tasklist_id": "...",
            "tasklist_title": "Work"
        }

    Example:
        # List today's tasks
        result = await tasks_list_tasks(tasklist_id="@default")
        for task in result['tasks']:
            status = "✓" if task['status'] == 'completed' else "☐"
            print(f"{status} {task['title']}")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated",
            "tasks": [],
            "count": 0
        }

    try:
        from tools.api_implementations.tasks_api import tasks_list_tasks as tasks_list_impl
        result = await tasks_list_impl(
            creds, tasklist_id,
            max_results=max_results, show_completed=show_completed,
        )
        logger.info(f"Listed {result.get('count', 0)} tasks from list: {tasklist_id}")
        return result
    except Exception as e:
        logger.error(f"Error listing tasks: {e}")
        return {
            "error": str(e),
            "status": "error",
            "tasks": [],
            "count": 0,
            "tasklist_id": tasklist_id
        }


async def tasks_create_task(
    title: str,
    tasklist_id: str = "@default",
    notes: Optional[str] = None,
    due: Optional[str] = None,
    parent: Optional[str] = None
) -> dict:
    """
    Create a new task.

    Args:
        title: Task title (REQUIRED) - use action verbs (e.g., "Send report")
        tasklist_id: Task list ID or "@default" (default: "@default")
        notes: Additional notes/context (optional)
        due: Due date in RFC3339 format (e.g., "2024-01-20T17:00:00Z") (optional)
        parent: Parent task ID for subtasks (optional)

    Returns:
        Dictionary with created task details:
        {
            "id": "task_id",
            "title": "Send quarterly report",
            "status": "needsAction",
            "due": "2024-01-20T17:00:00Z",
            "notes": "Include Q4 financials",
            "tasklist_id": "...",
            "created": "2024-01-15T10:30:00Z"
        }

    Example:
        # Create a task with due date
        result = await tasks_create_task(
            title="Send quarterly report",
            notes="Include Q4 financials and projections",
            due="2024-01-20T17:00:00Z",
            tasklist_id="work_list_id"
        )
        print(f"Task created: {result['id']}")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.tasks_api import tasks_create_task as tasks_create_impl
        result = await tasks_create_impl(creds, tasklist_id, title, notes, due, parent)
        logger.info(f"Created task: {title}")
        return result
    except UnconfirmedWrite as e:
        # The write may already have landed; only the answer is gone. Reporting
        # "failed" here would read as "nothing happened" and invite the model to
        # call this tool again — the duplicate the missing retry was meant to
        # prevent, arriving one level up instead.
        logger.warning("Unconfirmed write in tasks_create_task: %s", e)
        return {"error": str(e), "status": "unknown", "outcome": "unknown"}
    except Exception as e:
        logger.error(f"Error creating task: {e}")
        return {
            "error": str(e),
            "status": "error"
        }


async def tasks_update_task(
    task_id: str,
    tasklist_id: str = "@default",
    title: Optional[str] = None,
    notes: Optional[str] = None,
    due: Optional[str] = None,
    status: Optional[str] = None
) -> dict:
    """
    Update an existing task.

    Args:
        task_id: Task ID (REQUIRED)
        tasklist_id: Task list ID or "@default" (default: "@default")
        title: New title (optional)
        notes: New notes (optional)
        due: New due date in RFC3339 format (optional)
        status: New status: "needsAction" or "completed" (optional)

    Returns:
        Dictionary with updated task details

    Example:
        # Mark task as completed
        result = await tasks_update_task(
            task_id="task123",
            status="completed"
        )

        # Update task with new due date
        result = await tasks_update_task(
            task_id="task123",
            due="2024-01-25T17:00:00Z",
            notes="Extended deadline approved"
        )
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.tasks_api import tasks_update_task as tasks_update_impl
        result = await tasks_update_impl(creds, tasklist_id, task_id, title, notes, status, due)
        logger.info(f"Updated task: {task_id}")
        return result
    except Exception as e:
        logger.error(f"Error updating task: {e}")
        return {
            "error": str(e),
            "status": "error",
            "task_id": task_id
        }


async def tasks_delete_task(
    task_id: str,
    tasklist_id: str = "@default"
) -> dict:
    """
    Delete a task.

    Args:
        task_id: Task ID (REQUIRED)
        tasklist_id: Task list ID or "@default" (default: "@default")

    Returns:
        Dictionary with deletion status:
        {
            "status": "deleted",
            "task_id": "task123",
            "tasklist_id": "..."
        }

    Example:
        result = await tasks_delete_task(task_id="task123")
        if result['status'] == 'deleted':
            print("Task deleted successfully")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.tasks_api import tasks_delete_task as tasks_delete_impl
        result = await tasks_delete_impl(creds, tasklist_id, task_id)
        logger.info(f"Deleted task: {task_id}")
        return result
    except Exception as e:
        logger.error(f"Error deleting task: {e}")
        return {
            "error": str(e),
            "status": "error",
            "task_id": task_id
        }


async def tasks_complete_task(
    task_id: str,
    tasklist_id: str = "@default"
) -> dict:
    """
    Mark a task as completed (convenience function).

    This is a shorthand for tasks_update_task with status="completed".

    Args:
        task_id: Task ID (REQUIRED)
        tasklist_id: Task list ID or "@default" (default: "@default")

    Returns:
        Dictionary with completed task details

    Example:
        result = await tasks_complete_task(task_id="task123")
        print(f"Task completed: {result['title']}")
    """
    return await tasks_update_task(
        task_id=task_id,
        tasklist_id=tasklist_id,
        status="completed"
    )
