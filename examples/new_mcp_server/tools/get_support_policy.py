import logging
from langchain.tools import tool
logger = logging.getLogger(__name__)


@tool
def get_support_policy(customer_summary: str) -> str:
    """Return support policy decision based on a customer summary."""
    logger.info("get_support_policy invoke customer_summary=%s", customer_summary)
    if "Gold tier" in customer_summary:
        return "Escalate to priority support and offer live engineer callback."
    if "Free tier" in customer_summary:
        return "Use standard support queue and suggest upgrade path."
    return "Ask for more customer details before choosing support path."
