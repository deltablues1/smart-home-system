"""
User-Friendly Error Messages

Maps error keys to human-readable messages in English and Croatian.
"""

ERROR_MESSAGES = {
    "en": {
        "quota_exceeded": "The system is currently experiencing high traffic. Retrying shortly...",
        "service_unavailable": "Google services are temporarily unavailable. Please try again in a moment.",
        "not_found": "The requested resource (file, email, or contact) could not be found. Please check the ID or name.",
        "unauthenticated": "Authentication failed. Please sign in again or check your credentials.",
        "permission_denied": "You do not have permission to access this resource.",
        "invalid_argument": "The request contained invalid data. Please check your input.",
        "auth_error": "There is an issue with your account credentials.",
        "network_error": "Network connection issue. Please check your internet connection.",
        "unknown_error": "An unexpected system error occurred. Our team has been notified."
    },
    "hr": {
        "quota_exceeded": "Sustav je trenutno opterećen (Quota Exceeded). Pokušavam ponovo za nekoliko sekundi...",
        "service_unavailable": "Google servisi su privremeno nedostupni. Molim pokušajte ponovo uskoro.",
        "not_found": "Traženi resurs (datoteka, email ili kontakt) nije pronađen. Molim provjerite ID ili naziv.",
        "unauthenticated": "Autentifikacija nije uspjela. Molim prijavite se ponovo.",
        "permission_denied": "Nemate dozvolu za pristup ovom resursu.",
        "invalid_argument": "Zahtjev sadrži neispravne podatke. Molim provjerite unos.",
        "auth_error": "Postoji problem s vašim vjerodajnicama (credentials).",
        "network_error": "Problem s mrežnom vezom. Molim provjerite internet.",
        "unknown_error": "Došlo je do neočekivane greške u sustavu."
    }
}

def get_error_message(key: str, language: str = "hr") -> str:
    """
    Get localized error message.
    
    Args:
        key: Error message key (from classifier)
        language: Language code ('en' or 'hr')
        
    Returns:
        Localized message string
    """
    lang_messages = ERROR_MESSAGES.get(language, ERROR_MESSAGES["en"])
    return lang_messages.get(key, lang_messages["unknown_error"])
