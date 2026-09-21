"""Conversation policy shared by the terminal agent and MCP hosts."""

from pathlib import Path

INSTRUCTIONS_PATH = Path(__file__).with_name("instructions.md")
ASSISTANT_CONTRACT = INSTRUCTIONS_PATH.read_text(encoding="utf-8")


ONBOARDING_WELCOME = (
    "I can help you understand what you own and where you’re exposed, assess "
    "investments you’re considering, and plan around goals, income and taxes.\n\n"
    "Let’s start with your financial situation: what comes in each month, "
    "roughly what you spend, and what you have in savings, investments or debt. "
    "Estimates are fine; share whatever you know, and we’ll fill in the gaps "
    "together. If there’s a particular decision on your mind, include that too."
)
