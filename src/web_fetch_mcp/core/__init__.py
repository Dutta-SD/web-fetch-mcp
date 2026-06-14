"""Core domain layer: pure types, config, and side-effect-free helpers.

This is the innermost layer — it imports nothing from the controller, service,
or accessor layers. A future ML page-state classifier would live in
``detection.py`` (with its model loaded via a thin accessor).
"""
