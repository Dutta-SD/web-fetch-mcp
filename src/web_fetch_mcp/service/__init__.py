"""Service layer: orchestration and policy.

Holds the retry decorator, the tier strategy registry, the escalation ladder, and
the ``fetch_url`` facade. Depends inward on ``core`` and ``accessor``; holds no
framework (FastMCP) or external-I/O code of its own. A future ML relevance
router/reranker would live here alongside the escalation logic.
"""
