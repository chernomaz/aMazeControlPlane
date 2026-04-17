import logging
from typing import Optional
from langchain_core.tools import tool
from langsmith import traceable

logger = logging.getLogger(__name__)

# Static list of predefined emails used as dummy data for testing
EMAILS = [
    {
        "to": "alice",
        "subject": "Q1 Report",
        "body": "Hi Alice, please find the Q1 report attached. Let me know if you have any questions.",
    },
    {
        "to": "bob",
        "subject": "Meeting Tomorrow",
        "body": "Hi Bob, just a reminder that we have a team meeting tomorrow at 10am. See you there!",
    },
    {
        "to": "carol",
        "subject": "Project Update",
        "body": "Hi Carol, wanted to share a quick update on the project — we are on track for the deadline.",
    },
]


@tool
@traceable(name="dummy_email")
def dummy_email(person: Optional[str] = None) -> str:
    """Return predefined dummy emails. If person is specified, return only the email for that person.
    Known recipients: alice, bob, carol.
    """
    logger.info("dummy_email invoke person=%s", person)
    if person:
        # Filter emails by recipient name (case-insensitive)
        match = [e for e in EMAILS if e["to"].lower() == person.strip().lower()]
        if not match:
            return f"No email found for '{person}'."
        e = match[0]
        return f"To: {e['to']}\nSubject: {e['subject']}\n\n{e['body']}"

    # No person specified — return all emails separated by a divider
    parts = []
    for e in EMAILS:
        parts.append(f"To: {e['to']}\nSubject: {e['subject']}\n\n{e['body']}")
    return "\n\n---\n\n".join(parts)
