"""
Operationskatalogen.

Import av modulerna registrerar dem i REGISTRY. Ordningen här är den ordning
verktygen presenteras i - läsoperationer och kontext först, så modellen ser
"hämta först"-verktygen innan skrivverktygen.
"""

from . import (
    areas_ops,  # noqa: F401,E402
    context_ops,  # noqa: F401,E402
    faq_ops,  # noqa: F401,E402
    pages,  # noqa: F401,E402
    services_ops,  # noqa: F401,E402
)
from .base import (  # noqa: F401
    REGISTRY,
    Operation,
    OperationError,
    Prepared,
    register,
)


def get(name):
    """Operation efter namn, eller None."""
    return REGISTRY.get(name)


def all_operations():
    """Alla operationer i registreringsordning."""
    return list(REGISTRY.values())
