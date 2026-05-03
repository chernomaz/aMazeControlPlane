import logging
from langchain.tools import tool
logger = logging.getLogger(__name__)

@tool
def get_customer_profile(customer_id: str) -> str:
    """Return customer profile by customer id."""
    logger.info("get_customer_profile invoke customer_id=%s", customer_id)
    profiles = {
        "C123": "Customer C123 is Gold tier, prefers short technical answers, active subscription.",
        "C999": "Customer C999 is Free tier, prefers beginner explanations, trial expires soon.",
    }
    return profiles.get(customer_id, "Unknown customer")


