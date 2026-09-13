"""
Google Tasks API Implementation

Real Google Tasks API functions using Google Tasks API v1
"""

from typing import Dict, Any, List, Optional
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from datetime import datetime
import logging

from tools.resilience.retry_handler import (
    with_retry, RetryConfig, report_unconfirmed,
)
from tools.resilience.circuit_breaker import with_circuit_breaker
from tools.resilience.rate_limiter import with_rate_limit
from tools.resilience.cache import with_cache, invalidates_cache
from tools.google_api_client import aexecute

logger = logging.getLogger(__name__)


# ============================================================================
# GOOGLE TASKS API FUNCTIONS
# ============================================================================

@with_circuit_breaker("tasks")
@with_cache("tasks", ttl=300, user_id_param="credentials")  # Cache for 5 min (task lists rarely change)
@with_rate_limit("tasks", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def tasks_list_task_lists(
    credentials: Credentials,
    max_results: int = 100
) -> Dict[str, Any]:
    """
    List all task lists

    Args:
        credentials: OAuth2 credentials
        max_results: Maximum number of task lists to return (default: 100)

    Returns:
        Dictionary with list of task lists

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.tasks_service()

        logger.info(f"Listing task lists: max_results={max_results}")

        # List task lists
        results = await aexecute(service.tasklists().list(
            maxResults=max_results
        ))

        task_lists = results.get('items', [])

        logger.info(f"Retrieved {len(task_lists)} task lists")

        return {
            'task_lists': [
                {
                    'id': tl['id'],
                    'title': tl.get('title', ''),
                    'updated': tl.get('updated', ''),
                    'self_link': tl.get('selfLink', '')
                }
                for tl in task_lists
            ],
            'count': len(task_lists)
        }

    except HttpError as e:
        logger.error(f"Failed to list task lists: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in tasks_list_task_lists: {e}")
        raise


@with_circuit_breaker("tasks")
@with_cache("tasks", ttl=120, user_id_param="credentials")  # Cache for 2 min (tasks change frequently)
@with_rate_limit("tasks", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def tasks_list_tasks(
    credentials: Credentials,
    tasklist_id: str = "@default",
    max_results: int = 100,
    show_completed: bool = False,
    show_hidden: bool = False
) -> Dict[str, Any]:
    """
    List tasks in a task list

    Args:
        credentials: OAuth2 credentials
        tasklist_id: Task list ID (default: "@default")
        max_results: Maximum number of tasks to return (default: 100)
        show_completed: Include completed tasks (default: False)
        show_hidden: Include hidden tasks (default: False)

    Returns:
        Dictionary with list of tasks

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.tasks_service()

        logger.info(f"Listing tasks: tasklist={tasklist_id}, max={max_results}")

        # List tasks
        results = await aexecute(service.tasks().list(
            tasklist=tasklist_id,
            maxResults=max_results,
            showCompleted=show_completed,
            showHidden=show_hidden
        ))

        tasks = results.get('items', [])

        # Parse tasks
        parsed_tasks = []
        for task in tasks:
            parsed_task = {
                'id': task['id'],
                'title': task.get('title', ''),
                'status': task.get('status', ''),
                'notes': task.get('notes', ''),
                'due': task.get('due', ''),
                'completed': task.get('completed', ''),
                'updated': task.get('updated', ''),
                'self_link': task.get('selfLink', '')
            }
            parsed_tasks.append(parsed_task)

        logger.info(f"Retrieved {len(parsed_tasks)} tasks")

        return {
            'tasklist_id': tasklist_id,
            'tasks': parsed_tasks,
            'count': len(parsed_tasks)
        }

    except HttpError as e:
        logger.error(f"Failed to list tasks: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in tasks_list_tasks: {e}")
        raise


@with_circuit_breaker("tasks")
@with_cache("tasks", ttl=120, user_id_param="credentials")  # Cache for 2 min
@with_rate_limit("tasks", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def tasks_get_task(
    credentials: Credentials,
    tasklist_id: str,
    task_id: str
) -> Dict[str, Any]:
    """
    Get a specific task by ID

    Args:
        credentials: OAuth2 credentials
        tasklist_id: Task list ID
        task_id: Task ID

    Returns:
        Dictionary with task details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.tasks_service()

        logger.info(f"Getting task: tasklist={tasklist_id}, task={task_id}")

        # Get task
        task = await aexecute(service.tasks().get(
            tasklist=tasklist_id,
            task=task_id
        ))

        result = {
            'id': task['id'],
            'title': task.get('title', ''),
            'status': task.get('status', ''),
            'notes': task.get('notes', ''),
            'due': task.get('due', ''),
            'completed': task.get('completed', ''),
            'updated': task.get('updated', ''),
            'parent': task.get('parent', ''),
            'position': task.get('position', ''),
            'links': task.get('links', []),
            'self_link': task.get('selfLink', '')
        }

        logger.info(f"Task retrieved: {task.get('title', 'Untitled')}")

        return result

    except HttpError as e:
        logger.error(f"Failed to get task: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in tasks_get_task: {e}")
        raise


@with_circuit_breaker("tasks")
@with_rate_limit("tasks", user_id_param="credentials")
# NOTE: no @with_retry here — create makes a new task on every call.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("stvaranje zadatka")
@invalidates_cache("tasks")
async def tasks_create_task(
    credentials: Credentials,
    tasklist_id: str,
    title: str,
    notes: Optional[str] = None,
    due: Optional[str] = None,
    parent: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new task

    Args:
        credentials: OAuth2 credentials
        tasklist_id: Task list ID where the task will be created
        title: Task title
        notes: Task notes/description (optional)
        due: Due date in RFC 3339 format (optional)
        parent: Parent task ID for subtasks (optional)

    Returns:
        Dictionary with created task details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.tasks_service()

        logger.info(f"Creating task: '{title}' in tasklist={tasklist_id}")

        # Build task body
        task_body = {
            'title': title
        }

        if notes:
            task_body['notes'] = notes
        if due:
            task_body['due'] = due

        # Create task
        task = await aexecute(service.tasks().insert(
            tasklist=tasklist_id,
            body=task_body,
            parent=parent if parent else None
        ))

        result = {
            'id': task['id'],
            'title': task.get('title', ''),
            'task_status': task.get('status', ''),
            'notes': task.get('notes', ''),
            'due': task.get('due', ''),
            'updated': task.get('updated', ''),
            'self_link': task.get('selfLink', ''),
            'status': 'created'
        }

        logger.info(f"Task created: {task['id']}")

        return result

    except HttpError as e:
        logger.error(f"Failed to create task: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in tasks_create_task: {e}")
        raise


@with_circuit_breaker("tasks")
@with_rate_limit("tasks", user_id_param="credentials")
# Retry is safe: sets a known task id to a known state.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("tasks")
async def tasks_update_task(
    credentials: Credentials,
    tasklist_id: str,
    task_id: str,
    title: Optional[str] = None,
    notes: Optional[str] = None,
    status: Optional[str] = None,
    due: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update an existing task

    Args:
        credentials: OAuth2 credentials
        tasklist_id: Task list ID
        task_id: Task ID
        title: Updated title (optional)
        notes: Updated notes (optional)
        status: Updated status: "needsAction" or "completed" (optional)
        due: Updated due date in RFC 3339 format (optional)

    Returns:
        Dictionary with updated task details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.tasks_service()

        logger.info(f"Updating task: tasklist={tasklist_id}, task={task_id}")

        # Get current task
        current_task = await aexecute(service.tasks().get(
            tasklist=tasklist_id,
            task=task_id
        ))

        # Build clean update body with only mutable fields
        # Google Tasks API rejects unknown/read-only fields in update body
        update_body = {
            'id': current_task['id'],
            'title': current_task.get('title', ''),
            'status': current_task.get('status', 'needsAction'),
        }

        # Preserve existing optional fields
        if current_task.get('notes'):
            update_body['notes'] = current_task['notes']
        if current_task.get('due'):
            update_body['due'] = current_task['due']
        if current_task.get('completed'):
            update_body['completed'] = current_task['completed']
        if current_task.get('parent'):
            update_body['parent'] = current_task['parent']

        # Apply updates
        if title:
            update_body['title'] = title
        if notes is not None:
            update_body['notes'] = notes
        if status:
            update_body['status'] = status
            if status == 'completed':
                update_body['completed'] = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
            elif status == 'needsAction':
                # Remove completed timestamp when re-opening
                update_body.pop('completed', None)
        if due is not None:
            update_body['due'] = due

        # Update task
        updated_task = await aexecute(service.tasks().update(
            tasklist=tasklist_id,
            task=task_id,
            body=update_body
        ))

        result = {
            'id': updated_task['id'],
            'title': updated_task.get('title', ''),
            'task_status': updated_task.get('status', ''),
            'notes': updated_task.get('notes', ''),
            'due': updated_task.get('due', ''),
            'completed': updated_task.get('completed', ''),
            'updated': updated_task.get('updated', ''),
            'status': 'updated'
        }

        logger.info(f"Task updated: {task_id}")

        return result

    except HttpError as e:
        logger.error(f"Failed to update task: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in tasks_update_task: {e}")
        raise


@with_circuit_breaker("tasks")
@with_rate_limit("tasks", user_id_param="credentials")
# Retry is safe: a second delete returns 404, which is not retried.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("tasks")
async def tasks_delete_task(
    credentials: Credentials,
    tasklist_id: str,
    task_id: str
) -> Dict[str, Any]:
    """
    Delete a task

    Args:
        credentials: OAuth2 credentials
        tasklist_id: Task list ID
        task_id: Task ID

    Returns:
        Dictionary with deletion status

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.tasks_service()

        logger.info(f"Deleting task: tasklist={tasklist_id}, task={task_id}")

        # Delete task
        await aexecute(service.tasks().delete(
            tasklist=tasklist_id,
            task=task_id
        ))

        logger.info(f"Task deleted: {task_id}")

        return {
            'tasklist_id': tasklist_id,
            'task_id': task_id,
            'status': 'deleted'
        }

    except HttpError as e:
        logger.error(f"Failed to delete task: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in tasks_delete_task: {e}")
        raise


# ============================================================================
# TOOL REGISTRATION
# ============================================================================

def register_tasks_tools(tool_registry):
    """
    Register all Google Tasks tools in the tool registry

    Args:
        tool_registry: ToolRegistry instance
    """
    # tasks_list_task_lists
    tool_registry.register_tool(
        name="tasks_list_task_lists",
        function=tasks_list_task_lists,
        description="List all task lists in Google Tasks.",
        parameters={
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of task lists to return (default: 100)",
                    "default": 100
                }
            },
            "required": []
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # tasks_list_tasks
    tool_registry.register_tool(
        name="tasks_list_tasks",
        function=tasks_list_tasks,
        description="List tasks in a Google Tasks list.",
        parameters={
            "type": "object",
            "properties": {
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID (default: '@default')",
                    "default": "@default"
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of tasks (default: 100)",
                    "default": 100
                },
                "show_completed": {
                    "type": "boolean",
                    "description": "Include completed tasks (default: False)",
                    "default": False
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "Include hidden tasks (default: False)",
                    "default": False
                }
            },
            "required": []
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # tasks_get_task
    tool_registry.register_tool(
        name="tasks_get_task",
        function=tasks_get_task,
        description="Get a specific task by ID from Google Tasks.",
        parameters={
            "type": "object",
            "properties": {
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID"
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID"
                }
            },
            "required": ["tasklist_id", "task_id"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # tasks_create_task
    tool_registry.register_tool(
        name="tasks_create_task",
        function=tasks_create_task,
        description="Create a new task in Google Tasks.",
        parameters={
            "type": "object",
            "properties": {
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID"
                },
                "title": {
                    "type": "string",
                    "description": "Task title"
                },
                "notes": {
                    "type": "string",
                    "description": "Task notes/description (optional)"
                },
                "due": {
                    "type": "string",
                    "description": "Due date in RFC 3339 format (optional)"
                },
                "parent": {
                    "type": "string",
                    "description": "Parent task ID for subtasks (optional)"
                }
            },
            "required": ["tasklist_id", "title"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # tasks_update_task
    tool_registry.register_tool(
        name="tasks_update_task",
        function=tasks_update_task,
        description="Update an existing task in Google Tasks.",
        parameters={
            "type": "object",
            "properties": {
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID"
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID"
                },
                "title": {
                    "type": "string",
                    "description": "Updated title (optional)"
                },
                "notes": {
                    "type": "string",
                    "description": "Updated notes (optional)"
                },
                "status": {
                    "type": "string",
                    "description": "Updated status: 'needsAction' or 'completed' (optional)"
                },
                "due": {
                    "type": "string",
                    "description": "Updated due date in RFC 3339 format (optional)"
                }
            },
            "required": ["tasklist_id", "task_id"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # tasks_delete_task
    tool_registry.register_tool(
        name="tasks_delete_task",
        function=tasks_delete_task,
        description="Delete a task from Google Tasks.",
        parameters={
            "type": "object",
            "properties": {
                "tasklist_id": {
                    "type": "string",
                    "description": "Task list ID"
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID"
                }
            },
            "required": ["tasklist_id", "task_id"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    logger.info("Google Tasks tools registered successfully")
