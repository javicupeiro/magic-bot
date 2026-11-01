"""Rulebook pipeline: PDF text extraction, structured parsing and cached indexing.

documents defines the shared source contract; embeddings isolates vector creation.
Each processing stage exposes a Python function and its own terminal entry point.
"""
