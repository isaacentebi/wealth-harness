"""Conversation policy shared by the terminal agent and MCP hosts."""

from pathlib import Path

INSTRUCTIONS_PATH = Path(__file__).with_name("instructions.md")
ASSISTANT_CONTRACT = INSTRUCTIONS_PATH.read_text(encoding="utf-8")


ONBOARDING_WELCOME = (
    "Let’s start with the rough picture: what comes in each month, what goes "
    "out, and what you have saved, invested or owe. Estimates are fine. If a "
    "statement is easier, attach it."
)

ONBOARDING_WELCOME_ES = (
    "Empecemos por el panorama general: cuánto te entra al mes, cuánto gastas "
    "y qué tienes ahorrado, invertido o por pagar. Con cifras aproximadas basta. "
    "Si te es más fácil, adjunta un estado de cuenta."
)
