"""Conversation policy shared by the terminal agent and MCP hosts."""

from pathlib import Path

INSTRUCTIONS_PATH = Path(__file__).with_name("instructions.md")
ASSISTANT_CONTRACT = INSTRUCTIONS_PATH.read_text(encoding="utf-8")


ONBOARDING_WELCOME = (
    "I can help you understand what you own and where you’re exposed, assess "
    "investments you’re considering, and plan around goals, income and taxes.\n\n"
    "I’d like to get to know your finances a little. We can start with your "
    "savings and current portfolio, discuss an investment you have in mind, "
    "or do both together. Tell me a little about what you own and what "
    "you’re thinking about—a rough picture is enough to get started."
)
