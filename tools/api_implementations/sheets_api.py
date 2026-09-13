"""
Google Sheets API Implementation

Real Google Sheets API functions using Google Sheets API v4
"""

from typing import Dict, Any, List, Optional
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
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
# GOOGLE SHEETS API FUNCTIONS
# ============================================================================

@with_circuit_breaker("sheets")
@with_rate_limit("sheets", user_id_param="credentials")
# NOTE: no @with_retry here — create makes a new spreadsheet on every call; a retry after a lost
# answer leaves two.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("stvaranje tablice")
@invalidates_cache("sheets")
async def sheets_create_spreadsheet(
    credentials: Credentials,
    title: str,
    sheet_titles: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Create a new Google Sheets spreadsheet

    Args:
        credentials: OAuth2 credentials
        title: Spreadsheet title
        sheet_titles: List of sheet names to create (optional)

    Returns:
        Dictionary with created spreadsheet details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.sheets_service()

        logger.info(f"Creating Google Sheets spreadsheet: {title}")

        # Build spreadsheet body
        spreadsheet_body = {
            'properties': {
                'title': title
            }
        }

        # Add custom sheets if provided
        if sheet_titles:
            spreadsheet_body['sheets'] = [
                {'properties': {'title': sheet_title}}
                for sheet_title in sheet_titles
            ]

        # Create spreadsheet
        spreadsheet = await aexecute(service.spreadsheets().create(
            body=spreadsheet_body,
            fields='spreadsheetId,spreadsheetUrl,sheets.properties'
        ))

        logger.info(f"Spreadsheet created: {spreadsheet['spreadsheetId']}")

        return {
            'spreadsheet_id': spreadsheet['spreadsheetId'],
            'spreadsheet_url': spreadsheet.get('spreadsheetUrl'),
            'sheets': [
                {
                    'sheet_id': sheet['properties']['sheetId'],
                    'title': sheet['properties']['title']
                }
                for sheet in spreadsheet.get('sheets', [])
            ],
            'status': 'created'
        }

    except HttpError as e:
        logger.error(f"Failed to create spreadsheet: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in sheets_create_spreadsheet: {e}")
        raise


@with_circuit_breaker("sheets")
@with_cache("sheets", ttl=300, user_id_param="credentials")  # Cache for 5 min
@with_rate_limit("sheets", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def sheets_get_values(
    credentials: Credentials,
    spreadsheet_id: str,
    range: str,
    value_render_option: str = "FORMATTED_VALUE"
) -> Dict[str, Any]:
    """
    Get values from a range in a spreadsheet

    Args:
        credentials: OAuth2 credentials
        spreadsheet_id: Google Sheets spreadsheet ID
        range: A1 notation range (e.g., 'Sheet1!A1:D10')
        value_render_option: How values should be rendered
            - "FORMATTED_VALUE" (default)
            - "UNFORMATTED_VALUE"
            - "FORMULA"

    Returns:
        Dictionary with values from the range

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.sheets_service()

        logger.info(f"Getting values from spreadsheet: {spreadsheet_id}, range: {range}")

        # Get values
        result = await aexecute(service.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueRenderOption=value_render_option
        ))

        values = result.get('values', [])

        logger.info(f"Retrieved {len(values)} rows")

        return {
            'spreadsheet_id': spreadsheet_id,
            'range': result.get('range'),
            'values': values,
            'row_count': len(values),
            'column_count': len(values[0]) if values else 0,
            'major_dimension': result.get('majorDimension', 'ROWS')
        }

    except HttpError as e:
        logger.error(f"Failed to get values: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in sheets_get_values: {e}")
        raise


@with_circuit_breaker("sheets")
@with_rate_limit("sheets", user_id_param="credentials")
# Retry is safe: writes the given values to an absolute A1 range, so a
# repeat lands on exactly the same cells.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("sheets")
async def sheets_update_values(
    credentials: Credentials,
    spreadsheet_id: str,
    range: str,
    values: List[List[Any]],
    value_input_option: str = "USER_ENTERED"
) -> Dict[str, Any]:
    """
    Update values in a range

    Args:
        credentials: OAuth2 credentials
        spreadsheet_id: Google Sheets spreadsheet ID
        range: A1 notation range (e.g., 'Sheet1!A1:D10')
        values: 2D array of values to update
        value_input_option: How input should be interpreted
            - "USER_ENTERED" (default) - parses input as if typed in UI
            - "RAW" - input is not parsed

    Returns:
        Dictionary with update result

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.sheets_service()

        logger.info(f"Updating values in spreadsheet: {spreadsheet_id}, range: {range}")

        # Update values
        body = {
            'values': values
        }

        result = await aexecute(service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption=value_input_option,
            body=body
        ))

        updated_cells = result.get('updatedCells', 0)
        updated_rows = result.get('updatedRows', 0)
        updated_columns = result.get('updatedColumns', 0)

        logger.info(f"Updated {updated_cells} cells ({updated_rows} rows, {updated_columns} columns)")

        return {
            'spreadsheet_id': result.get('spreadsheetId'),
            'updated_range': result.get('updatedRange'),
            'updated_cells': updated_cells,
            'updated_rows': updated_rows,
            'updated_columns': updated_columns,
            'status': 'updated'
        }

    except HttpError as e:
        logger.error(f"Failed to update values: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in sheets_update_values: {e}")
        raise


@with_circuit_breaker("sheets")
@with_rate_limit("sheets", user_id_param="credentials")
# NOTE: no @with_retry here — append is not idempotent: a timeout after Sheets accepted the rows
# would add them a second time.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("dodavanje redaka u tablicu")
@invalidates_cache("sheets")
async def sheets_append_values(
    credentials: Credentials,
    spreadsheet_id: str,
    range: str,
    values: List[List[Any]],
    value_input_option: str = "USER_ENTERED"
) -> Dict[str, Any]:
    """
    Append values to the end of a range

    Args:
        credentials: OAuth2 credentials
        spreadsheet_id: Google Sheets spreadsheet ID
        range: A1 notation range (e.g., 'Sheet1!A1:D1')
        values: 2D array of values to append
        value_input_option: How input should be interpreted

    Returns:
        Dictionary with append result

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.sheets_service()

        logger.info(f"Appending values to spreadsheet: {spreadsheet_id}, range: {range}")

        # Append values
        body = {
            'values': values
        }

        result = await aexecute(service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range,
            valueInputOption=value_input_option,
            insertDataOption='INSERT_ROWS',
            body=body
        ))

        updated_cells = result.get('updates', {}).get('updatedCells', 0)
        updated_rows = result.get('updates', {}).get('updatedRows', 0)

        logger.info(f"Appended {updated_rows} rows ({updated_cells} cells)")

        return {
            'spreadsheet_id': result.get('spreadsheetId'),
            'table_range': result.get('tableRange'),
            'updated_range': result.get('updates', {}).get('updatedRange'),
            'updated_cells': updated_cells,
            'updated_rows': updated_rows,
            'status': 'appended'
        }

    except HttpError as e:
        logger.error(f"Failed to append values: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in sheets_append_values: {e}")
        raise


@with_circuit_breaker("sheets")
@with_rate_limit("sheets", user_id_param="credentials")
# Retry is safe: clearing an already-cleared range is a no-op.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("sheets")
async def sheets_clear_values(
    credentials: Credentials,
    spreadsheet_id: str,
    range: str
) -> Dict[str, Any]:
    """
    Clear values from a range

    Args:
        credentials: OAuth2 credentials
        spreadsheet_id: Google Sheets spreadsheet ID
        range: A1 notation range (e.g., 'Sheet1!A1:D10')

    Returns:
        Dictionary with clear result

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.sheets_service()

        logger.info(f"Clearing values from spreadsheet: {spreadsheet_id}, range: {range}")

        # Clear values
        result = await aexecute(service.spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=range,
            body={}
        ))

        logger.info(f"Cleared range: {result.get('clearedRange')}")

        return {
            'spreadsheet_id': result.get('spreadsheetId'),
            'cleared_range': result.get('clearedRange'),
            'status': 'cleared'
        }

    except HttpError as e:
        logger.error(f"Failed to clear values: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in sheets_clear_values: {e}")
        raise


@with_circuit_breaker("sheets")
@with_rate_limit("sheets", user_id_param="credentials")
# NOTE: no @with_retry here — the caller supplies the requests; insertRows/appendCells among them
# are not idempotent.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("batch izmjena tablice")
@invalidates_cache("sheets")
async def sheets_batch_update(
    credentials: Credentials,
    spreadsheet_id: str,
    requests: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Perform batch updates on a spreadsheet (formatting, formulas, etc.)

    Args:
        credentials: OAuth2 credentials
        spreadsheet_id: Google Sheets spreadsheet ID
        requests: List of request objects (see Google Sheets API documentation)

    Returns:
        Dictionary with batch update results

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.sheets_service()

        logger.info(f"Performing batch update on spreadsheet: {spreadsheet_id}")
        logger.info(f"Number of requests: {len(requests)}")

        # Execute batch update
        body = {
            'requests': requests
        }

        result = await aexecute(service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body=body
        ))

        logger.info(f"Batch update completed: {spreadsheet_id}")

        return {
            'spreadsheet_id': result.get('spreadsheetId'),
            'replies': result.get('replies', []),
            'updated_spreadsheet': result.get('updatedSpreadsheet'),
            'status': 'batch_updated'
        }

    except HttpError as e:
        logger.error(f"Failed to perform batch update: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in sheets_batch_update: {e}")
        raise


@with_circuit_breaker("sheets")
@with_cache("sheets", ttl=600, user_id_param="credentials")  # Cache for 10 min (metadata changes less)
@with_rate_limit("sheets", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def sheets_get_spreadsheet(
    credentials: Credentials,
    spreadsheet_id: str,
    include_grid_data: bool = False
) -> Dict[str, Any]:
    """
    Get metadata and properties of a spreadsheet

    Args:
        credentials: OAuth2 credentials
        spreadsheet_id: Google Sheets spreadsheet ID
        include_grid_data: Include cell data (default: False)

    Returns:
        Dictionary with spreadsheet metadata

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.sheets_service()

        logger.info(f"Getting spreadsheet metadata: {spreadsheet_id}")

        # Get spreadsheet
        result = await aexecute(service.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            includeGridData=include_grid_data
        ))

        sheets_info = [
            {
                'sheet_id': sheet['properties']['sheetId'],
                'title': sheet['properties']['title'],
                'index': sheet['properties']['index'],
                'sheet_type': sheet['properties'].get('sheetType', 'GRID'),
                'grid_properties': sheet['properties'].get('gridProperties', {})
            }
            for sheet in result.get('sheets', [])
        ]

        logger.info(f"Spreadsheet retrieved: {result['properties']['title']} ({len(sheets_info)} sheets)")

        return {
            'spreadsheet_id': result['spreadsheetId'],
            'title': result['properties']['title'],
            'locale': result['properties'].get('locale'),
            'timezone': result['properties'].get('timeZone'),
            'spreadsheet_url': result.get('spreadsheetUrl'),
            'sheets': sheets_info,
            'named_ranges': result.get('namedRanges', []),
            'developer_metadata': result.get('developerMetadata', [])
        }

    except HttpError as e:
        logger.error(f"Failed to get spreadsheet: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in sheets_get_spreadsheet: {e}")
        raise


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _convert_to_a1_notation(row: int, col: int) -> str:
    """
    Convert row/column indices to A1 notation

    Args:
        row: Row index (0-based)
        col: Column index (0-based)

    Returns:
        A1 notation (e.g., 'A1', 'B5')
    """
    col_letter = ''
    col += 1  # Make 1-based
    while col > 0:
        col -= 1
        col_letter = chr(col % 26 + 65) + col_letter
        col //= 26
    return f"{col_letter}{row + 1}"


# ============================================================================
# TOOL REGISTRATION
# ============================================================================

def register_sheets_tools(tool_registry):
    """
    Register all Google Sheets tools in the tool registry

    Args:
        tool_registry: ToolRegistry instance
    """
    # sheets_create_spreadsheet
    tool_registry.register_tool(
        name="sheets_create_spreadsheet",
        function=sheets_create_spreadsheet,
        description="Create a new Google Sheets spreadsheet with optional custom sheet names.",
        parameters={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Spreadsheet title"
                },
                "sheet_titles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of sheet names to create (optional)"
                }
            },
            "required": ["title"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # sheets_get_values
    tool_registry.register_tool(
        name="sheets_get_values",
        function=sheets_get_values,
        description="Get values from a range in a spreadsheet using A1 notation.",
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Google Sheets spreadsheet ID"
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range (e.g., 'Sheet1!A1:D10')"
                },
                "value_render_option": {
                    "type": "string",
                    "description": "How values should be rendered: FORMATTED_VALUE, UNFORMATTED_VALUE, FORMULA",
                    "default": "FORMATTED_VALUE"
                }
            },
            "required": ["spreadsheet_id", "range"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # sheets_update_values
    tool_registry.register_tool(
        name="sheets_update_values",
        function=sheets_update_values,
        description="Update values in a range of a spreadsheet.",
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Google Sheets spreadsheet ID"
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range (e.g., 'Sheet1!A1:D10')"
                },
                "values": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {}
                    },
                    "description": "2D array of values to update"
                },
                "value_input_option": {
                    "type": "string",
                    "description": "USER_ENTERED or RAW",
                    "default": "USER_ENTERED"
                }
            },
            "required": ["spreadsheet_id", "range", "values"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # sheets_append_values
    tool_registry.register_tool(
        name="sheets_append_values",
        function=sheets_append_values,
        description="Append values to the end of a range in a spreadsheet.",
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Google Sheets spreadsheet ID"
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range (e.g., 'Sheet1!A1:D1')"
                },
                "values": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {}
                    },
                    "description": "2D array of values to append"
                },
                "value_input_option": {
                    "type": "string",
                    "description": "USER_ENTERED or RAW",
                    "default": "USER_ENTERED"
                }
            },
            "required": ["spreadsheet_id", "range", "values"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # sheets_clear_values
    tool_registry.register_tool(
        name="sheets_clear_values",
        function=sheets_clear_values,
        description="Clear values from a range in a spreadsheet.",
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Google Sheets spreadsheet ID"
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range (e.g., 'Sheet1!A1:D10')"
                }
            },
            "required": ["spreadsheet_id", "range"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # sheets_batch_update
    tool_registry.register_tool(
        name="sheets_batch_update",
        function=sheets_batch_update,
        description="Perform batch updates on a spreadsheet (formatting, formulas, etc.).",
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Google Sheets spreadsheet ID"
                },
                "requests": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of request objects (see Google Sheets API documentation)"
                }
            },
            "required": ["spreadsheet_id", "requests"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # sheets_get_spreadsheet
    tool_registry.register_tool(
        name="sheets_get_spreadsheet",
        function=sheets_get_spreadsheet,
        description="Get metadata and properties of a spreadsheet.",
        parameters={
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "Google Sheets spreadsheet ID"
                },
                "include_grid_data": {
                    "type": "boolean",
                    "description": "Include cell data (default: False)",
                    "default": False
                }
            },
            "required": ["spreadsheet_id"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    logger.info("Google Sheets tools registered successfully")
