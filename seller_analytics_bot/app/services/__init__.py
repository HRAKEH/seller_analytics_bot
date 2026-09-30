"""Business-service package.

Import concrete services from their modules (for example
``app.services.collection``). Keeping this package initializer lightweight
prevents storage<->service circular imports during cold start.
"""
