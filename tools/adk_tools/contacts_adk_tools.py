"""
Google Contacts ADK Tools

ADK-compatible wrappers for Google Contacts (People API) operations.
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
        logger.warning("No valid credentials available for Contacts operations")
        return None
    except Exception as e:
        logger.error(f"Failed to get credentials: {e}")
        return None


# ============================================================================
# CONTACTS TOOLS
# ============================================================================

async def contacts_search_people(
    query: str,
    max_results: int = 10
) -> dict:
    """
    Search for contacts by name, email, or phone number.

    Use this to find contact information for people in your contact list.
    Perfect for getting email addresses before sending emails.

    Args:
        query: Search query - can be name, email, or phone number
               Examples: "Ana Horvat", "john@example.com", "+385..."
        max_results: Maximum number of results to return (default: 10)

    Returns:
        Dictionary with:
        {
            "contacts": [
                {
                    "name": "Ana Horvat",
                    "email": "tomislav.golic@example.com",
                    "resource_name": "people/c123456",
                    "phone": "+385...",
                    "photo_url": "https://...",
                    "organization": "Company Name"
                },
                ...
            ],
            "count": 2,
            "query": "Ana Horvat"
        }

    Example:
        # Find contact by name
        result = await contacts_search_people(query="Ana Horvat")
        if result['contacts']:
            email = result['contacts'][0]['email']
            print(f"Email: {email}")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated",
            "contacts": [],
            "count": 0
        }

    try:
        from tools.api_implementations.contacts_api import contacts_search_contacts

        result = await contacts_search_contacts(
            credentials=creds,
            query=query,
            page_size=max_results
        )

        logger.info(f"Found {result.get('count', 0)} contacts for query: '{query}'")

        return {
            "contacts": result.get('contacts', []),
            "count": result.get('count', 0),
            "query": query,
            "status": "success"
        }

    except Exception as e:
        logger.error(f"Error searching contacts: {e}")
        return {
            "error": str(e),
            "status": "error",
            "contacts": [],
            "count": 0,
            "query": query
        }


async def contacts_list_all(
    max_results: int = 100,
    sort_order: str = "LAST_MODIFIED_DESCENDING"
) -> dict:
    """
    List all contacts from your Google Contacts.

    Args:
        max_results: Maximum number of contacts to return (default: 100, max: 1000)
        sort_order: How to sort contacts
                   - "LAST_MODIFIED_DESCENDING" (default - recently updated first)
                   - "LAST_MODIFIED_ASCENDING"
                   - "FIRST_NAME_ASCENDING"
                   - "LAST_NAME_ASCENDING"

    Returns:
        Dictionary with all contacts and their details

    Example:
        result = await contacts_list_all(max_results=50)
        for contact in result['contacts']:
            print(f"{contact['name']}: {contact['email']}")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated",
            "contacts": [],
            "count": 0
        }

    try:
        from tools.api_implementations.contacts_api import contacts_list_contacts

        result = await contacts_list_contacts(
            credentials=creds,
            page_size=max_results,
            sort_order=sort_order
        )

        logger.info(f"Listed {result.get('count', 0)} contacts")

        return {
            "contacts": result.get('contacts', []),
            "count": result.get('count', 0),
            "total": result.get('total_people', result.get('count', 0)),
            "status": "success"
        }

    except Exception as e:
        logger.error(f"Error listing contacts: {e}")
        return {
            "error": str(e),
            "status": "error",
            "contacts": [],
            "count": 0
        }


async def contacts_get_by_name(
    name: str
) -> dict:
    """
    Get contact details by searching for a specific name.

    Searches for a contact by name. Returns the contact ONLY when exactly one
    person matches. With multiple matches it returns status "ambiguous" and a
    candidates list — present those to the user and ask which one they meant;
    NEVER silently pick one (wrong pick = email/invite to the wrong person).

    Args:
        name: Person's name to search for (e.g., "Ana Horvat")

    Returns:
        Dictionary with contact details if exactly one match:
        {
            "found": True,
            "name": "Ana Horvat",
            "email": "tomislav.golic@example.com",
            ...
        }

        Multiple matches:
        {
            "found": False,
            "status": "ambiguous",
            "candidates": [{"name": ..., "email": ..., "organization": ...}, ...]
        }

        Or if not found:
        {
            "found": False,
            "error": "No contact found with name: Ana Horvat"
        }
    """
    try:
        # Fetch several matches so ambiguity is detectable
        search_result = await contacts_search_people(query=name, max_results=5)

        if search_result.get('count', 0) == 0:
            return {
                "found": False,
                "error": f"No contact found with name: {name}",
                "status": "not_found"
            }

        contacts = search_result.get('contacts', [])
        if len(contacts) > 1:
            return {
                "found": False,
                "status": "ambiguous",
                "message": (
                    f"Found {len(contacts)} contacts matching '{name}'. "
                    "Ask the user which one they meant before proceeding."
                ),
                "candidates": [
                    {
                        "name": c.get('name'),
                        "email": c.get('email'),
                        "organization": c.get('organization'),
                        "resource_name": c.get('resource_name'),
                    }
                    for c in contacts
                ],
            }

        contact = contacts[0]

        return {
            "found": True,
            "name": contact.get('name'),
            "email": contact.get('email'),
            "phone": contact.get('phone'),
            "resource_name": contact.get('resource_name'),
            "organization": contact.get('organization'),
            "photo_url": contact.get('photo_url'),
            "status": "success"
        }

    except Exception as e:
        logger.error(f"Error getting contact by name: {e}")
        return {
            "found": False,
            "error": str(e),
            "status": "error"
        }


async def contacts_create_contact(
    name: str,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    organization: Optional[str] = None
) -> dict:
    """
    Create a new contact in Google Contacts.

    Use this to add new people to the user's contact list.

    Args:
        name: Full name of the contact (required)
              Example: "Marko Marić" or "Ana Horvat"
        email: Email address (optional)
               Example: "davor@example.com"
        phone: Phone number (optional)
               Example: "+385 91 123 4567" or "0911234567"
        organization: Company or organization name (optional)
                      Example: "Acme Corp"

    Returns:
        Dictionary with created contact details:
        {
            "status": "created",
            "name": "Marko Marić",
            "email": "davor@example.com",
            "phone": "+385 91 123 4567",
            "resource_name": "people/c123456789"
        }

        Or on error:
        {
            "status": "error",
            "error": "Error message"
        }

    Example:
        result = await contacts_create_contact(
            name="Marko Marić",
            email="marko@example.com",
            phone="0911234567"
        )
        if result['status'] == 'created':
            print(f"Contact created: {result['name']}")
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.contacts_api import contacts_create_contact as api_create_contact

        # Parse name into given_name and family_name
        # Support formats: "First Last", "First", "First Middle Last"
        name_parts = name.strip().split()
        given_name = name_parts[0] if name_parts else name
        family_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else None

        logger.info(f"Creating contact: given_name='{given_name}', family_name='{family_name}'")

        result = await api_create_contact(
            credentials=creds,
            given_name=given_name,
            family_name=family_name,
            email=email,
            phone=phone,
            organization=organization
        )

        if result.get('status') == 'created':
            logger.info(f"Created contact: {name}")
            return {
                "status": "created",
                "name": result.get('name', name),
                "email": result.get('email'),
                "phone": result.get('phone'),
                "resource_name": result.get('resource_name'),
                "message": f"Contact '{name}' created successfully"
            }
        else:
            return {
                "status": "error",
                "error": result.get('error', 'Unknown error creating contact')
            }

    except UnconfirmedWrite as e:
        # The write may already have landed; only the answer is gone. Reporting
        # "failed" here would read as "nothing happened" and invite the model to
        # call this tool again — the duplicate the missing retry was meant to
        # prevent, arriving one level up instead.
        logger.warning("Unconfirmed write in contacts_create_contact: %s", e)
        return {"error": str(e), "status": "unknown", "outcome": "unknown"}
    except Exception as e:
        logger.error(f"Error creating contact: {e}")
        return {
            "status": "error",
            "error": str(e)
        }


async def contacts_update_contact(
    resource_name: str,
    name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    organization: Optional[str] = None
) -> dict:
    """
    Update an existing contact in Google Contacts.

    Use this to modify contact information for people in the user's contact list.
    You must first search for a contact to get their resource_name.

    Args:
        resource_name: Contact resource name (required)
                      Example: "people/c1234567890123456789"
                      Get this from contacts_search_people or contacts_list_all
        name: Updated full name (optional)
              Example: "Ana Marija Horvat"
        email: Updated email address (optional)
               Example: "ana.new@example.com"
        phone: Updated phone number (optional)
               Example: "+385 99 999 9999"
        organization: Updated company/organization (optional)
                      Example: "New Company d.o.o."

    Returns:
        Dictionary with updated contact details:
        {
            "status": "updated",
            "name": "Ana Marija Horvat",
            "email": "ana.new@example.com",
            "resource_name": "people/c1234567890123456789"
        }

        Or on error:
        {
            "status": "error",
            "error": "Error message"
        }

    Example:
        # First search for the contact
        search = await contacts_search_people(query="Ana Horvat")
        resource_name = search['contacts'][0]['resource_name']

        # Then update
        result = await contacts_update_contact(
            resource_name=resource_name,
            phone="+385 98 765 4321"
        )
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.contacts_api import contacts_update_contact as api_update_contact

        # Parse name into given_name and family_name if provided
        given_name = None
        family_name = None
        if name:
            name_parts = name.strip().split()
            given_name = name_parts[0] if name_parts else None
            family_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else None

        logger.info(f"Updating contact: {resource_name}")

        result = await api_update_contact(
            credentials=creds,
            resource_name=resource_name,
            given_name=given_name,
            family_name=family_name,
            email=email,
            phone=phone,
            organization=organization
        )

        if result.get('status') == 'updated':
            logger.info(f"Updated contact: {resource_name}")
            return {
                "status": "updated",
                "name": result.get('name'),
                "email": result.get('email'),
                "phone": result.get('phone'),
                "resource_name": result.get('resource_name'),
                "message": f"Contact updated successfully"
            }
        else:
            return {
                "status": "error",
                "error": result.get('error', 'Unknown error updating contact')
            }

    except Exception as e:
        logger.error(f"Error updating contact: {e}")
        return {
            "status": "error",
            "error": str(e)
        }


async def contacts_delete_contact(
    resource_name: str
) -> dict:
    """
    Delete a contact from Google Contacts.

    Use this to permanently remove a contact from the user's contact list.
    This action cannot be undone!

    Args:
        resource_name: Contact resource name (required)
                      Example: "people/c1234567890123456789"
                      Get this from contacts_search_people or contacts_list_all

    Returns:
        Dictionary with deletion status:
        {
            "status": "deleted",
            "resource_name": "people/c1234567890123456789",
            "message": "Contact deleted successfully"
        }

        Or on error:
        {
            "status": "error",
            "error": "Error message"
        }

    Example:
        # First search for the contact
        search = await contacts_search_people(query="Ana Horvat")
        resource_name = search['contacts'][0]['resource_name']

        # Confirm with user before deleting!
        # Then delete
        result = await contacts_delete_contact(resource_name=resource_name)
    """
    creds = _get_credentials()
    if creds is None:
        return {
            "error": "Authentication required. Run: python tools/oauth_cli.py --auth",
            "status": "unauthenticated"
        }

    try:
        from tools.api_implementations.contacts_api import contacts_delete_contact as api_delete_contact

        logger.info(f"Deleting contact: {resource_name}")

        result = await api_delete_contact(
            credentials=creds,
            resource_name=resource_name
        )

        if result.get('status') == 'deleted':
            logger.info(f"Deleted contact: {resource_name}")
            return {
                "status": "deleted",
                "resource_name": resource_name,
                "message": "Contact deleted successfully"
            }
        else:
            return {
                "status": "error",
                "error": result.get('error', 'Unknown error deleting contact')
            }

    except Exception as e:
        logger.error(f"Error deleting contact: {e}")
        return {
            "status": "error",
            "error": str(e)
        }
