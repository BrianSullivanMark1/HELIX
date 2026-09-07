"""helix.api — the web shell's backend: FastAPI over localhost, WebSocket events, and the Qt-free
voice loop. This layer plays the role helix/ui plays for the Qt shell: it calls services, marshals
events, and owns no business logic. It must never import helix.ui (the Qt stack must not load in the
web process); everything shared between the two shells lives in services/ or adapters/.
"""
