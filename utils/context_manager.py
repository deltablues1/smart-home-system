"""
Context Manager

Helper funkcije za upravljanje kontekstom i povijesti u multi-agent sustavu
"""

from typing import List, Dict, Any, Optional
import logging

logger = logging.getLogger(__name__)


class ContextManager:
    """
    Upravlja kontekstom i povijesti za ADK agente

    Odgovornosti:
    - Filtriranje povijesti po relevantnosti
    - Sažimanje velike povijesti
    - Kreiranje scoped context za sub-agente
    """

    @staticmethod
    def filter_history_by_agent(
        history: List[Dict[str, Any]],
        agent_name: str,
        max_messages: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Filtrira povijest relevantnu za određenog agenta

        Args:
            history: Puna povijest konverzacije
            agent_name: Naziv agenta
            max_messages: Maksimalan broj poruka za zadržati

        Returns:
            Filtrirana povijest
        """
        # Zadržava samo poruke relevantne za ovog agenta
        relevant = []

        for msg in history:
            # Zadrži user poruke i poruke od/prema ovom agentu
            if (
                msg.get('role') == 'user' or
                msg.get('agent') == agent_name or
                msg.get('to_agent') == agent_name
            ):
                relevant.append(msg)

        # Ograniči na max_messages (zadrži najnovije)
        if len(relevant) > max_messages:
            relevant = relevant[-max_messages:]

        logger.debug(f"Filtered history for {agent_name}: {len(relevant)} messages")
        return relevant

    @staticmethod
    def create_scoped_context(
        parent_context: Dict[str, Any],
        agent_name: str,
        task_description: str
    ) -> Dict[str, Any]:
        """
        Kreira scoped context za sub-agenta

        Args:
            parent_context: Kontekst od parent agenta
            agent_name: Naziv sub-agenta
            task_description: Opis zadatka

        Returns:
            Novi scoped context dictionary
        """
        return {
            'agent': agent_name,
            'task': task_description,
            'parent_session': parent_context.get('session_id'),
            'inherited_context': {
                'user_id': parent_context.get('user_id'),
                'timezone': parent_context.get('timezone'),
                'preferences': parent_context.get('preferences', {}),
            }
        }

    @staticmethod
    def summarize_history(
        history: List[Dict[str, Any]],
        max_length: int = 500
    ) -> str:
        """
        Sažima povijest u kraći tekst

        Args:
            history: Povijest poruka
            max_length: Maksimalna duljina sažetka

        Returns:
            Sažetak povijesti
        """
        if not history:
            return "No previous context."

        # Ekstrakcija ključnih informacija
        user_queries = [
            msg.get('content', '')
            for msg in history
            if msg.get('role') == 'user'
        ]

        summary = "Previous context:\n"
        for i, query in enumerate(user_queries[-3:], 1):  # Zadnje 3 query-a
            summary += f"{i}. {query[:100]}...\n"

        if len(summary) > max_length:
            summary = summary[:max_length] + "..."

        return summary

    @staticmethod
    def extract_entities(message: str) -> Dict[str, List[str]]:
        """
        Ekstraktira entitete iz poruke (emails, dates, names, etc.)

        Args:
            message: Poruka za analizu

        Returns:
            Dictionary s entitetima
        """
        import re

        entities = {
            'emails': [],
            'dates': [],
            'urls': [],
            'numbers': []
        }

        # Email pattern
        email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        entities['emails'] = re.findall(email_pattern, message)

        # URL pattern
        url_pattern = r'https?://[^\s]+'
        entities['urls'] = re.findall(url_pattern, message)

        # Date patterns (simple)
        date_pattern = r'\b\d{4}-\d{2}-\d{2}\b'
        entities['dates'] = re.findall(date_pattern, message)

        # Numbers
        number_pattern = r'\b\d+\b'
        entities['numbers'] = re.findall(number_pattern, message)

        logger.debug(f"Extracted entities: {entities}")
        return entities


# Singleton instance
_context_manager: Optional[ContextManager] = None


def get_context_manager() -> ContextManager:
    """
    Dohvaća singleton instancu ContextManager-a

    Returns:
        ContextManager instance
    """
    global _context_manager
    if _context_manager is None:
        _context_manager = ContextManager()
    return _context_manager
