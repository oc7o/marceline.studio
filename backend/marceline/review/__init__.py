"""Internal moderation API for the review SPA (`frontend/`, ADR 0014).

Staff-only Ninja routers over existing models and services. The project package may import every
app; the apps never import this package.
"""
