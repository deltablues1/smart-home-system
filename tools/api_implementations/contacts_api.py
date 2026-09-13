"""
Google Contacts API Implementation (People API)

Real Google Contacts API functions using Google People API v1
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
# GOOGLE CONTACTS API FUNCTIONS (People API)
# ============================================================================

@with_circuit_breaker("people")
@with_cache("people", ttl=900, user_id_param="credentials")  # Cache for 15 min (contacts rarely change)
@with_rate_limit("people", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def contacts_list_contacts(
    credentials: Credentials,
    page_size: int = 100,
    sort_order: str = "LAST_MODIFIED_DESCENDING"
) -> Dict[str, Any]:
    """
    List all contacts from the user's contact list

    Args:
        credentials: OAuth2 credentials
        page_size: Maximum number of contacts to return (default: 100, max: 1000)
        sort_order: Sort order for contacts
            - "LAST_MODIFIED_DESCENDING" (default)
            - "LAST_MODIFIED_ASCENDING"
            - "FIRST_NAME_ASCENDING"
            - "LAST_NAME_ASCENDING"

    Returns:
        Dictionary with list of contacts

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.people_service()

        logger.info(f"Listing contacts: page_size={page_size}, sort={sort_order}")

        # List contacts (connections)
        results = await aexecute(service.people().connections().list(
            resourceName='people/me',
            pageSize=page_size,
            personFields='names,emailAddresses,phoneNumbers,organizations,photos',
            sortOrder=sort_order
        ))

        connections = results.get('connections', [])

        # Parse contacts
        contacts = []
        for person in connections:
            contact = _parse_contact(person)
            contacts.append(contact)

        logger.info(f"Retrieved {len(contacts)} contacts")

        return {
            'contacts': contacts,
            'count': len(contacts),
            'next_page_token': results.get('nextPageToken'),
            'total_people': results.get('totalPeople', len(contacts))
        }

    except HttpError as e:
        logger.error(f"Failed to list contacts: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in contacts_list_contacts: {e}")
        raise


@with_circuit_breaker("people")
@with_cache("people", ttl=900, user_id_param="credentials")  # Cache for 15 min
@with_rate_limit("people", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def contacts_get_contact(
    credentials: Credentials,
    resource_name: str
) -> Dict[str, Any]:
    """
    Get a specific contact by resource name

    Args:
        credentials: OAuth2 credentials
        resource_name: Contact resource name (e.g., 'people/c1234567890')

    Returns:
        Dictionary with contact details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.people_service()

        logger.info(f"Getting contact: {resource_name}")

        # Get contact
        person = await aexecute(service.people().get(
            resourceName=resource_name,
            personFields='names,emailAddresses,phoneNumbers,addresses,organizations,birthdays,photos,biographies'
        ))

        contact = _parse_contact(person, detailed=True)

        logger.info(f"Contact retrieved: {contact.get('name', 'Unknown')}")

        return contact

    except HttpError as e:
        logger.error(f"Failed to get contact: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in contacts_get_contact: {e}")
        raise


@with_circuit_breaker("people")
@with_rate_limit("people", user_id_param="credentials")
# NOTE: no @with_retry here — create makes a new contact on every call.
# report_unconfirmed instead: a lost answer is reported as unknown, so
# the agent checks the result rather than repeating the write.
@report_unconfirmed("stvaranje kontakta")
@invalidates_cache("people")
async def contacts_create_contact(
    credentials: Credentials,
    given_name: str,
    family_name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    organization: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new contact

    Args:
        credentials: OAuth2 credentials
        given_name: First name
        family_name: Last name (optional)
        email: Email address (optional)
        phone: Phone number (optional)
        organization: Organization/company (optional)

    Returns:
        Dictionary with created contact details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.people_service()

        logger.info(f"Creating contact: {given_name} {family_name or ''}")

        # Build contact body
        contact_body = {
            'names': [{
                'givenName': given_name
            }]
        }

        if family_name:
            contact_body['names'][0]['familyName'] = family_name

        if email:
            contact_body['emailAddresses'] = [{
                'value': email
            }]

        if phone:
            contact_body['phoneNumbers'] = [{
                'value': phone
            }]

        if organization:
            contact_body['organizations'] = [{
                'name': organization
            }]

        # Create contact
        person = await aexecute(service.people().createContact(
            body=contact_body
        ))

        contact = _parse_contact(person, detailed=True)

        logger.info(f"Contact created: {person.get('resourceName')}")

        return {
            **contact,
            'status': 'created'
        }

    except HttpError as e:
        logger.error(f"Failed to create contact: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in contacts_create_contact: {e}")
        raise


@with_circuit_breaker("people")
@with_rate_limit("people", user_id_param="credentials")
# Retry is safe: the etag makes a stale repeat fail with 412 rather than
# apply twice.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("people")
async def contacts_update_contact(
    credentials: Credentials,
    resource_name: str,
    given_name: Optional[str] = None,
    family_name: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    organization: Optional[str] = None
) -> Dict[str, Any]:
    """
    Update an existing contact

    Args:
        credentials: OAuth2 credentials
        resource_name: Contact resource name (e.g., 'people/c1234567890')
        given_name: Updated first name (optional)
        family_name: Updated last name (optional)
        email: Updated email address (optional)
        phone: Updated phone number (optional)
        organization: Updated organization (optional)

    Returns:
        Dictionary with updated contact details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.people_service()

        logger.info(f"Updating contact: {resource_name}")

        # Get current contact first (etag is returned automatically, not as personField)
        current_person = await aexecute(service.people().get(
            resourceName=resource_name,
            personFields='names,emailAddresses,phoneNumbers,organizations'
        ))

        # Build update body
        update_body = {
            'resourceName': resource_name,
            'etag': current_person['etag']
        }

        # Update names
        if given_name or family_name:
            names = current_person.get('names', [{}])[0].copy()
            if given_name:
                names['givenName'] = given_name
            if family_name:
                names['familyName'] = family_name
            update_body['names'] = [names]
        else:
            update_body['names'] = current_person.get('names', [])

        # Update email: replace the primary (first) address, KEEP the others —
        # replacing the whole list would silently drop secondary addresses.
        if email:
            existing_emails = [
                dict(e) for e in current_person.get('emailAddresses', [])
            ]
            if any(e.get('value') == email for e in existing_emails):
                update_body['emailAddresses'] = existing_emails
            elif existing_emails:
                existing_emails[0]['value'] = email
                update_body['emailAddresses'] = existing_emails
            else:
                update_body['emailAddresses'] = [{'value': email}]
        else:
            update_body['emailAddresses'] = current_person.get('emailAddresses', [])

        # Update phone: same keep-the-rest semantics as email.
        if phone:
            existing_phones = [
                dict(p) for p in current_person.get('phoneNumbers', [])
            ]
            if any(p.get('value') == phone for p in existing_phones):
                update_body['phoneNumbers'] = existing_phones
            elif existing_phones:
                existing_phones[0]['value'] = phone
                update_body['phoneNumbers'] = existing_phones
            else:
                update_body['phoneNumbers'] = [{'value': phone}]
        else:
            update_body['phoneNumbers'] = current_person.get('phoneNumbers', [])

        # Update organization
        if organization:
            update_body['organizations'] = [{'name': organization}]
        else:
            update_body['organizations'] = current_person.get('organizations', [])

        # Update contact
        person = await aexecute(service.people().updateContact(
            resourceName=resource_name,
            updatePersonFields='names,emailAddresses,phoneNumbers,organizations',
            body=update_body
        ))

        contact = _parse_contact(person, detailed=True)

        logger.info(f"Contact updated: {resource_name}")

        return {
            **contact,
            'status': 'updated'
        }

    except HttpError as e:
        logger.error(f"Failed to update contact: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in contacts_update_contact: {e}")
        raise


@with_circuit_breaker("people")
@with_rate_limit("people", user_id_param="credentials")
# Retry is safe: a second delete returns 404, which is not retried.
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
@invalidates_cache("people")
async def contacts_delete_contact(
    credentials: Credentials,
    resource_name: str
) -> Dict[str, Any]:
    """
    Delete a contact

    Args:
        credentials: OAuth2 credentials
        resource_name: Contact resource name (e.g., 'people/c1234567890')

    Returns:
        Dictionary with deletion status

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.people_service()

        logger.info(f"Deleting contact: {resource_name}")

        # Delete contact
        await aexecute(service.people().deleteContact(
            resourceName=resource_name
        ))

        logger.info(f"Contact deleted: {resource_name}")

        return {
            'resource_name': resource_name,
            'status': 'deleted'
        }

    except HttpError as e:
        logger.error(f"Failed to delete contact: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in contacts_delete_contact: {e}")
        raise


@with_circuit_breaker("people")
@with_cache("people", ttl=60, user_id_param="credentials")  # Reduced cache to 1 min for faster newly created contact discovery
@with_rate_limit("people", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def contacts_search_contacts(
    credentials: Credentials,
    query: str,
    page_size: int = 30
) -> Dict[str, Any]:
    """
    Search contacts by query string

    Uses searchContacts API first, then falls back to connections.list if no results.
    This handles the Google People API propagation delay for newly created contacts.

    Args:
        credentials: OAuth2 credentials
        query: Search query (name, email, phone, etc.)
        page_size: Maximum number of results (default: 30)

    Returns:
        Dictionary with search results

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.people_service()

        logger.info(f"Searching contacts: query='{query}', page_size={page_size}")

        # First try: searchContacts API (faster but has propagation delay)
        results = await aexecute(service.people().searchContacts(
            query=query,
            pageSize=page_size,
            readMask='names,emailAddresses,phoneNumbers,photos'
        ))

        search_results = results.get('results', [])

        # Parse contacts from searchContacts
        contacts = []
        for result in search_results:
            person = result.get('person', {})
            contact = _parse_contact(person)
            contacts.append(contact)

        # Fallback: If searchContacts returns 0, use connections.list with local filtering
        # This handles newly created contacts that aren't indexed yet
        if len(contacts) == 0:
            logger.info(f"searchContacts returned 0 results, trying connections.list fallback for '{query}'")

            # Page through ALL connections and filter locally (a single
            # pageSize=200 call silently missed contacts beyond the first 200)
            query_lower = query.lower()
            page_token = None
            while True:
                connections_result = await aexecute(service.people().connections().list(
                    resourceName='people/me',
                    pageSize=200,
                    pageToken=page_token,
                    personFields='names,emailAddresses,phoneNumbers,organizations,photos'
                ))

                for person in connections_result.get('connections', []):
                    contact = _parse_contact(person)

                    # Check if query matches name, email, or phone
                    name_match = contact.get('name', '').lower().find(query_lower) >= 0
                    given_name_match = contact.get('given_name', '').lower().find(query_lower) >= 0
                    family_name_match = contact.get('family_name', '').lower().find(query_lower) >= 0
                    email_match = contact.get('email', '').lower().find(query_lower) >= 0
                    phone_match = query_lower in contact.get('phone', '').replace(' ', '').replace('-', '')

                    if name_match or given_name_match or family_name_match or email_match or phone_match:
                        contacts.append(contact)
                        if len(contacts) >= page_size:
                            break

                page_token = connections_result.get('nextPageToken')
                if len(contacts) >= page_size or not page_token:
                    break

            logger.info(f"Fallback search found {len(contacts)} contacts matching '{query}'")

        logger.info(f"Found {len(contacts)} contacts matching '{query}'")

        return {
            'contacts': contacts,
            'count': len(contacts),
            'query': query
        }

    except HttpError as e:
        logger.error(f"Failed to search contacts: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in contacts_search_contacts: {e}")
        raise


@with_circuit_breaker("people")
@with_cache("people", ttl=900, user_id_param="credentials")  # Cache for 15 min
@with_rate_limit("people", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def contacts_batch_get(
    credentials: Credentials,
    resource_names: List[str]
) -> Dict[str, Any]:
    """
    Get multiple contacts in a single batch request

    Args:
        credentials: OAuth2 credentials
        resource_names: List of contact resource names

    Returns:
        Dictionary with batch get results

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.people_service()

        logger.info(f"Batch getting {len(resource_names)} contacts")

        # Batch get
        results = await aexecute(service.people().getBatchGet(
            resourceNames=resource_names,
            personFields='names,emailAddresses,phoneNumbers,organizations,photos'
        ))

        responses = results.get('responses', [])

        # Parse contacts
        contacts = []
        for response in responses:
            if 'person' in response:
                person = response['person']
                contact = _parse_contact(person)
                contacts.append(contact)

        logger.info(f"Retrieved {len(contacts)} contacts in batch")

        return {
            'contacts': contacts,
            'count': len(contacts),
            'requested_count': len(resource_names)
        }

    except HttpError as e:
        logger.error(f"Failed to batch get contacts: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in contacts_batch_get: {e}")
        raise


@with_circuit_breaker("people")
@with_cache("people", ttl=900, user_id_param="credentials")  # Cache for 15 min
@with_rate_limit("people", user_id_param="credentials")
@with_retry(RetryConfig(max_retries=3, base_delay=1.0))
async def contacts_resolve_email(
    credentials: Credentials,
    name: str
) -> Dict[str, Any]:
    """
    Find email address for a person by name

    Args:
        credentials: OAuth2 credentials
        name: Person's name to search for

    Returns:
        Dictionary with email address and contact details

    Raises:
        HttpError: If API call fails
    """
    try:
        from tools.google_api_client import GoogleAPIClient

        api_client = GoogleAPIClient(credentials=credentials)
        service = api_client.people_service()

        logger.info(f"Resolving email for: {name}")

        # Search contacts
        results = await aexecute(service.people().searchContacts(
            query=name,
            pageSize=10,
            readMask='names,emailAddresses,phoneNumbers,photos'
        ))

        search_results = results.get('results', [])

        if not search_results:
            logger.warning(f"No contacts found for: {name}")
            return {
                'name': name,
                'email': None,
                'status': 'not_found',
                'message': f'No contact found with name "{name}"'
            }

        # Get first result (best match)
        person = search_results[0].get('person', {})
        contact = _parse_contact(person)

        email = contact.get('email', None)

        if not email:
            logger.warning(f"Contact found but no email: {name}")
            return {
                'name': contact.get('name', name),
                'resource_name': contact.get('resource_name'),
                'email': None,
                'status': 'no_email',
                'message': f'Contact "{contact.get("name", name)}" found but has no email address'
            }

        logger.info(f"Resolved email for {name}: {email}")

        return {
            'name': contact.get('name', name),
            'email': email,
            'resource_name': contact.get('resource_name'),
            'phone': contact.get('phone'),
            'status': 'found'
        }

    except HttpError as e:
        logger.error(f"Failed to resolve email: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in contacts_resolve_email: {e}")
        raise


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _parse_contact(person: Dict[str, Any], detailed: bool = False) -> Dict[str, Any]:
    """
    Parse contact from People API person object

    Args:
        person: Person object from People API
        detailed: Include all available fields (default: False)

    Returns:
        Parsed contact dictionary
    """
    contact = {
        'resource_name': person.get('resourceName', ''),
        'etag': person.get('etag', '')
    }

    # Name
    names = person.get('names', [])
    if names:
        name = names[0]
        contact['name'] = name.get('displayName', '')
        contact['given_name'] = name.get('givenName', '')
        contact['family_name'] = name.get('familyName', '')

    # Email
    emails = person.get('emailAddresses', [])
    if emails:
        contact['email'] = emails[0].get('value', '')
        if detailed:
            contact['emails'] = [e.get('value') for e in emails]

    # Phone
    phones = person.get('phoneNumbers', [])
    if phones:
        contact['phone'] = phones[0].get('value', '')
        if detailed:
            contact['phones'] = [p.get('value') for p in phones]

    # Organization
    organizations = person.get('organizations', [])
    if organizations:
        contact['organization'] = organizations[0].get('name', '')

    # Photo
    photos = person.get('photos', [])
    if photos:
        contact['photo_url'] = photos[0].get('url', '')

    # Detailed fields
    if detailed:
        # Addresses
        addresses = person.get('addresses', [])
        if addresses:
            contact['addresses'] = [
                {
                    'formatted': addr.get('formattedValue', ''),
                    'type': addr.get('type', '')
                }
                for addr in addresses
            ]

        # Birthdays
        birthdays = person.get('birthdays', [])
        if birthdays:
            birthday = birthdays[0].get('date', {})
            contact['birthday'] = f"{birthday.get('year', '')}-{birthday.get('month', '')}-{birthday.get('day', '')}"

        # Biography
        biographies = person.get('biographies', [])
        if biographies:
            contact['biography'] = biographies[0].get('value', '')

    return contact


# ============================================================================
# TOOL REGISTRATION
# ============================================================================

def register_contacts_tools(tool_registry):
    """
    Register all Google Contacts tools in the tool registry

    Args:
        tool_registry: ToolRegistry instance
    """
    # contacts_list_contacts
    tool_registry.register_tool(
        name="contacts_list_contacts",
        function=contacts_list_contacts,
        description="List all contacts from the user's Google Contacts.",
        parameters={
            "type": "object",
            "properties": {
                "page_size": {
                    "type": "integer",
                    "description": "Maximum number of contacts to return (default: 100, max: 1000)",
                    "default": 100
                },
                "sort_order": {
                    "type": "string",
                    "description": "Sort order: LAST_MODIFIED_DESCENDING, LAST_MODIFIED_ASCENDING, FIRST_NAME_ASCENDING, LAST_NAME_ASCENDING",
                    "default": "LAST_MODIFIED_DESCENDING"
                }
            },
            "required": []
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # contacts_get_contact
    tool_registry.register_tool(
        name="contacts_get_contact",
        function=contacts_get_contact,
        description="Get a specific contact by resource name.",
        parameters={
            "type": "object",
            "properties": {
                "resource_name": {
                    "type": "string",
                    "description": "Contact resource name (e.g., 'people/c1234567890')"
                }
            },
            "required": ["resource_name"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # contacts_create_contact
    tool_registry.register_tool(
        name="contacts_create_contact",
        function=contacts_create_contact,
        description="Create a new contact in Google Contacts.",
        parameters={
            "type": "object",
            "properties": {
                "given_name": {
                    "type": "string",
                    "description": "First name"
                },
                "family_name": {
                    "type": "string",
                    "description": "Last name (optional)"
                },
                "email": {
                    "type": "string",
                    "description": "Email address (optional)"
                },
                "phone": {
                    "type": "string",
                    "description": "Phone number (optional)"
                },
                "organization": {
                    "type": "string",
                    "description": "Organization/company (optional)"
                }
            },
            "required": ["given_name"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # contacts_update_contact
    tool_registry.register_tool(
        name="contacts_update_contact",
        function=contacts_update_contact,
        description="Update an existing contact in Google Contacts.",
        parameters={
            "type": "object",
            "properties": {
                "resource_name": {
                    "type": "string",
                    "description": "Contact resource name"
                },
                "given_name": {
                    "type": "string",
                    "description": "Updated first name (optional)"
                },
                "family_name": {
                    "type": "string",
                    "description": "Updated last name (optional)"
                },
                "email": {
                    "type": "string",
                    "description": "Updated email (optional)"
                },
                "phone": {
                    "type": "string",
                    "description": "Updated phone (optional)"
                },
                "organization": {
                    "type": "string",
                    "description": "Updated organization (optional)"
                }
            },
            "required": ["resource_name"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # contacts_delete_contact
    tool_registry.register_tool(
        name="contacts_delete_contact",
        function=contacts_delete_contact,
        description="Delete a contact from Google Contacts.",
        parameters={
            "type": "object",
            "properties": {
                "resource_name": {
                    "type": "string",
                    "description": "Contact resource name"
                }
            },
            "required": ["resource_name"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # contacts_search_contacts
    tool_registry.register_tool(
        name="contacts_search_contacts",
        function=contacts_search_contacts,
        description="Search contacts by name, email, phone, or other fields.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query"
                },
                "page_size": {
                    "type": "integer",
                    "description": "Maximum number of results (default: 30)",
                    "default": 30
                }
            },
            "required": ["query"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # contacts_batch_get
    tool_registry.register_tool(
        name="contacts_batch_get",
        function=contacts_batch_get,
        description="Get multiple contacts in a single batch request.",
        parameters={
            "type": "object",
            "properties": {
                "resource_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of contact resource names"
                }
            },
            "required": ["resource_names"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    # contacts_resolve_email
    tool_registry.register_tool(
        name="contacts_resolve_email",
        function=contacts_resolve_email,
        description="Find email address for a person by name. Returns the email of the best matching contact.",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Person's name to search for"
                }
            },
            "required": ["name"]
        },
        requires_auth=True,
        auth_type="oauth"
    )

    logger.info("Google Contacts tools registered successfully")
